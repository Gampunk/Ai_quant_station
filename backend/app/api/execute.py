"""
Sandbox for AI-written Python, used by the AI Analyst, Autopilot, Historical Lab
and Prompt Backtest. There is deliberately no endpoint that runs code sent by a
client; an earlier POST /api/execute/code did, and nothing in the app used it.

Multi-user isolation:
  - When user_id is provided, sandbox runs in an isolated subprocess
  - Session state is keyed by user_id:symbol to prevent cross-user leaks
  - Each subprocess exits after one execution (no state persistence risk)
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import io
import contextlib
import logging
import traceback
import math
import json
import base64
import asyncio
from ..core.security import get_current_user
import time
import hashlib
import os
import sys
import subprocess
import uuid
import re

from ..core.utils import sanitize_for_json as _sanitize

router = APIRouter(prefix="/execute", tags=["AI"])


# ── Sandbox session state ──────────────────────────────────────────────────
_SESSION_TTL = 300
_sandbox_state: dict[str, dict] = {}

# Keys excluded from session persistence (modules, internals, helpers)
_SESSION_EXCLUDE = {
    "__builtins__", "builtins", "_real_builtins", "_SAFE_IMPORT_MODULES",
    "_safe_import", "safe_builtins", "safe_globals",
    "show_chart", "show_table",
    "_charts", "_tables", "backtest_result",
    "pd", "np", "ta", "scipy", "stats", "cluster",
    "sm", "sklearn", "sns", "tabulate", "yf", "plt",
    "math", "json", "random", "itertools",
    "collections", "decimal", "warnings",
}


def _session_key(symbol: Optional[str], user_id: int = 0) -> str:
    """Session key isolated by user_id to prevent cross-user data leaks."""
    raw = (symbol or "default").strip().lower()
    if user_id:
        return f"u{user_id}:sess:{raw}"
    return f"sess:{raw}"


def _prune_expired() -> None:
    now = time.monotonic()
    expired = [k for k, v in _sandbox_state.items() if now - v["_ts"] > _SESSION_TTL]
    for k in expired:
        del _sandbox_state[k]


def _get_session(symbol: Optional[str], user_id: int = 0) -> dict:
    _prune_expired()
    key = _session_key(symbol, user_id)
    if key not in _sandbox_state:
        _sandbox_state[key] = {"_ts": time.monotonic()}
    else:
        _sandbox_state[key]["_ts"] = time.monotonic()
    return _sandbox_state[key]


def _capture_json_safe_state(safe_globals: dict) -> dict:
    """Capture user-defined variables that are JSON-serializable only."""
    state = {}
    for k, v in safe_globals.items():
        if k.startswith("_") or k in _SESSION_EXCLUDE:
            continue
        try:
            json.dumps(v)
            state[k] = v
        except (TypeError, OverflowError):
            pass
    return state


def _restore_state(safe_globals: dict, session: dict) -> None:
    for k, v in session.items():
        if k == "_ts":
            continue
        safe_globals[k] = v


def _strip_docstrings(code: str) -> str:
    """Remove triple-quoted strings (docstrings) from AI-generated code to avoid syntax errors."""
    import re
    code = re.sub(r'"""[\s\S]*?"""', '', code)
    code = re.sub(r"'''[\s\S]*?'''", '', code)
    code = re.sub(r'"""[\s\S]*', '', code)
    code = re.sub(r"'''[\s\S]*", '', code)
    return code


_DANGEROUS_DUNDERS = [
    '__class__', '__bases__', '__mro__', '__subclasses__',
    '__globals__', '__builtins__', '__code__', '__closure__',
]

def _code_has_dunder_access(code: str) -> bool:
    """Check for __dunder__ escapes via AST (dot access) AND via text-level scan (getattr strings, comments, etc)."""

    # Text-level scan: dangerous dunders anywhere in the code (catches getattr strings)
    for d in _DANGEROUS_DUNDERS:
        if d in code:
            return True

    # Scan for chr(95) / chr(0x5f) / chr(0X5F) — obfuscated underscore construction
    if re.search(r'chr\s*\(\s*(?:95|0[xX]5[fF])\s*\)', code):
        return True

    # Scan for hex escape in string literals (e.g. "\x5f\x5fclass\x5f\x5f")
    if re.search(r'\\x5[fF]', code):
        return True

    # AST-level attribute scan (catches obj.__class__ dot access)
    import ast
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in _DANGEROUS_DUNDERS:
                return True
        return False
    except SyntaxError:
        return True


# ── Core Sandbox (Synchronous) ─────────────────────────────────────────────
def _execute_sandbox_sync(
    code: str,
    market_data: Optional[List[Dict[str, Any]]] = None,
    symbol: Optional[str] = None,
    session_state: Optional[dict] = None,
    inject_df=None,
) -> dict:
    """Synchronous sandbox execution. Safe to run in a subprocess.

    Returns a JSON-serializable dict with keys:
      success, output, error, data_preview, charts, tables,
      modified_data, session_state
    """
    # Create DataFrame from market data or use injected DataFrame
    df = None
    if inject_df is not None:
        df = inject_df
    elif market_data:
        try:
            import pandas as _pd
            df = _pd.DataFrame(market_data)
            for col in ['open', 'high', 'low', 'close', 'volume']:
                if col in df.columns:
                    df[col] = _pd.to_numeric(df[col], errors='coerce')
            if 'volume' not in df.columns:
                df['volume'] = 0  # MT5 may not include volume — prevent AI code from crashing
            # Restore datetime index from 'datetime' column when available
            if 'datetime' in df.columns:
                df['datetime'] = _pd.to_datetime(df['datetime'])
                df = df.set_index('datetime')
                df.index.name = None
                # Add 'timestamp' column as datetime (not int64) so resample(on='timestamp') works
                df['timestamp'] = df.index
            elif 'time' in df.columns:
                df['time'] = _pd.to_datetime(df['time'])
                df = df.set_index('time')
                df.index.name = None
                df['timestamp'] = df.index
        except Exception as e:
            return {"success": False, "error": f"Failed to create DataFrame: {str(e)}"}

    # Guard: reject tiny DataFrames that would crash windowed indicators
    if df is not None and len(df) < 10:
        return {
            "success": False,
            "error": f"Insufficient data: DataFrame has only {len(df)} rows. Most indicators require at least 20-200 candles. Please load more data and try again.",
            "output": f"DataFrame has {len(df)} rows — too few for technical indicator calculations.",
        }

    # Build execution environment with RESTRICTED builtins
    import random, itertools, collections, decimal, warnings
    import builtins as _real_builtins

    _SAFE_IMPORT_MODULES = {
        'pandas', 'numpy', 'math', 'json', 'random', 'itertools', 'collections',
        'decimal', 'warnings', 'ta', 'scipy', 'statsmodels', 'sklearn', 'seaborn',
        'tabulate', 'yfinance', 'matplotlib', 'datetime',
    }

    def _safe_import(name, *args, **kwargs):
        base = name.split('.')[0]
        if base not in _SAFE_IMPORT_MODULES:
            raise ImportError(f"Module '{name}' is not allowed in sandbox")
        return _real_builtins.__import__(name, *args, **kwargs)

    safe_builtins = {
        'abs': abs, 'all': all, 'any': any, 'bool': bool, 'dict': dict,
        'enumerate': enumerate, 'float': float, 'int': int, 'len': len,
        'list': list, 'max': max, 'min': min, 'range': range,
        'round': round, 'sorted': sorted, 'str': str, 'sum': sum,
        'tuple': tuple, 'type': type, 'zip': zip, 'map': map, 'filter': filter,
        'True': True, 'False': False, 'None': None,
        'isinstance': isinstance, 'hasattr': hasattr, 'getattr': getattr,
        'setattr': setattr, 'reversed': reversed, 'slice': slice,
        'iter': iter, 'next': next, 'print': print, 'Exception': Exception,
        'ValueError': ValueError, 'TypeError': TypeError, 'KeyError': KeyError,
        'IndexError': IndexError, 'ZeroDivisionError': ZeroDivisionError,
        'KeyboardInterrupt': KeyboardInterrupt,
        '__import__': _safe_import,
    }

    safe_globals = {
        '__builtins__': safe_builtins,
        'pd': None, 'np': None,
        'math': math, 'json': json, 'random': random,
        'itertools': itertools, 'collections': collections,
        'decimal': decimal, 'warnings': warnings,
        'df': df, 'symbol': symbol,
        'print': print,
        'show_chart': None, 'show_table': None,
        '_charts': [], '_tables': [],
    }

    # Restore previous session variables
    if session_state:
        _restore_state(safe_globals, session_state)

    # Import commonly needed libraries
    try:
        import pandas as pd
        import numpy as np
        safe_globals['pd'] = pd
        safe_globals['np'] = np

        try:
            import ta
            safe_globals['ta'] = ta
        except Exception:
            traceback.print_exc()

        try:
            import scipy
            import scipy.stats as stats
            import scipy.cluster as cluster
            safe_globals['scipy'] = scipy
            safe_globals['stats'] = stats
            safe_globals['cluster'] = cluster
        except Exception:
            traceback.print_exc()

        try:
            import statsmodels.api as sm
            from statsmodels.tsa.stattools import adfuller, coint
            from statsmodels.regression.linear_model import OLS
            safe_globals['sm'] = sm
            safe_globals['adfuller'] = adfuller
            safe_globals['coint'] = coint
        except Exception:
            traceback.print_exc()

        try:
            import sklearn
            from sklearn.model_selection import train_test_split
            from sklearn.preprocessing import StandardScaler
            from sklearn.ensemble import RandomForestRegressor, RandomForestClassifier
            from sklearn.linear_model import LinearRegression, LogisticRegression
            from sklearn.metrics import mean_squared_error, r2_score, accuracy_score
            safe_globals['sklearn'] = sklearn
            safe_globals['train_test_split'] = train_test_split
            safe_globals['StandardScaler'] = StandardScaler
            safe_globals['RandomForestRegressor'] = RandomForestRegressor
            safe_globals['LinearRegression'] = LinearRegression
        except Exception:
            traceback.print_exc()

        try:
            import seaborn as sns
            safe_globals['sns'] = sns
        except Exception:
            traceback.print_exc()

        try:
            from tabulate import tabulate
            safe_globals['tabulate'] = tabulate
        except Exception:
            traceback.print_exc()

        try:
            import yfinance as yf
            safe_globals['yf'] = yf
        except Exception:
            traceback.print_exc()

        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        def show_chart(data, title="Chart", color="#2563eb", chart_type="line"):
            entry = {"title": title, "color": color, "type": chart_type}
            if isinstance(data, dict):
                entry["data"] = {k: (v.tolist() if hasattr(v, 'tolist') else v) for k, v in data.items()}
            elif isinstance(data, list):
                if data and isinstance(data[0], dict) and "label" in data[0]:
                    entry["type"] = "multi"
                    entry["multi_series"] = [
                        {"label": s["label"], "data": s["data"].tolist() if hasattr(s["data"], 'tolist') else s["data"]}
                        for s in data
                    ]
                    entry.pop("data", None)
                else:
                    entry["data"] = data
            elif hasattr(data, 'tolist'):
                entry["data"] = data.tolist()
            else:
                return
            safe_globals['_charts'].append(entry)

        def show_table(data, title="Data"):
            if isinstance(data, pd.DataFrame):
                safe_globals['_tables'].append({
                    "title": title,
                    "columns": list(data.columns),
                    "rows": data.head(50).values.tolist()
                })
            elif isinstance(data, list):
                safe_globals['_tables'].append({
                    "title": title,
                    "rows": data[:50]
                })

        safe_globals['show_chart'] = show_chart
        safe_globals['show_table'] = show_table
        safe_globals['plt'] = plt

    except ImportError:
        traceback.print_exc()

    # Security: reject code with __dunder__ attribute access (bypasses builtin restrictions)
    stripped_code = _strip_docstrings(code)
    if _code_has_dunder_access(stripped_code):
        return {
            "success": False,
            "error": "Code contains restricted attribute access (__dunder__ patterns). Only user-defined variables and standard library calls are allowed.",
            "output": "",
        }

    # Output capture
    output = io.StringIO()

    try:
        with contextlib.redirect_stdout(output):
            exec(stripped_code, safe_globals)

        output_text = output.getvalue()
        charts = safe_globals.get('_charts', [])
        tables = safe_globals.get('_tables', [])

        # Capture new session state (JSON-safe only)
        new_session = _capture_json_safe_state(safe_globals)

        # Capture backtest_result for BarByBacktestEngine runs
        _raw_result = safe_globals.get('backtest_result')
        backtest_result = _sanitize(_raw_result) if _raw_result is not None else None

        # Data preview
        data_preview = None
        if df is not None and len(df) > 0:
            try:
                data_preview = f"DataFrame shape: {df.shape}\nLast 5 rows:\n{df.tail(5).to_string()}"
            except Exception:
                traceback.print_exc()

        # Auto-chart if none created
        if not charts and df is not None and len(df) > 0:
            try:
                has_time = 'time' in df.columns or 'timestamp' in df.columns
                time_col = 'time' if 'time' in df.columns else ('timestamp' if 'timestamp' in df.columns else None)
                if 'close' in df.columns:
                    if has_time and time_col:
                        time_data = pd.to_numeric(df[time_col].tail(100), errors='coerce').fillna(0).astype(int).tolist()
                        close_data = df['close'].tail(100).tolist()
                        charts.append({
                            "title": "Close Price", "color": "#22c55e", "type": "line",
                            "data": {"time": time_data, "value": close_data}
                        })
                    else:
                        charts.append({
                            "title": "Close Price", "color": "#22c55e", "type": "line",
                            "data": df['close'].tail(50).tolist()
                        })
            except Exception:
                traceback.print_exc()

        return {
            "success": True,
            "output": output_text if output_text else "",
            "data_preview": data_preview,
            "charts": _sanitize(charts) if charts else None,
            "tables": _sanitize(tables) if tables else None,
            "modified_data": _sanitize(df.to_dict('records')) if df is not None else None,
            "session_state": new_session,
            "backtest_result": backtest_result,
        }

    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}"
        return {
            "success": False,
            "error": error_msg,
            "output": output.getvalue() if output.getvalue() else "",
            "session_state": {},
            "backtest_result": None,
        }


# ── Subprocess Worker Path ─────────────────────────────────────────────────
def _get_worker_path() -> str:
    """Return absolute path to sandbox_worker.py."""
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "sandbox_worker.py")


async def run_python_code(
    code: str,
    market_data: Optional[List[Dict[str, Any]]] = None,
    symbol: Optional[str] = None,
    session_id: Optional[str] = None,
    inject_df: Optional[Any] = None,
    user_id: int = 0,
):
    """Execute Python code safely with market data.

    When user_id > 0, execution runs in an isolated subprocess.
    Session state is keyed by user_id:symbol to prevent cross-user data leaks.
    """
    # ── Session key (user-isolated) ─────────────────────────────────────────
    if session_id:
        sess_key = session_id
    else:
        sess_key = _session_key(symbol, user_id)

    # ── Load previous session state ─────────────────────────────────────────
    _prune_expired()
    if sess_key not in _sandbox_state:
        _sandbox_state[sess_key] = {"_ts": time.monotonic()}
    else:
        _sandbox_state[sess_key]["_ts"] = time.monotonic()
    session = _sandbox_state[sess_key]
    session_state = {k: v for k, v in session.items() if k != "_ts"}

    # ── Convert inject_df to market_data for serialization ──────────────────
    md = market_data
    if inject_df is not None and md is None:
        try:
            df_for_records = inject_df
            if hasattr(df_for_records, 'index') and type(df_for_records.index).__name__ == 'DatetimeIndex':
                df_for_records = df_for_records.reset_index()
            records = df_for_records.to_dict('records') if hasattr(df_for_records, 'to_dict') else df_for_records
            md = _sanitize(records) if isinstance(records, list) else records
        except Exception:
            md = None

    # ── Auto-fetch more candles if data is insufficient ──────────────────────
    if md is not None and len(md) < 100 and symbol:
        try:
            from ..core.mt5_service import fetch_latest_candles
            more = await fetch_latest_candles(symbol, count=10000)
            if more and len(more) > len(md):
                logger = logging.getLogger(__name__)
                logger.info(f"[AutoFetch] {symbol}: {len(md)} → {len(more)} candles fetched from MT5")
                md = more
        except Exception:
            pass

    # ── Decide execution mode ───────────────────────────────────────────────
    use_subprocess = user_id > 0

    if use_subprocess:
        worker_path = _get_worker_path()
        request_data = {
            "code": code,
            "market_data": md,
            "symbol": symbol,
            "session_state": session_state,
        }

        try:
            proc = subprocess.run(
                [sys.executable, worker_path],
                input=json.dumps(request_data),
                capture_output=True,
                text=True,
                timeout=60,
                encoding='utf-8',
            )
            if proc.returncode != 0:
                stderr = proc.stderr or ""
                return {
                    "success": False,
                    "error": f"Sandbox worker crashed (exit {proc.returncode}): {stderr[:500]}",
                    "output": "",
                }
            result = json.loads(proc.stdout)
        except subprocess.TimeoutExpired:
            result = {
                "success": False,
                "error": "Execution timed out (25s limit). Simplify your code or reduce loop iterations.",
                "output": "",
            }
        except json.JSONDecodeError as e:
            result = {
                "success": False,
                "error": f"Sandbox response parse error: {e}",
                "output": proc.stdout[:500] if proc.stdout else "",
            }
        except Exception as e:
            result = {
                "success": False,
                "error": f"Subprocess error: {str(e)}",
                "output": "",
            }
    else:
        # Inline mode (same process) — backward compat for anonymous calls
        result = _execute_sandbox_sync(code, md, symbol, session_state)

    # ── Update session state ────────────────────────────────────────────────
    new_state = result.get("session_state", {})
    if isinstance(new_state, dict):
        new_state["_ts"] = time.monotonic()
        _sandbox_state[sess_key] = new_state

    # ── Add session_id to response ──────────────────────────────────────────
    result["session_id"] = sess_key
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Calculate Indicator Endpoint (unchanged)
# ═══════════════════════════════════════════════════════════════════════════

class CalculateIndicatorRequest(BaseModel):
    indicator: str
    period: int
    market_data: List[Dict[str, Any]]


@router.post("/calculate-indicator")
async def calculate_indicator(request: CalculateIndicatorRequest, current_user: dict = Depends(get_current_user)):
    try:
        import pandas as pd
        
        # Guard: Ensure enough data
        if len(request.market_data) < request.period + 5:
            return {
                "success": False, 
                "error": f"Insufficient data: period is {request.period} but provided data has only {len(request.market_data)} rows. Please provide at least {request.period + 10} candles."
            }

        df = pd.DataFrame(request.market_data)

        if request.indicator.upper() == 'ATR':
            df['tr'] = df.apply(
                lambda row: max(
                    row['high'] - row['low'],
                    abs(row['high'] - df.shift(1).close[row.name]) if row.name > 0 else row['high'] - row['low'],
                    abs(row['low'] - df.shift(1).close[row.name]) if row.name > 0 else row['high'] - row['low']
                ), axis=1
            )
            df['atr'] = df['tr'].rolling(window=request.period).mean()
            current_atr = round(df['atr'].iloc[-1], 2) if not pd.isna(df['atr'].iloc[-1]) else None
            return {"success": True, "indicator": "ATR", "period": request.period, "current_value": current_atr, "values": df['atr'].dropna().tolist()[-20:]}

        elif request.indicator.upper() == 'SMA':
            df['sma'] = df['close'].rolling(window=request.period).mean()
            current_sma = round(df['sma'].iloc[-1], 2) if not pd.isna(df['sma'].iloc[-1]) else None
            return {"success": True, "indicator": "SMA", "period": request.period, "current_value": current_sma, "values": df['sma'].dropna().tolist()[-20:]}

        elif request.indicator.upper() == 'EMA':
            df['ema'] = df['close'].ewm(span=request.period).mean()
            current_ema = round(df['ema'].iloc[-1], 2) if not pd.isna(df['ema'].iloc[-1]) else None
            return {"success": True, "indicator": "EMA", "period": request.period, "current_value": current_ema, "values": df['ema'].dropna().tolist()[-20:]}

        elif request.indicator.upper() == 'RSI':
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=request.period).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=request.period).mean()
            rs = gain / loss
            df['rsi'] = 100 - (100 / (1 + rs))
            current_rsi = round(df['rsi'].iloc[-1], 2) if not pd.isna(df['rsi'].iloc[-1]) else None
            return {"success": True, "indicator": "RSI", "period": request.period, "current_value": current_rsi, "values": df['rsi'].dropna().tolist()[-20:]}

        else:
            return {"success": False, "error": f"Unsupported indicator: {request.indicator}"}

    except Exception as e:
        return {"success": False, "error": str(e)}
