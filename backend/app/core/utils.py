import json
import re
import math
from typing import Optional, Any, List, Dict

def sanitize_for_json(obj: Any) -> Any:
    """
    Recursively cleans objects for JSON serialization safety.
    Handles Datetime, Pandas, and NumPy types.
    """
    from datetime import date, time, datetime

    if isinstance(obj, (datetime,)):
        return obj.isoformat()
    if isinstance(obj, (date, time)):
        return str(obj)

    try:
        import pandas as _pd
        if isinstance(obj, _pd.Timestamp):
            return obj.isoformat()
        if isinstance(obj, _pd.Timedelta):
            return str(obj)
        try:
            if _pd.isna(obj):
                return None
        except (TypeError, ValueError):
            pass
    except ImportError:
        pass

    try:
        import numpy as _np
        if isinstance(obj, (_np.integer,)):
            return int(obj)
        if isinstance(obj, (_np.floating,)):
            v = float(obj)
            return None if math.isnan(v) or math.isinf(v) else v
        if isinstance(obj, _np.bool_):
            return bool(obj)
        if isinstance(obj, _np.ndarray):
            return sanitize_for_json(obj.tolist())
    except ImportError:
        pass

    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_for_json(i) for i in obj]
    if hasattr(obj, 'item') and callable(obj.item):
        try:
            return sanitize_for_json(obj.item())
        except Exception:
            pass
    return obj

def detect_trade_setup(text: str) -> Optional[dict]:
    """Detect TRADE_SETUP JSON from AI response."""
    # Pattern to match ```json { ... } ```
    json_pattern = r"```json\s*(.*?)\s*```"
    blocks = re.findall(json_pattern, text, re.S | re.I)

    for block in blocks:
        try:
            data = json.loads(block)
            if isinstance(data, dict) and data.get("action") == "TRADE_SETUP":
                return data
        except json.JSONDecodeError:
            pass

    # Fallback: try to parse the entire text as JSON
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get("action") == "TRADE_SETUP":
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    return None

def get_robust_code_gen_prompt(base_instructions: str = "") -> str:
    """Returns the mandatory robust system prompt for AI code generation."""
    return f"""You are a Quantitative Developer. Convert the following natural language trading strategy into a Python function.

{base_instructions}

MANDATORY RULES:
1. Use the variable 'df' which is a pandas DataFrame.
2. The function must be named 'calculate_signals(df)'.
3. It must return a pandas Series named 'signal' where: 1 = Buy, -1 = Sell, 0 = No Signal.
4. PERFORMANCE: MUST use pandas vectorization. Absolutely NO explicit `for` loops for resampling or indicator calculation.
5. ROBUSTNESS: Wrap ALL logic in a `try...except` block.
6. ERROR HANDLING: If an error occurs, you MUST print a structured JSON error object to stdout: `print(json.dumps({{"status": "failed", "error": "description"}}))`.
7. SCHEMA: Ensure output adheres to expected formats.
8. PRECISION: Handle NaN values explicitly using .fillna() or .dropna().
9. SAFETY: Always check `if len(df) == 0:` before accessing `.iloc[-1]` or similar index operations to avoid IndexError on empty DataFrames. If the DataFrame is empty after filtering, return an empty signal series.
10. MEMORY: You MUST slice the DataFrame to the minimum necessary date range before any resampling or heavy calculations. If analyzing a large timeframe, work on a copy: `df_subset = df.loc[start:end].copy()`.

Output ONLY the code block enclosed in ```python ... ```, no explanations.
"""

# ─── Default SL/TP ──────────────────────────────────────────────────────────
# Orders that arrive without a stop-loss / take-profit (AI models often return
# 0.0 for both) get a default distance as a fraction of price so no trade is
# ever sent naked. Applied on all three execution paths: autopilot, Terminal,
# and AI-Analyst Execute-Trade. Existing broker min-distance rules run after
# this and may push the values further out.
DEFAULT_SL_TP_PCT = 0.002  # 0.2% of price


def apply_default_sl_tp(
    action: str,
    price: Optional[float],
    sl: Optional[float],
    tp: Optional[float],
    digits: Optional[int] = None,
    pct: float = DEFAULT_SL_TP_PCT,
):
    """Fill missing/zero SL or TP with a +/- pct distance around `price`.

    BUY:  SL below price, TP above price (mirrored for SELL and pending
    actions such as BUY_LIMIT / SELL_STOP). Each side is independent — only
    the missing one is filled. Returns (sl, tp); rounds to `digits` when
    given. Passes through unchanged when price is missing/invalid.
    """
    if not price or price <= 0:
        return sl, tp
    is_buy = "BUY" in (action or "").upper()
    if not sl or sl <= 0:
        sl = price * (1 - pct) if is_buy else price * (1 + pct)
    if not tp or tp <= 0:
        tp = price * (1 + pct) if is_buy else price * (1 - pct)
    if digits is not None:
        sl = round(sl, digits)
        tp = round(tp, digits)
    return sl, tp
