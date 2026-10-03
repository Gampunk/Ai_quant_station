import os
import pandas as pd
import ta
import numpy as np
import re
import asyncio
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from sqlalchemy import select, update
from openai import AsyncOpenAI
from pathlib import Path
import logging
import traceback

from ..core.config import settings
from ..core.security import get_current_user, require_trader
from ..core.database import AsyncSessionLocal
from ..core.providers import PROVIDERS, get_api_key as _get_api_key, get_base_url
from ..core.historical_loader import add_indicators, to_pandas_freq
from ..core.utils import get_robust_code_gen_prompt
from ..models.ai_memory import UserPrompt, DefaultPromptStrategy
from ..models.historical_lab import HistoricalBacktest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/backtest", tags=["Backtest"])


def _validate_backtest_result(bt: dict) -> dict:
    """Ensure backtest_result has all required keys with safe defaults."""
    bt = bt or {}
    metrics = bt.get("metrics") or {}
    bt["metrics"] = {
        "total_return_pct": float(metrics.get("total_return_pct") or 0),
        "total_pnl": float(metrics.get("total_pnl") or 0),
        "sharpe_ratio": float(metrics.get("sharpe_ratio") or 0),
        "max_drawdown_pct": float(metrics.get("max_drawdown_pct") or 0),
        "win_rate_pct": float(metrics.get("win_rate_pct") or 0),
        "profit_factor": float(metrics.get("profit_factor") or 0),
        "num_trades": int(metrics.get("num_trades") or 0),
        "final_equity": float(metrics.get("final_equity") or 0),
        "lot_size": float(metrics.get("lot_size") or 0),
    }
    eq = bt.get("equity_curve") or []
    bt["equity_curve"] = [
        {"time": str(e.get("time", "")), "balance": float(e.get("balance") or 0)}
        for e in eq if isinstance(e, dict)
    ]
    tl = bt.get("trade_log") or []
    bt["trade_log"] = [
        {
            "entry_time": str(t.get("entry_time", "")),
            "exit_time": str(t.get("exit_time", "")),
            "direction": str(t.get("direction", "")),
            "entry_price": float(t.get("entry_price") or 0),
            "exit_price": float(t.get("exit_price") or 0),
            "pnl": float(t.get("pnl") or 0),
        }
        for t in tl if isinstance(t, dict)
    ]
    return bt

PARQUET_DIR = Path(__file__).parent.parent.parent.parent / "data_archive" / "parquet_storage"
PROMPT_FILE = str(Path(__file__).resolve().parent.parent.parent.parent / "backend" / "prompt_list.txt")


def _capture_raw_response(response) -> dict | None:
    try:
        return response.model_dump(mode='json')
    except Exception:  # swallow-ok: older SDK objects; .dict() is tried next
        try:
            return response.dict()
        except Exception:  # swallow-ok: the raw copy is only kept for debugging
            return None

CONTRACT_MULTIPLIERS = {
    'XAUUSD': 100, 'XAGUSD': 5000, 'EURUSD': 100000, 'GBPUSD': 100000,
    'USDJPY': 100000, 'USDCAD': 100000, 'AUDUSD': 100000, 'NZDUSD': 100000,
    'GBPJPY': 100000, 'EURJPY': 100000, 'BTCUSD': 1, 'ETHUSD': 1,
    'US30': 10, 'SPX500': 50, 'NAS100': 20, 'DAX40': 25,
    'UK100': 10, 'JP225': 10,
}

def _tf_to_minutes(tf: str) -> int:
    if tf.endswith('T'):
        return int(tf[:-1])
    elif tf.endswith('H'):
        return int(tf[:-1]) * 60
    elif tf.endswith('D'):
        return int(tf[:-1]) * 1440
    return 1


class BacktestRequest(BaseModel):
    prompt_id: str
    symbol: str
    timeframe: str = "1T"
    start_date: str = "2024-01-01"
    end_date: str = "2024-12-31"
    provider: Optional[str] = "nvidia"
    model: Optional[str] = "qwen/qwen3.5-122b-a10b"
    lot_size: Optional[float] = 0.01
    initial_capital: Optional[float] = 10000.0
    spread: Optional[float] = 0.0
    commission: Optional[float] = 0.0
    strategy_code: Optional[str] = None

class BacktestResponse(BaseModel):
    success: bool
    metrics: Optional[Dict[str, Any]] = None
    equity_curve: Optional[List[float]] = None
    error: Optional[str] = None
    generated_code: Optional[str] = None
    trades: Optional[List[Dict[str, Any]]] = None

async def generate_strategy_code(prompt_text: str, provider: str = "nvidia", model: str = "qwen/qwen3.5-122b-a10b", error_msg: Optional[str] = None):
    api_key = _get_api_key(provider, settings)
    if not api_key:
        raise Exception(f"AI API Key for {provider} not configured")
    base_url = get_base_url(provider)

    client = AsyncOpenAI(
        base_url=base_url,
        api_key=api_key
    )

    system_prompt = get_robust_code_gen_prompt(base_instructions="""
RULES:
1. The DataFrame also has indicator columns from higher timeframes with _1h, _4h, _1d suffixes, e.g.: rsi_14_1h, ema_9_4h, sma_50_1d. Reference them as: df['rsi_14_1h'], df['ema_9_4h'], etc.
2. Use 'ta' library for technical indicators (e.g., ta.momentum.rsi, ta.trend.sma_indicator, ta.trend.ema_indicator).
3. Be precise with logic. If multiple conditions are mentioned, all must be met.
4. CRITICAL: You MUST create `signal = pd.Series(0, index=df.index)` and you MUST `return signal` at the end.
""")
    
    user_content = f"Convert this strategy: {prompt_text}"
    if error_msg:
        user_content = f"Your previous code for this strategy: '{prompt_text}' failed with this error: {error_msg}. Please fix the code and return ONLY the corrected 'calculate_signals(df)' function."

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        temperature=0.1,
        max_tokens=8192,
        timeout=120
    )

    full_raw_response = _capture_raw_response(response)
    code_content = response.choices[0].message.content or ""
    code = code_content.strip()
    match = re.search(r"```python\s*(.*?)\s*```", code, re.S)
    if match:
        code = match.group(1).strip()
    elif "```python" in code:
        code = code.split("```python", 1)[1].strip()
        code = code.strip("`").strip()
    return {"code": code, "raw_thinking": full_raw_response}

def _map_freeform_metrics(ff_metrics: dict) -> dict:
    """Convert freeform backtest_result metrics to backtest.py format."""
    return {
        "total_return": ff_metrics.get("total_return_pct", 0),
        "total_pnl": ff_metrics.get("total_pnl", 0),
        "final_equity": ff_metrics.get("final_equity", 0),
        "win_rate": ff_metrics.get("win_rate_pct", 0),
        "max_drawdown": ff_metrics.get("max_drawdown_pct", 0),
        "trades": ff_metrics.get("num_trades", 0),
    }


def _map_freeform_trades(ff_trades: list) -> list:
    """Convert freeform trade_log to backtest.py format."""
    mapped = []
    for t in (ff_trades or []):
        entry_p = t.get("entry_price", 0)
        exit_p = t.get("exit_price", 0)
        pnl = t.get("pnl", 0)
        pnl_pct = (pnl / entry_p * 100) if entry_p else 0
        mapped.append({
            "entry_time": t.get("entry_time", ""),
            "exit_time": t.get("exit_time", ""),
            "direction": t.get("direction", ""),
            "entry_price": entry_p,
            "exit_price": exit_p,
            "pnl_dollars": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "holding_period": 0,
        })
    return mapped


def _build_freeform_prompt_backtest(df, prompt_text, symbol, initial_capital=10000.0, lot_size=0.01, contract_multiplier=100.0) -> str:
    base_cols = ['open', 'high', 'low', 'close', 'volume']
    indicator_cols = sorted([c for c in df.columns if c not in base_cols and c != 'timestamp' and c != 'datetime'])
    indicator_str = ", ".join(indicator_cols[:50])
    return f"""You are a trading strategy developer. Write Python code that simulates trades on `df`.

AVAILABLE COLUMNS: {indicator_str}

The df is a pandas DataFrame with DatetimeIndex. Each row is one bar.

PARAMETERS (already defined as Python variables):
  initial_capital = {initial_capital}
  lot_size = {lot_size}
  contract_multiplier = {contract_multiplier}
  spread_cost_per_lot   # spread in points per lot, subtract from trade P&L
  commission_per_lot    # fixed commission per lot per trade, subtract from trade P&L

Write ANY Python code you want. Use loops, state variables, SL/TP checks, time filters, whatever the strategy needs.
At the end, store results in `backtest_result` which is a dict with this exact shape:

backtest_result = {{
    "equity_curve": [{{"time": "...", "balance": 10000.0}}, ...],
    "metrics": {{
        "total_return_pct": 12.34,
        "total_pnl": 1234.56,
        "sharpe_ratio": 1.23,
        "max_drawdown_pct": -5.67,
        "win_rate_pct": 55.5,
        "profit_factor": 1.5,
        "num_trades": 42,
        "final_equity": 11234.56,
        "lot_size": {lot_size},
    }},
    "trade_log": [{{"entry_time": "...", "exit_time": "...", "direction": "BUY", "entry_price": 2000.0, "exit_price": 2050.0, "pnl": 50.0}}, ...],
}}

PnL per trade: lot_size * contract_multiplier * (exit - entry) * direction_sign
Use df.index[i] for time access. Use row.get('col', default) for NaN-safe access.
Output ONLY ```python ... ``` with no text outside.

Strategy: {prompt_text}"""


async def _run_freeform_backtest_async(df, prompt_text, symbol,
                                        lot_size, contract_multiplier,
                                        initial_capital, spread, commission,
                                        provider, model, user_id=0) -> dict:
    """AI writes arbitrary Python that simulates trades and produces backtest_result."""
    system_prompt = _build_freeform_prompt_backtest(
        df, prompt_text, symbol, initial_capital, lot_size, contract_multiplier
    )
    api_key = _get_api_key(provider, settings)
    base_url = get_base_url(provider)
    if not api_key:
        return {"error": f"API key for {provider} not configured"}
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Write backtest simulation for {symbol}."}
    ]
    last_error = None

    for attempt in range(3):
        try:
            response = await client.chat.completions.create(
                model=model, messages=messages,
                temperature=0.05 if attempt == 0 else 0.2, timeout=60,
            )
            raw = response.choices[0].message.content or ""
            pm = re.search(r"```python\s*(.*?)(?:```|$)", raw, re.S)
            if not pm:
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": "Output ONLY ```python ... ```."})
                continue
            code = pm.group(1).strip()
            # Inject parameters as actual Python variables before AI code
            params = f"""
initial_capital = {initial_capital}
lot_size = {lot_size}
contract_multiplier = {contract_multiplier}
spread_cost_per_lot = {spread}
commission_per_lot = {commission}
"""
            full_code = params + "\n" + code
            from .execute import run_python_code
            res = await run_python_code(full_code, symbol=symbol, inject_df=df.copy(), user_id=user_id)
            bt = res.get("backtest_result")
            if not bt:
                bt = res.get("session_state", {}).get("backtest_result")
            if bt and "metrics" in bt:
                bt = _validate_backtest_result(bt)
                # Sample equity curve server-side to cap at 500 points
                eq_raw = bt.get("equity_curve", [])
                if len(eq_raw) > 500:
                    step = max(1, len(eq_raw) // 500)
                    eq_raw = eq_raw[::step]
                eq_values = [e["balance"] for e in eq_raw]
                return {
                    "metrics": _map_freeform_metrics(bt["metrics"]),
                    "equity_curve": eq_values,
                    "trades": _map_freeform_trades(bt.get("trade_log", [])),
                    "generated_code": code,
                }
            error_detail = res.get("error") or "Unknown error"
            last_error = error_detail
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": f"Failed: {error_detail[:500]}. Fix and output ONLY valid Python."})
        except Exception as e:
            logger.warning("[Backtest] Strategy generation attempt failed", exc_info=True)
            last_error = str(e)
            await asyncio.sleep(1)
    return {"error": "AI service temporarily unavailable. Please check your API key configuration and try again."
                     + (f" Last error: {last_error[:300]}" if last_error else "")}


def run_vectorized_backtest(df, strategy_code, lot_size=0.01, contract_multiplier=100, initial_capital=10000.0, spread=0.0, commission=0.0):
    """Execute code and calculate PnL."""
    try:
        # 1. Execute strategy code to define function - RESTRICTED builtins for security
        import builtins as _real_builtins
        _SAFE_IMPORT_MODULES = {'pandas', 'numpy', 'ta', 'scipy', 'sklearn', 'math', 'json', 'random', 'itertools', 'collections', 'decimal', 'warnings'}
        def _safe_import(name, *args, **kwargs):
            base = name.split('.')[0]
            if base not in _SAFE_IMPORT_MODULES:
                raise ImportError(f"Module '{name}' is not allowed")
            return _real_builtins.__import__(name, *args, **kwargs)

        safe_builtins = {
            'abs': abs, 'all': all, 'any': any, 'bool': bool, 'True': True,
            'False': False, 'None': None, 'dict': dict, 'enumerate': enumerate,
            'float': float, 'int': int, 'isinstance': isinstance, 'len': len,
            'list': list, 'max': max, 'min': min, 'range': range,
            'round': round, 'sorted': sorted, 'str': str, 'sum': sum,
            'tuple': tuple, 'type': type, 'zip': zip,
            'Exception': Exception, 'ValueError': ValueError,
            'TypeError': TypeError, 'KeyError': KeyError,
            'IndexError': IndexError, 'ZeroDivisionError': ZeroDivisionError,
            '__import__': _safe_import,
        }
        _extra_libs = {}
        try:
            import scipy
            import sklearn
            _extra_libs["scipy"] = scipy
            _extra_libs["sklearn"] = sklearn
        except Exception:
            logger.warning("[Backtest] Optional libraries unavailable", exc_info=True)
        exec_globals = {"__builtins__": safe_builtins, "pd": pd, "np": np, "ta": ta, **_extra_libs}
        exec(strategy_code, exec_globals)
        calculate_signals = exec_globals.get('calculate_signals')
        
        if not calculate_signals:
            return {"error": "Function calculate_signals not found in generated code"}

        if not callable(calculate_signals):
            return {"error": f"calculate_signals is not callable (got {type(calculate_signals).__name__})"}

        # 2. Get signals
        try:
            signal = calculate_signals(df.copy())
        except Exception:  # swallow-ok: AI-written code failed; the traceback goes back to the AI to fix
            return {"error": f"calculate_signals() raised on execution:\n{traceback.format_exc()}"}

        if not isinstance(signal, pd.Series):
            return {"error": f"calculate_signals() must return a pandas Series, got {type(signal).__name__}"}

        df = df.copy()
        df['signal'] = signal

        # 3. Extract individual trades from signal transitions
        trades = []
        entry_mask = (df['signal'] != 0) & (df['signal'].shift(1).fillna(0) == 0)
        exit_mask = (df['signal'] == 0) & (df['signal'].shift(1).fillna(0) != 0)

        entry_indices = df.index[entry_mask].tolist()
        exit_indices = df.index[exit_mask].tolist()

        # Handle case where signal ends non-zero (still in position at last bar)
        if len(entry_indices) > len(exit_indices):
            exit_indices.append(df.index[-1])

        for entry_idx, exit_idx in zip(entry_indices, exit_indices):
            entry_signal = int(df.loc[entry_idx, 'signal'])
            direction = 'BUY' if entry_signal == 1 else 'SELL'

            entry_row = df.loc[entry_idx]
            exit_row = df.loc[exit_idx]

            entry_price = float(entry_row['close'])
            exit_price = float(exit_row['close'])
            entry_time = str(entry_row['datetime']) if 'datetime' in df.columns else ''
            exit_time = str(exit_row['datetime']) if 'datetime' in df.columns else ''

            if direction == 'BUY':
                pnl_points = exit_price - entry_price - spread
            else:
                pnl_points = entry_price - exit_price - spread

            pnl_pct = (pnl_points / entry_price) * 100 if entry_price else 0
            pnl_dollars = (pnl_points * contract_multiplier * lot_size) - commission
            holding_period = exit_idx - entry_idx

            trades.append({
                'entry_time': entry_time,
                'exit_time': exit_time,
                'direction': direction,
                'entry_price': round(entry_price, 5),
                'exit_price': round(exit_price, 5),
                'pnl_points': round(pnl_points, 2),
                'pnl_pct': round(pnl_pct, 2),
                'pnl_dollars': round(pnl_dollars, 2),
                'holding_period': holding_period,
                'entry_idx': entry_idx,
                'exit_idx': exit_idx,
            })

        # 4. Build equity curve from actual trade P&Ls
        trades_by_exit: dict[int, list] = {}
        for t in trades:
            trades_by_exit.setdefault(t['exit_idx'], []).append(t)

        equity_curve_bar = [float(initial_capital)] * len(df)
        current_equity = float(initial_capital)
        for i in range(len(df)):
            if i in trades_by_exit:
                for t in trades_by_exit[i]:
                    current_equity += t['pnl_dollars']
            equity_curve_bar[i] = current_equity

        # 5. Metrics
        total_pnl = round(current_equity - initial_capital, 2)
        total_return = round((current_equity / initial_capital - 1) * 100, 2)
        final_equity = round(current_equity, 2)
        winning_trades = sum(1 for t in trades if t['pnl_pct'] > 0)
        total_trades = len(trades)
        win_rate = round((winning_trades / total_trades * 100), 2) if total_trades > 0 else 0.0

        equity_series = pd.Series(equity_curve_bar)
        running_max = equity_series.cummax()
        dd_pct = ((equity_series / running_max - 1) * 100).min()
        dd_dollars = round((running_max - equity_series).max(), 2)

        step = max(1, len(equity_curve_bar) // 100)
        sampled_curve = equity_curve_bar[::step]

        return {
            "metrics": {
                "total_return": total_return,
                "total_pnl": total_pnl,
                "final_equity": final_equity,
                "win_rate": win_rate,
                "max_drawdown": round(dd_pct, 2),
                "max_dd_dollars": dd_dollars,
                "trades": total_trades,
            },
            "equity_curve": sampled_curve,
            "trades": trades,
        }
    except Exception as e:
        logger.exception("[Backtest] Backtest run failed")
        return {"error": str(e)}

@router.post("/run", response_model=BacktestResponse)
async def run_backtest(request: BacktestRequest, current_user: dict = Depends(require_trader)):
    user_id = current_user["id"]
    
    # 1. Get Prompt Text and Strategy Code (Cache)
    strategy_code = request.strategy_code  # Allow direct code injection for testing
    prompt_text = ""
    
    async with AsyncSessionLocal() as db:
        if request.prompt_id.startswith("custom_"):
            db_id = int(request.prompt_id.split("_")[1])
            result = await db.execute(select(UserPrompt).where(UserPrompt.id == db_id, UserPrompt.user_id == user_id))
            prompt_obj = result.scalar_one_or_none()
            if not prompt_obj:
                raise HTTPException(status_code=404, detail="Prompt not found")
            prompt_text = prompt_obj.content
            strategy_code = prompt_obj.strategy_code
        else:
            # Default prompts
            p_num = int(request.prompt_id)
            # Try to get prompt text from file (handles both old "N. text" and new "PROMPT #N:" formats)
            try:
                with open(PROMPT_FILE, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                current_num = None
                current_lines = []
                for raw_line in lines:
                    line = raw_line.strip()
                    if not line:
                        continue
                    new_match = re.match(r"^PROMPT\s*#(\d+):?\s*$", line, re.I)
                    if new_match:
                        if current_num == p_num and current_lines:
                            prompt_text = " ".join(current_lines).strip()
                            break
                        current_num = int(new_match.group(1))
                        current_lines = []
                        continue
                    old_match = re.match(r"^(\d+)\.\s*(.*)", line)
                    if old_match and current_num is None:
                        if int(old_match.group(1)) == p_num:
                            prompt_text = old_match.group(2).strip()
                            break
                        current_num = int(old_match.group(1))
                        current_lines = [old_match.group(2)]
                        continue
                    if current_num == p_num:
                        cleaned = re.sub(r'\s+', ' ', line).strip()
                        if cleaned:
                            current_lines.append(cleaned)
                if current_num == p_num and current_lines and not prompt_text:
                    prompt_text = " ".join(current_lines).strip()
            except Exception:
                logger.warning("[Backtest] Could not read prompt #%s from the prompt list", p_num, exc_info=True)
            
            # Check default strategy cache
            result = await db.execute(select(DefaultPromptStrategy).where(DefaultPromptStrategy.prompt_number == p_num))
            strategy_obj = result.scalar_one_or_none()
            if strategy_obj:
                strategy_code = strategy_obj.strategy_code

    raw_thinking = None
    if not strategy_code:
        try:
            gen_result = await generate_strategy_code(
                prompt_text, 
                provider=request.provider, 
                model=request.model
            )
            strategy_code = gen_result["code"]
            raw_thinking = gen_result["raw_thinking"]
            # Save to cache
            async with AsyncSessionLocal() as db:
                if request.prompt_id.startswith("custom_"):
                    db_id = int(request.prompt_id.split("_")[1])
                    await db.execute(update(UserPrompt).where(UserPrompt.id == db_id).values(strategy_code=strategy_code))
                else:
                    new_cache = DefaultPromptStrategy(prompt_number=int(request.prompt_id), strategy_code=strategy_code)
                    db.add(new_cache)
                await db.commit()
        except Exception as e:
            logger.exception("[Backtest] Strategy generation failed")
            return BacktestResponse(success=False, error=f"AI Generation failed: {str(e)}")

    # 3. Load Market Data
    start_dt = datetime.strptime(request.start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(request.end_date, "%Y-%m-%d")

    df_list = []
    for year in range(start_dt.year, end_dt.year + 1):
        file_path = PARQUET_DIR / f"{request.symbol}_{year}.parquet"
        if file_path.exists():
            df_list.append(pd.read_parquet(file_path))
    
    if not df_list:
        return BacktestResponse(success=False, error=f"No historical data found for {request.symbol} in range {request.start_date} to {request.end_date}")

    full_df = pd.concat(df_list).sort_values('timestamp')
    full_df['datetime'] = pd.to_datetime(full_df['timestamp'], unit='s')

    # Filter by date range
    full_df = full_df[(full_df['datetime'] >= start_dt) & (full_df['datetime'] <= end_dt.replace(hour=23, minute=59, second=59))]
    if full_df.empty:
        return BacktestResponse(success=False, error=f"No data for {request.symbol} between {request.start_date} and {request.end_date}")

    # Resample if needed (Parquet is M1)
    m1_df = full_df.copy()
    if request.timeframe != "1T":
        full_df.set_index('datetime', inplace=True)
        resampled = full_df.resample(to_pandas_freq(request.timeframe)).agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna()
        full_df = resampled.reset_index()  # keep datetime as a column

    # Multi-TF: merge higher-TF indicator columns into primary df
    HIGHER_TF_CONFIG = {
        '1H': ('_1h', 60),
        '4H': ('_4h', 240),
        '1D': ('_1d', 1440),
    }
    primary_minutes = _tf_to_minutes(request.timeframe)
    tfs_to_merge = {alias: (suffix, mins) for alias, (suffix, mins) in HIGHER_TF_CONFIG.items() if mins > primary_minutes}
    if tfs_to_merge:
        primary_idx = pd.DatetimeIndex(full_df['datetime'])
        df_primary = add_indicators(full_df.set_index('datetime'))
        for alias, (suffix, mins) in tfs_to_merge.items():
            df_higher = m1_df.set_index('datetime').resample(to_pandas_freq(alias)).agg({
                'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
            }).dropna()
            df_higher = add_indicators(df_higher)
            indicator_cols = [c for c in df_higher.columns if c not in ('open', 'high', 'low', 'close', 'volume', 'timestamp')]
            for col in indicator_cols:
                df_primary[f"{col}{suffix}"] = df_higher[col].reindex(primary_idx, method='ffill').bfill().fillna(0)
        full_df = df_primary.reset_index()

    # 4. Run Backtest
    lot_size = request.lot_size or 0.01
    contract_multiplier = CONTRACT_MULTIPLIERS.get(request.symbol, 100)
    initial_capital = request.initial_capital or 10000.0

    # ── Try freeform: AI writes arbitrary Python that produces backtest_result ──
    logger.info(f"[Backtest] Trying freeform AI for {request.symbol}")
    result = await _run_freeform_backtest_async(
        full_df, prompt_text, request.symbol,
        lot_size=lot_size, contract_multiplier=contract_multiplier,
        initial_capital=initial_capital,
        spread=request.spread or 0.0, commission=request.commission or 0.0,
        provider=request.provider, model=request.model,
        user_id=user_id,
    )

    if "error" in result:
        # ── Fallback: vectorized signal-based engine ──
        logger.warning(f"[Backtest] Freeform failed: {result['error']}, using vectorized")
        max_retries = 3
        last_error = None
        raw_thinking = None
        loop = asyncio.get_event_loop()
        result = {"error": "No attempt made"}

        for attempt in range(max_retries):
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, run_vectorized_backtest, full_df, strategy_code,
                                         lot_size, contract_multiplier, initial_capital,
                                         request.spread or 0.0, request.commission or 0.0),
                    timeout=60.0
                )
            except asyncio.TimeoutError:
                result = {"error": "Code execution timed out (60s limit). Simplify the strategy logic."}

            if "error" not in result:
                break

            last_error = result["error"]
            logger.warning(f"[Backtest] Vectorized attempt {attempt+1} failed: {last_error}. Retrying...")

            hint = ""
            if "IndexError" in last_error:
                hint = " (HINT: Ensure you don't slice the DataFrame too small before calculating indicators like ATR, RSI, or SMA. Technical indicators need a sufficient historical window.)"
            elif "NaN" in last_error:
                hint = " (HINT: Use .dropna() after calculating indicators to avoid NaN issues in signals.)"

            try:
                gen_result = await generate_strategy_code(
                    prompt_text, provider=request.provider, model=request.model,
                    error_msg=f"{last_error}{hint}"
                )
                strategy_code = gen_result["code"]
                raw_thinking = gen_result["raw_thinking"]
                async with AsyncSessionLocal() as db:
                    if request.prompt_id.startswith("custom_"):
                        db_id = int(request.prompt_id.split("_")[1])
                        await db.execute(update(UserPrompt).where(UserPrompt.id == db_id).values(strategy_code=strategy_code))
                    else:
                        await db.execute(update(DefaultPromptStrategy).where(DefaultPromptStrategy.prompt_number == int(request.prompt_id)).values(strategy_code=strategy_code))
                    await db.commit()
            except Exception:
                logging.getLogger("backtest").error("[Backtest] AI retry generation failed", exc_info=True)
                break

    if "error" in result:
        return BacktestResponse(success=False, error=result["error"], generated_code=strategy_code)

    # 5. Save Backtest Record
    async with AsyncSessionLocal() as db:
        backtest_rec = HistoricalBacktest(
            user_id=user_id,
            symbol=request.symbol,
            start_date=start_dt.replace(tzinfo=timezone.utc),
            end_date=end_dt.replace(tzinfo=timezone.utc),
            timeframe=request.timeframe,
            mode="backtest",
            prompt=prompt_text,
            status="completed",
            metrics=result["metrics"],
            equity_curve=result["equity_curve"],
            generated_code=strategy_code,
            raw_thinking=raw_thinking if not result.get("generated_code") else None
        )
        db.add(backtest_rec)
        await db.commit()

    return BacktestResponse(
        success=True,
        metrics=result["metrics"],
        equity_curve=result.get("equity_curve"),
        trades=result.get("trades"),
        generated_code=result.get("generated_code") or strategy_code
    )
