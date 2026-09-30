"""
How a trade ended, from its closing deal.

The broker says why a deal happened in its reason field, which the connector
reports as "sl", "tp", "stop_out", "expert" and so on. Seven places used to guess
instead, by searching the deal's comment for the letters "sl" or "tp", so any
comment containing them was misread.

A connector too old to report the reason gets a narrower fallback: MT5 writes
"[sl 2650.10]" or "[tp 2680.00]" as the whole comment when a stop or target fills.
"""
import re
from typing import Optional

_MT5_STOP_COMMENT = re.compile(r"^\s*\[(sl|tp)\s", re.IGNORECASE)
_BY_REASON = {"sl": "SL_HIT", "tp": "TP_HIT", "stop_out": "STOP_OUT"}


def close_result(deal: Optional[dict]) -> str:
    """SL_HIT, TP_HIT, STOP_OUT, PROFIT or LOSS; OPEN when there is no closing deal."""
    if not deal:
        return "OPEN"
    reason = (deal.get("reason") or "").lower()
    if reason and reason != "unknown":
        result = _BY_REASON.get(reason)
    else:
        match = _MT5_STOP_COMMENT.match(deal.get("comment") or "")
        result = _BY_REASON.get(match.group(1).lower()) if match else None
    if result:
        return result
    return "PROFIT" if (deal.get("profit") or 0) > 0 else "LOSS"
