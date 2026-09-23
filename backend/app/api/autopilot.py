"""
Autopilot API - Automatic trading based on AI analysis
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import asyncio
import random
import os
import json
import re
from sqlalchemy import select, func
from openai import AsyncOpenAI
import pandas as pd
import ta
import numpy as np

from ..core.config import settings
from ..core.security import get_current_user, require_trader
from ..core.database import AsyncSessionLocal
from ..core.providers import PROVIDERS, get_api_key as _get_api_key, get_base_url, resolve_api_key
from ..models.ai_memory import AutopilotTrade, AutopilotSettings, UserPrompt, AutopilotLog, ModelUsage, AiCallLog
from ..models.strategy_score import StrategyScore
from ..core.providers import estimate_cost
from ..core.mt5_connector import ConnectorError, connector_client

router = APIRouter(prefix="/autopilot", tags=["Autopilot"])

logger = logging.getLogger("autopilot")

def _capture_raw_response(response) -> dict | None:
    try:
        return response.model_dump(mode='json')
    except Exception:
        try:
            return response.dict()
        except Exception:
            return None

_user_states: Dict[int, dict] = {}
_user_locks: Dict[int, asyncio.Lock] = {}

def _get_state(user_id: int) -> dict:
    if user_id not in _user_states:
        _user_states[user_id] = {
            "enabled": False,
            "running": False,
            "logs": [],
            "task": None,
            "last_error_feedback": None,
            "stats": {
                "total_runs": 0,
                "trades_executed": 0,
                "skipped_count": 0,
                "error_count": 0,
                "last_run": None,
                "daily_trade_count": 0,
                "daily_pnl": 0.0,
                "daily_reset_date": None,
            }
        }
    return _user_states[user_id]


def _ensure_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Make a datetime timezone-aware (UTC) if it is naive."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _rebuild_daily_state(user_id: int):
    """Rebuild daily counters from DB after server restart so safety limits aren't reset."""
    state = _get_state(user_id)
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AutopilotTrade).where(
                    AutopilotTrade.user_id == user_id,
                    AutopilotTrade.executed_at >= today_start
                )
            )
            today_trades = result.scalars().all()
        state["stats"]["daily_trade_count"] = len(today_trades)
        state["stats"]["daily_pnl"] = round(sum(t.profit or 0 for t in today_trades), 2)
        state["stats"]["daily_reset_date"] = now.date().isoformat()
    except Exception:
        pass


async def _rebuild_stats(user_id: int):
    """Rebuild all in-memory counters from DB so they persist across restarts."""
    state = _get_state(user_id)
    state["stats"]["trades_executed"] = 0
    state["stats"]["skipped_count"] = 0
    state["stats"]["error_count"] = 0
    try:
        async with AsyncSessionLocal() as db:
            # Trades executed = count of all trades
            result = await db.execute(
                select(func.count(AutopilotTrade.id)).where(
                    AutopilotTrade.user_id == user_id
                )
            )
            count = result.scalar()
            if count:
                state["stats"]["trades_executed"] = count

            # Last run = most recent log timestamp
            result = await db.execute(
                select(func.max(AutopilotLog.timestamp)).where(
                    AutopilotLog.user_id == user_id
                )
            )
            last_ts = result.scalar()
            if last_ts:
                state["stats"]["last_run"] = last_ts.isoformat()

            # Skipped count = distinct cycles with skip messages (not per-provider noise)
            skip_patterns = [
                "Daily trade limit%Skipping%",
                "All backup providers%NO_SETUP%",
                "Skipping cycle%",
            ]
            for pattern in skip_patterns:
                result = await db.execute(
                    select(func.count(func.distinct(AutopilotLog.cycle_number))).where(
                        AutopilotLog.user_id == user_id,
                        AutopilotLog.cycle_number.isnot(None),
                        AutopilotLog.message.ilike(pattern)
                    )
                )
                cnt = result.scalar() or 0
                state["stats"]["skipped_count"] += cnt

            # Error count = distinct cycles with error messages (not per-provider noise)
            error_patterns = [
                "No market data available",
                "AI code generation failed after retries",
                "Trade failed%",
            ]
            for pattern in error_patterns:
                result = await db.execute(
                    select(func.count(func.distinct(AutopilotLog.cycle_number))).where(
                        AutopilotLog.user_id == user_id,
                        AutopilotLog.cycle_number.isnot(None),
                        AutopilotLog.message.ilike(pattern)
                    )
                )
                cnt = result.scalar() or 0
                state["stats"]["error_count"] += cnt

            # Total runs = sum of all three (every cycle ends as trade, skip, or error)
            state["stats"]["total_runs"] = (
                state["stats"]["trades_executed"] +
                state["stats"]["skipped_count"] +
                state["stats"]["error_count"]
            )
    except Exception:
        pass


PROMPT_FILE = str(Path(__file__).resolve().parent.parent.parent.parent / "backend" / "prompt_list.txt")

def load_prompts():
    """Load prompts from file.

    Supports two formats:
      - Old: "1. Analyze XAUUSD..."
      - New: "PROMPT #1:\\nAnalyze XAUUSD price structure..."
    Returns list of "N. <full prompt text>" for backward compatibility.
    """
    try:
        with open(PROMPT_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []

    prompts = []
    current_num = None
    current_lines = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        # Check for new format: "PROMPT #N:"
        new_match = re.match(r"^PROMPT\s*#(\d+):?\s*$", line, re.I)
        if new_match:
            # Save previous prompt if any
            if current_num is not None and current_lines:
                text = " ".join(current_lines).strip()
                prompts.append(f"{current_num}. {text}")
            current_num = int(new_match.group(1))
            current_lines = []
            continue

        # Check for old format: "N. text"
        old_match = re.match(r"^(\d+)\.\s*(.*)", line)
        if old_match and current_num is None:
            # Save previous old-style prompt
            if current_num is not None and current_lines:
                text = " ".join(current_lines).strip()
                prompts.append(f"{current_num}. {text}")
            current_num = int(old_match.group(1))
            current_lines = [old_match.group(2)]
            continue

        # Accumulate content lines for the current prompt
        if current_num is not None:
            # Clean up extra whitespace
            cleaned = re.sub(r'\s+', ' ', line).strip()
            if cleaned:
                current_lines.append(cleaned)

    # Save the last prompt
    if current_num is not None and current_lines:
        text = " ".join(current_lines).strip()
        prompts.append(f"{current_num}. {text}")

    return prompts


def add_log(user_id: int, message: str, level: str = "INFO"):
    state = _get_state(user_id)
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    log_entry = {"timestamp": timestamp, "level": level, "message": message}
    state["logs"].append(log_entry)
    if len(state["logs"]) > 100:
        state["logs"] = state["logs"][-100:]
    # Also emit to Python logger so journalctl captures it
    if level == "ERROR":
        logger.error("[user=%d] %s", user_id, message)
    elif level == "WARNING":
        logger.warning("[user=%d] %s", user_id, message)
    elif level == "SUCCESS":
        logger.info("[user=%d] %s", user_id, message)
    else:
        logger.info("[user=%d] %s", user_id, message)
    # Persist to DB (fire-and-forget)
    cycle_number = state.get("stats", {}).get("total_runs")
    asyncio.create_task(_persist_log(user_id, level, message, cycle_number))


async def _persist_log(user_id: int, level: str, message: str, cycle_number: int | None = None):
    try:
        async with AsyncSessionLocal() as db:
            entry = AutopilotLog(
                user_id=user_id,
                level=level,
                message=message,
                cycle_number=cycle_number,
            )
            db.add(entry)
            await db.commit()
    except Exception:
        pass  # Log persistence should never crash the calling code


# All connector traffic goes through connector_client, which holds the one
# connector address from the server settings. Autopilot used to keep its own
# HTTP client and let each user set a different connector address.


async def initialize_mt5_connector(user_id: int) -> bool:
    try:
        data = await connector_client.initialize()
        if data.get("success"):
            account = data.get("account", {})
            add_log(user_id, f"MT5 Connected: {account.get('server')} | Balance: ${account.get('balance', 0):.2f}", "SUCCESS")
            return True
        add_log(user_id, f"MT5 init failed", "ERROR")
    except Exception as e:
        add_log(user_id, f"MT5 connection error: {str(e)}", "ERROR")
    return False


async def get_market_data(user_id: int, symbol: str, timeframe: str = "1m", count: int = 500):
    try:
        data = await connector_client.get_latest_data(symbol, timeframe, count)
        if data.get("success"):
            return data.get("data", [])
    except Exception as e:
        add_log(user_id, f"Failed to fetch market data: {str(e)}", "ERROR")
    return None


def _build_order_action(direction: str, order_type: str) -> str:
    """Build MT5 action string from direction and order type."""
    d = direction.upper()
    ot = (order_type or "market").lower()
    if ot == "market":
        return d  # "BUY" or "SELL"
    if ot == "limit":
        return f"{d}_LIMIT"  # "BUY_LIMIT" or "SELL_LIMIT"
    if ot == "stop":
        return f"{d}_STOP"  # "BUY_STOP" or "SELL_STOP"
    return d


async def execute_trade(user_id: int, symbol: str, direction: str, volume: float, entry_price: float = None,
                       sl: float = None, tp: float = None, comment: str = "[AUTOPILOT]", prompt_num: int = None,
                       order_type: str = "market"):
    try:
        if prompt_num:
            if isinstance(prompt_num, int) and prompt_num < 0:
                trade_comment = f"[AUTOPILOT] Custom-{abs(prompt_num)}"
            else:
                trade_comment = f"[AUTOPILOT] P{prompt_num}"
        else:
            trade_comment = comment

        action = _build_order_action(direction, order_type)
        is_pending = order_type.lower() in ("limit", "stop")

        # Fetch symbol info for min stop distance + current price
        price = None
        min_dist = None
        digits = None
        try:
            sym_data = await connector_client.get_symbol(symbol)
            price = sym_data.get("bid") or sym_data.get("ask")
            stops_level = sym_data.get("trade_stops_level") or sym_data.get("stops_level")
            point = sym_data.get("point")
            if stops_level is not None and point:
                min_dist = max(stops_level, 10) * point
            digits = sym_data.get("digits")
        except Exception as e:
            add_log(user_id, f"Could not fetch symbol info for {symbol}: {str(e)}", "ERROR")

        # Reference price for stop distance checks (current market for pending orders too)
        ref_price = price or entry_price

        # Apply minimum stop distance safeguard to SL
        if sl and sl > 0 and min_dist and ref_price and digits:
            sl = round(sl, digits)
            is_buy = direction.upper() == "BUY"
            if is_buy:
                if sl >= ref_price - min_dist:
                    adjusted = round(ref_price - min_dist, digits)
                    add_log(user_id, f"SL {sl} too close, adjusted to {adjusted}", "WARNING")
                    sl = adjusted
            else:
                if sl <= ref_price + min_dist:
                    adjusted = round(ref_price + min_dist, digits)
                    add_log(user_id, f"SL {sl} too close, adjusted to {adjusted}", "WARNING")
                    sl = adjusted

        # Minimum stop distance safeguard to TP
        if tp and tp > 0 and min_dist and ref_price and digits:
            tp = round(tp, digits)
            is_buy = direction.upper() == "BUY"
            if is_buy:
                if tp <= ref_price + min_dist:
                    adjusted = round(ref_price + min_dist, digits)
                    add_log(user_id, f"TP {tp} too close, adjusted to {adjusted}", "WARNING")
                    tp = adjusted
            else:
                if tp >= ref_price - min_dist:
                    adjusted = round(ref_price - min_dist, digits)
                    add_log(user_id, f"TP {tp} too close, adjusted to {adjusted}", "WARNING")
                    tp = adjusted

        payload = {"symbol": symbol, "action": action, "volume": volume, "comment": trade_comment}
        if is_pending:
            payload["price"] = entry_price
        if sl and sl > 0:
            payload["sl"] = sl
        if tp and tp > 0:
            payload["tp"] = tp

        data = await connector_client.place_order(payload)
        if data.get("success"):
            return {"success": True, "ticket": data.get("ticket"), "price": data.get("price")}
        return {"success": False, "error": "Order failed"}
    except ConnectorError as e:
        add_log(user_id, f"Trade execution failed: {e.detail}", "ERROR")
        return {"success": False, "error": e.detail}
    except Exception as e:
        add_log(user_id, f"Trade execution failed: {str(e)}", "ERROR")
        return {"success": False, "error": str(e)}


async def _update_model_usage(user_id: int, usage: dict):
    """Update ModelUsage counters for a successful API call (fire-and-forget)."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(ModelUsage).where(
                    ModelUsage.provider == usage["provider"],
                    ModelUsage.model == usage["model"],
                    ModelUsage.user_id == user_id,
                )
            )
            mu = result.scalar_one_or_none()
            cost = estimate_cost(
                usage["prompt_tokens"], usage["completion_tokens"],
                usage["provider"], usage["model"],
            )
            if mu:
                mu.total_requests += 1
                mu.total_tokens += usage["total_tokens"]
                mu.total_cost += cost
                mu.last_used = datetime.now(timezone.utc)
            else:
                mu = ModelUsage(
                    provider=usage["provider"],
                    model=usage["model"],
                    user_id=user_id,
                    total_requests=1,
                    total_tokens=usage["total_tokens"],
                    total_cost=cost,
                )
                db.add(mu)
            await db.commit()
    except Exception:
        pass  # Never crash the autopilot loop


async def _log_ai_call(
    user_id: int, prompt_number: int, cycle_number: int,
    provider: str, model: str, stage: str,
    outcome: str = "pending",
    prompt_tokens: int = 0, completion_tokens: int = 0, total_tokens: int = 0,
    error_message: str = None,
) -> int | None:
    """Log every AI API call to AiCallLog. Returns the log ID or None on failure."""
    try:
        from ..core.providers import estimate_cost
        cost = estimate_cost(
            prompt_tokens, completion_tokens,
            provider or "", model or "",
        )
        async with AsyncSessionLocal() as db:
            log = AiCallLog(
                user_id=user_id, prompt_number=prompt_number,
                cycle_number=cycle_number,
                provider=provider, model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                stage=stage, outcome=outcome,
                error_message=error_message, cost=cost,
            )
            db.add(log)
            await db.commit()
            await db.refresh(log)
            return log.id
    except Exception:
        return None


async def check_open_positions(user_id: int = 0):
    try:
        data = await connector_client.get_positions()
        if data.get("success"):
            return data.get("positions", [])
    except Exception as e:
        import traceback as _tb
        add_log(user_id, f"check_open_positions error: {e}\n{_tb.format_exc()}", "ERROR")
    return []


def _infer_prompt_tags(prompt_text: str) -> dict:
    """Infer strategy metadata from free-form prompt text.

    This keeps the existing prompt file format intact while giving Autopilot
    enough structure to choose prompts by market condition.
    """
    text = (prompt_text or "").lower()
    styles = set()
    timeframes = set()
    avoid_when = set()

    keyword_styles = [
        ("breakout", ["breakout", "break out", "range break", "breaks above", "breaks below"]),
        ("trend_following", ["trend", "ema", "moving average", "higher high", "lower low", "pullback"]),
        ("mean_reversion", ["mean reversion", "overbought", "oversold", "rsi", "bollinger", "reversal"]),
        ("scalping", ["scalp", "scalping", "m1", "m5", "1 minute", "5 minute"]),
        ("momentum", ["momentum", "impulse", "strong candle", "volume spike"]),
        ("range", ["range", "support", "resistance", "sideways", "consolidation"]),
    ]
    for style, needles in keyword_styles:
        if any(n in text for n in needles):
            styles.add(style)

    tf_patterns = [
        ("1m", r"\b(?:m1|1m|1[-\s]?min(?:ute)?s?)\b"),
        ("5m", r"\b(?:m5|5m|5[-\s]?min(?:ute)?s?)\b"),
        ("15m", r"\b(?:m15|15m|15[-\s]?min(?:ute)?s?)\b"),
        ("30m", r"\b(?:m30|30m|30[-\s]?min(?:ute)?s?)\b"),
        ("1h", r"\b(?:h1|1h|1[-\s]?hour|hourly)\b"),
        ("4h", r"\b(?:h4|4h|4[-\s]?hour)\b"),
        ("1d", r"\b(?:d1|1d|daily|day)\b"),
    ]
    for label, pattern in tf_patterns:
        if re.search(pattern, text):
            timeframes.add(label)

    if "breakout" in styles:
        avoid_when.add("low_volatility_range")
    if "mean_reversion" in styles or "range" in styles:
        avoid_when.add("strong_trend")
    if "scalping" in styles:
        avoid_when.add("high_spread")

    if not styles:
        styles.add("general")

    return {
        "styles": sorted(styles),
        "timeframes": sorted(timeframes),
        "avoid_when": sorted(avoid_when),
    }


def _classify_market_regime(market_data: list[dict]) -> dict:
    """Classify market state from OHLC candles using deterministic indicators."""
    if not market_data or len(market_data) < 30:
        return {
            "regime": "unknown",
            "trend": "unknown",
            "volatility": "unknown",
            "direction_bias": "neutral",
            "confidence": 0,
            "reason": "insufficient_data",
            "candle_count": len(market_data or []),
        }

    try:
        df = pd.DataFrame(market_data).copy()
        for col in ("open", "high", "low", "close", "volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["open", "high", "low", "close"])
        if len(df) < 30:
            raise ValueError("not enough numeric candles")

        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        last_close = float(close.iloc[-1])

        ema_fast = ta.trend.ema_indicator(close, window=20)
        ema_slow = ta.trend.ema_indicator(close, window=50 if len(df) >= 50 else 30)
        atr = ta.volatility.average_true_range(high, low, close, window=14)
        bb_high = ta.volatility.bollinger_hband(close, window=20, window_dev=2)
        bb_low = ta.volatility.bollinger_lband(close, window=20, window_dev=2)

        latest_atr = float(atr.iloc[-1]) if not pd.isna(atr.iloc[-1]) else 0.0
        atr_mean = float(atr.tail(50).mean()) if len(atr.dropna()) else 0.0
        atr_ratio = latest_atr / atr_mean if atr_mean else 1.0
        bb_width_pct = ((float(bb_high.iloc[-1]) - float(bb_low.iloc[-1])) / last_close * 100) if last_close else 0.0

        fast_now = float(ema_fast.iloc[-1])
        fast_prev = float(ema_fast.iloc[-10]) if len(ema_fast.dropna()) >= 10 else fast_now
        slow_now = float(ema_slow.iloc[-1])
        ema_slope_pct = ((fast_now - fast_prev) / last_close * 100) if last_close else 0.0

        adx_value = None
        try:
            adx = ta.trend.adx(high, low, close, window=14)
            adx_value = float(adx.iloc[-1]) if not pd.isna(adx.iloc[-1]) else None
        except Exception:
            adx_value = None

        if atr_ratio >= 1.35 or bb_width_pct >= 1.8:
            volatility = "high"
        elif atr_ratio <= 0.75 and bb_width_pct <= 0.8:
            volatility = "low"
        else:
            volatility = "normal"

        trend_strength = abs(ema_slope_pct)
        is_trending = trend_strength >= 0.08 or (adx_value is not None and adx_value >= 22)
        if is_trending and fast_now > slow_now and last_close >= slow_now:
            trend = "bullish"
            direction_bias = "BUY"
        elif is_trending and fast_now < slow_now and last_close <= slow_now:
            trend = "bearish"
            direction_bias = "SELL"
        else:
            trend = "range"
            direction_bias = "neutral"

        if trend in ("bullish", "bearish") and volatility == "high":
            regime = f"{trend}_trend_high_volatility"
        elif trend in ("bullish", "bearish"):
            regime = f"{trend}_trend"
        elif volatility == "low":
            regime = "low_volatility_range"
        elif volatility == "high":
            regime = "high_volatility_range"
        else:
            regime = "range"

        confidence = 50
        confidence += min(25, int(trend_strength * 120))
        if adx_value is not None:
            confidence += 10 if adx_value >= 22 else -5
        if volatility == "normal":
            confidence += 5
        confidence = max(10, min(confidence, 90))

        return {
            "regime": regime,
            "trend": trend,
            "volatility": volatility,
            "direction_bias": direction_bias,
            "confidence": confidence,
            "candle_count": len(df),
            "last_close": round(last_close, 5),
            "atr": round(latest_atr, 5),
            "atr_ratio": round(atr_ratio, 3),
            "bb_width_pct": round(bb_width_pct, 3),
            "ema_slope_pct": round(ema_slope_pct, 4),
            "adx": round(adx_value, 2) if adx_value is not None else None,
        }
    except Exception as e:
        return {
            "regime": "unknown",
            "trend": "unknown",
            "volatility": "unknown",
            "direction_bias": "neutral",
            "confidence": 0,
            "reason": str(e)[:120],
            "candle_count": len(market_data or []),
        }


def _score_prompt_regime_fit(tags: dict, regime: dict) -> tuple[float, list[str]]:
    styles = set(tags.get("styles") or [])
    avoid_when = set(tags.get("avoid_when") or [])
    regime_name = regime.get("regime") or "unknown"
    trend = regime.get("trend")
    volatility = regime.get("volatility")
    score = 0.0
    reasons = []

    if trend in ("bullish", "bearish"):
        if "trend_following" in styles or "momentum" in styles:
            score += 18
            reasons.append("trend strategy fits trending market")
        if "breakout" in styles and volatility in ("normal", "high"):
            score += 10
            reasons.append("breakout strategy fits active trend")
        if "mean_reversion" in styles or "range" in styles:
            score -= 12
            reasons.append("range/reversal strategy penalized in trend")
    elif "range" in str(regime_name):
        if "mean_reversion" in styles or "range" in styles:
            score += 16
            reasons.append("range strategy fits ranging market")
        if "trend_following" in styles and volatility == "low":
            score -= 10
            reasons.append("trend strategy penalized in quiet range")

    if volatility == "high":
        if "momentum" in styles or "breakout" in styles:
            score += 10
            reasons.append("momentum/breakout fits high volatility")
        if "scalping" in styles:
            score -= 5
            reasons.append("scalping slightly penalized in high volatility")
    elif volatility == "low":
        if "breakout" in styles:
            score -= 12
            reasons.append("breakout penalized in low volatility")
        if "mean_reversion" in styles or "range" in styles:
            score += 8
            reasons.append("mean reversion fits low volatility")

    for avoid in avoid_when:
        if avoid == "strong_trend" and trend in ("bullish", "bearish"):
            score -= 10
        elif avoid == "low_volatility_range" and regime_name == "low_volatility_range":
            score -= 10

    return score, reasons


async def _choose_prompt_with_context(
    user_id: int,
    prompt_pool: list[dict],
    symbol: str,
    market_regime: dict,
) -> tuple[dict, dict]:
    """Choose a prompt using regime fit plus historical performance."""
    scores_by_prompt: dict[str, list[StrategyScore]] = {}
    recent_by_prompt: dict[str, dict] = {}

    try:
        async with AsyncSessionLocal() as db:
            score_result = await db.execute(
                select(StrategyScore).where(
                    StrategyScore.symbol == symbol,
                    StrategyScore.total_trades >= 3,
                )
            )
            for score in score_result.scalars().all():
                scores_by_prompt.setdefault(score.prompt_text, []).append(score)

            recent_result = await db.execute(
                select(AutopilotTrade)
                .where(
                    AutopilotTrade.user_id == user_id,
                    AutopilotTrade.symbol == symbol,
                    AutopilotTrade.profit.isnot(None),
                )
                .order_by(AutopilotTrade.executed_at.desc())
                .limit(200)
            )
            for trade in recent_result.scalars().all():
                bucket = recent_by_prompt.setdefault(
                    trade.prompt_text,
                    {"trades": 0, "wins": 0, "pnl": 0.0},
                )
                bucket["trades"] += 1
                bucket["pnl"] += trade.profit or 0.0
                if (trade.profit or 0.0) > 0:
                    bucket["wins"] += 1
    except Exception:
        pass

    ranked = []
    for prompt in prompt_pool:
        tags = _infer_prompt_tags(prompt["text"])
        regime_score, fit_reasons = _score_prompt_regime_fit(tags, market_regime)
        score = 50.0 + regime_score
        history_reasons = []

        strategy_rows = scores_by_prompt.get(prompt["text"], [])
        if strategy_rows:
            best = max(
                strategy_rows,
                key=lambda s: (
                    s.total_trades or 0,
                    s.profit_factor or 0,
                    s.total_pnl or 0,
                ),
            )
            win_rate = best.win_rate or 0.0
            profit_factor = best.profit_factor or 0.0
            total_pnl = best.total_pnl or 0.0
            sample = best.total_trades or 0
            score += min(22, max(-18, (win_rate - 50) * 0.45))
            if profit_factor:
                score += min(12, (profit_factor - 1.0) * 8)
            score += min(10, max(-10, total_pnl / 25))
            history_reasons.append(
                f"scoreboard {sample} trades, win {win_rate:.1f}%, pf {profit_factor or 0:.2f}"
            )

        recent = recent_by_prompt.get(prompt["text"])
        if recent and recent["trades"] >= 3:
            recent_wr = recent["wins"] / recent["trades"] * 100
            score += min(12, max(-12, (recent_wr - 50) * 0.35))
            score += min(8, max(-8, recent["pnl"] / 20))
            history_reasons.append(
                f"recent {recent['trades']} trades, win {recent_wr:.1f}%, pnl {recent['pnl']:+.2f}"
            )

        score = max(5.0, min(score, 95.0))
        weight = max(1, int(score))
        ranked.append({
            "prompt": prompt,
            "score": round(score, 2),
            "weight": weight,
            "tags": tags,
            "reasons": fit_reasons + history_reasons,
        })

    ranked.sort(key=lambda item: item["score"], reverse=True)
    weighted = []
    for item in ranked:
        weighted.extend([item] * item["weight"])

    selected = random.choice(weighted) if weighted else random.choice(ranked)
    context = {
        "selection_mode": "regime_score_weighted",
        "selected_score": selected["score"],
        "selected_tags": selected["tags"],
        "selected_reasons": selected["reasons"],
        "market_regime": market_regime,
        "top_candidates": [
            {
                "id": item["prompt"]["id"],
                "is_custom": item["prompt"]["is_custom"],
                "score": item["score"],
                "tags": item["tags"],
                "reasons": item["reasons"][:3],
            }
            for item in ranked[:5]
        ],
    }
    return selected["prompt"], context


async def run_autopilot_cycle(user_id: int):
    state = _get_state(user_id)
    default_prompts = load_prompts()
    
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AutopilotSettings).where(AutopilotSettings.user_id == user_id)
        )
        settings_obj = result.scalar_one_or_none()

        if not settings_obj:
            add_log(user_id, "Autopilot not configured", "ERROR")
            return

        result = await db.execute(
            select(UserPrompt).where(UserPrompt.user_id == user_id)
        )
        personal_prompts = result.scalars().all()

        symbol = settings_obj.symbol
        provider = settings_obj.provider
        model = settings_obj.model
        lot_size = settings_obj.default_lot
        mt5_connected = settings_obj.mt5_connected
        selected_ids = settings_obj.selected_prompts or []
        max_trades = settings_obj.max_trades_per_day
        max_loss = settings_obj.max_daily_loss
        cooldown = settings_obj.cooldown_minutes

    prompt_pool = []
    for line in default_prompts:
        try:
            p_num = int(line.split(".")[0].strip())
            if not selected_ids or p_num in selected_ids:
                prompt_pool.append({"id": p_num, "text": line.split(".", 1)[1].strip(), "is_custom": False})
        except Exception:
            continue

    for p in personal_prompts:
        custom_id = f"custom_{p.id}"
        if not selected_ids or custom_id in selected_ids:
            prompt_pool.append({"id": custom_id, "text": p.content, "is_custom": True})

    if not prompt_pool:
        add_log(user_id, "No prompts selected in settings", "ERROR")
        return

    if not mt5_connected:
        add_log(user_id, "Initializing MT5 connection...")
        conn_ok = await initialize_mt5_connector(user_id)
        if not conn_ok:
            add_log(user_id, "Failed to connect to MT5. Check MT5_CONNECTOR_URL and MT5_API_TOKEN on the server.", "ERROR")
            return
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AutopilotSettings).where(AutopilotSettings.user_id == user_id))
            s = result.scalar_one_or_none()
            if s:
                s.mt5_connected = True
                await db.commit()

    state["stats"]["total_runs"] += 1
    state["stats"]["last_run"] = datetime.now(timezone.utc).isoformat()
    add_log(user_id, f"=== Starting Cycle #{state['stats']['total_runs']} ===")

    # Safety limits — persist across server restarts by reading from DB
    today = datetime.now(timezone.utc).date()
    if state["stats"]["daily_reset_date"] != str(today):
        today_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
        today_end = today_start + timedelta(days=1)
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(
                    func.count(AutopilotTrade.id),
                    func.coalesce(func.sum(AutopilotTrade.profit), 0),
                ).where(
                    AutopilotTrade.user_id == user_id,
                    AutopilotTrade.executed_at >= today_start,
                    AutopilotTrade.executed_at < today_end,
                )
            )).one()
            state["stats"]["daily_trade_count"] = row[0]
            state["stats"]["daily_pnl"] = float(row[1])
        state["stats"]["daily_reset_date"] = str(today)

    if state["stats"]["daily_trade_count"] >= max_trades:
        add_log(user_id, f"Daily trade limit ({max_trades}) reached. Skipping.", "WARNING")
        state["stats"]["skipped_count"] += 1
        return

    baseline_market_data = await get_market_data(
        user_id,
        symbol,
        timeframe="15m",
        count=300,
    )
    market_regime = _classify_market_regime(baseline_market_data or [])
    add_log(
        user_id,
        (
            "Market regime: "
            f"{market_regime.get('regime')} | trend={market_regime.get('trend')} "
            f"vol={market_regime.get('volatility')} bias={market_regime.get('direction_bias')} "
            f"confidence={market_regime.get('confidence')}%"
        ),
    )

    chosen, decision_context = await _choose_prompt_with_context(
        user_id=user_id,
        prompt_pool=prompt_pool,
        symbol=symbol,
        market_regime=market_regime,
    )

    prompt_id_val = chosen["id"]
    prompt_text = chosen["text"]
    if chosen["is_custom"]:
        prompt_num = -int(prompt_id_val.split('_')[1])
        display_id = f"Custom-{prompt_id_val.split('_')[1]}"
    else:
        prompt_num = prompt_id_val
        display_id = f"#{prompt_num}"
    selected_score = decision_context.get("selected_score")
    selected_tags = decision_context.get("selected_tags", {})
    selected_styles = ",".join(selected_tags.get("styles") or ["general"])
    add_log(
        user_id,
        f"Using Strategy {display_id} | score={selected_score} | styles={selected_styles}: {prompt_text[:50]}...",
    )

    # ── AI call helper with 429 retry + provider fallback ────────────────────
    _call_count = 0
    _call_tokens = 0
    _call_log_ids: list[int] = []

    async def _log_call(outcome: str, p: str, m: str, st: str, pt: int = 0, ct: int = 0, tt: int = 0, err: str = None):
        nonlocal _call_count, _call_tokens
        _call_count += 1
        if tt:
            _call_tokens += tt
        log_id = await _log_ai_call(
            user_id, prompt_num, state["stats"]["total_runs"],
            p, m, st, outcome=outcome,
            prompt_tokens=pt, completion_tokens=ct, total_tokens=tt,
            error_message=err,
        )
        if log_id:
            _call_log_ids.append(log_id)

    async def _call_ai_with_retry(messages: list, provider: str, model: str, max_retries: int = 3, stage: str = "initial") -> tuple[str | None, dict | None]:
        """Call AI with exponential backoff on 429, fallback to next provider.
        Returns (content, usage_dict) where usage_dict has prompt_tokens, completion_tokens, total_tokens.
        """
        from ..core.providers import PROVIDERS, get_provider_names, get_base_url, resolve_api_key
        
        providers = get_provider_names()
        provider_idx = providers.index(provider) if provider in providers else 0
        
        for attempt in range(max_retries):
            for p_idx in range(provider_idx, len(providers)):
                p = providers[p_idx]
                api_key = await resolve_api_key(p, settings, user_id, AsyncSessionLocal)
                if not api_key:
                    await _log_call("no_key", p, model if p == provider else PROVIDERS[p]["models"][0], stage)
                    continue
                if p == "nvidia" and not api_key.startswith("nvapi-"):
                    api_key = f"nvapi-{api_key}"
                actual_model = model if p == provider else PROVIDERS[p]["models"][0]
                
                try:
                    client = AsyncOpenAI(base_url=get_base_url(p), api_key=api_key)
                    response = await client.chat.completions.create(
                        model=actual_model,
                        messages=messages,
                        temperature=0.2,
                        max_tokens=2500,
                        timeout=60
                    )
                    content = response.choices[0].message.content or ""
                    match = re.search(r'```(?:python)?\n?(.*?)```', content, re.DOTALL)
                    result = match.group(1).strip() if match else content.strip()
                    usage = None
                    if hasattr(response, 'usage') and response.usage:
                        usage = {
                            "provider": p,
                            "model": actual_model,
                            "prompt_tokens": response.usage.prompt_tokens or 0,
                            "completion_tokens": response.usage.completion_tokens or 0,
                            "total_tokens": response.usage.total_tokens or 0,
                        }
                    if usage:
                        asyncio.create_task(_update_model_usage(user_id, usage))
                        await _log_call("success", p, actual_model, stage,
                            usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"])
                    else:
                        await _log_call("no_usage", p, actual_model, stage)
                    return result, usage
                except Exception as e:
                    err_str = str(e).lower()
                    if "429" in err_str or "too_many_requests" in err_str or "queue_exceeded" in err_str:
                        wait = min(2 ** attempt * 10, 60)
                        add_log(user_id, f"Provider {p} rate limited (429), waiting {wait}s...", "WARNING")
                        await _log_call("rate_limited", p, actual_model, stage, err=str(e)[:200])
                        await asyncio.sleep(wait)
                        break
                    else:
                        err_msg = str(e)[:200]
                        add_log(user_id, f"Provider {p} error: {err_msg}", "WARNING")
                        await _log_call("error", p, actual_model, stage, err=err_msg)
                        continue
        return None, None

    # Detect required timeframe from prompt text and fetch from MT5 directly
    def _detect_timeframe(text: str) -> tuple:
        """Return (mt5_timeframe, candle_count) based on prompt.
        Check more specific (M15, M5) before broader (H1, H4, D1) to avoid
        catching reference levels (e.g. 'H1 resistance') instead of the
        actual analysis timeframe (e.g. 'M15').
        """
        lower = text.lower()
        # Check shorter (more granular) timeframes FIRST.
        # For multi-TF prompts ("H4 trend, H1 entry"), this detects the LOWEST TF
        # so the AI gets granular data and can resample UP to higher TFs if needed.
        if re.search(r'\b(?:m15|15m|15[-\s]?min(?:ute)?s?\b)', lower):
            return ("15m", 500)
        if re.search(r'\b(?:m5|5m|5[-\s]?min(?:ute)?s?\b)', lower):
            return ("5m", 500)
        if re.search(r'\b(?:m1|1m|1[-\s]?min(?:ute)?s?\b)', lower):
            return ("1m", 500)
        if re.search(r'\b(?:m30|30m|30[-\s]?min(?:ute)?s?\b)', lower):
            return ("30m", 500)
        if re.search(r'\b1[-\s]?(?:h|hour)\b|one[-\s]?hour|hourly|h1\b|1hrs?\b', lower):
            return ("1h", 300)
        if re.search(r'\b4[-\s]?(?:h|hour)\b|four[-\s]?hour|h4\b|4hrs?\b', lower):
            return ("4h", 200)
        if re.search(r'\b1[-\s]?(?:d|day|w|week)\b|daily|weekly|d1|w1|previous\s*day|yesterday', lower):
            return ("1d", 200)
        return ("4h", 200)  # default: 4H for swing trading

    tf, count = _detect_timeframe(prompt_text)
    market_data = await get_market_data(user_id, symbol, timeframe=tf, count=count)
    if not market_data or len(market_data) == 0:
        add_log(user_id, "No market data available", "ERROR")
        state["stats"]["error_count"] += 1
        return
    add_log(user_id, f"Loaded {len(market_data)} {tf} candles for {symbol}")

    # ── Compute ATR for SL/TP sizing ──
    atr_value = 0.0
    avg_atr_20 = 0.0
    try:
        df_atr = pd.DataFrame(market_data)
        if 'high' in df_atr.columns and 'low' in df_atr.columns and 'close' in df_atr.columns:
            atr_series = ta.volatility.average_true_range(
                df_atr['high'].astype(float),
                df_atr['low'].astype(float),
                df_atr['close'].astype(float),
                window=14
            )
            valid = atr_series.dropna()
            if len(valid) > 0:
                atr_value = float(valid.iloc[-1])
                avg_atr_20 = float(valid.tail(min(20, len(valid))).mean())
    except Exception:
        pass
    add_log(user_id, f"ATR(14): {atr_value:.2f} | Avg(20): {avg_atr_20:.2f}")

    # ── SANDBOX APPROACH ──────────────────────────────────────────────
    # Instead of dumping raw candle text into the AI prompt, we:
    # 1. Ask AI to write analysis code (short prompt, ~150 tokens)
    # 2. Execute the code in sandbox with 1m OHLC data
    # 3. Parse TRADE_SETUP JSON or NO_SETUP from sandbox output
    # 4. Self-correct if code fails (up to 2 retries)

    error_feedback = state.get("last_error_feedback")
    error_section = ""
    if error_feedback:
        error_section = f"\nPREVIOUS TRADE ERROR FEEDBACK (learn from this):\n{error_feedback}\n- Adjust stop loss / take profit to be further from entry.\n- Do NOT repeat the same mistake.\n"

    top_candidates = decision_context.get("top_candidates", [])
    top_candidate_lines = []
    for item in top_candidates[:3]:
        styles = ",".join((item.get("tags") or {}).get("styles") or [])
        top_candidate_lines.append(
            f"- {item.get('id')}: score={item.get('score')} styles={styles}"
        )
    decision_section = f"""
AUTOPILOT DECISION CONTEXT:
- Market regime: {market_regime.get('regime')} (trend={market_regime.get('trend')}, volatility={market_regime.get('volatility')}, directional_bias={market_regime.get('direction_bias')})
- Regime confidence: {market_regime.get('confidence')}%
- Selected prompt score: {decision_context.get('selected_score')}
- Selected prompt tags: {json.dumps(decision_context.get('selected_tags', {}))}
- Selection reasons: {"; ".join(decision_context.get('selected_reasons') or []) or "No historical reasons yet"}
- Top prompt candidates:
{chr(10).join(top_candidate_lines) if top_candidate_lines else "- No ranked candidates available"}
Use this context as guidance.
"""

    candle_count = len(market_data)
    data_warning = ""
    if candle_count < 100:
        data_warning = f"\nWARNING: Limited historical data ({candle_count} candles). Indicators with large windows (like SMA 200) will fail. RSI(14), ATR(14), and Bollinger Bands(20) are safe above 30 candles.\n"
    elif candle_count < 500:
        data_warning = f"\nNOTE: {candle_count} candles available. SMA 200 may produce NaNs. Use .dropna() before accessing results.\n"

    code_prompt = f"""You are a quant trader. Write Python code to analyze market data.

IMPORTANT RULES:
1. Write DIRECT executable statements -- NOT a function definition. The code runs via exec(), NOT by calling a function.
   WRONG (will produce NO output):
      def calculate_signals(df): ...
   RIGHT:
      rsi = ta.momentum.rsi(df['close'], window=14)
      print(f"RSI: {{rsi.iloc[-1]:.2f}}")

2. Available libraries in sandbox:
   - pandas as pd, numpy as np, math, json, datetime
   - ta (technical-analysis-library-python)
   - ta.momentum.rsi(close, window=14)
   - ta.trend.sma_indicator(close, window=200)
   - ta.trend.ema_indicator(close, window=50)
   - ta.volatility.average_true_range(high, low, close, window=14)
   - ta.volatility.bollinger_hband(close, window=20, window_dev=2)
   - ta.volatility.bollinger_lband(close, window=20, window_dev=2)
   - ta.momentum.stoch(high, low, close, window=14)

3. The DataFrame `df` is already loaded with {tf.upper()} OHLC data.
    Columns: open, high, low, close, volume (may be 0 if unavailable), timestamp (datetime).
    The DataFrame index is also datetime (same as timestamp column).
    timestamp is ALREADY a datetime object — DO NOT call pd.to_datetime() on it.
   Use df.tail(N) for last N rows. NEVER use hardcoded indices like df.iloc[13].

   For multi-timeframe analysis, resample df UP to higher TFs:
     df_4h = df.resample('4H', on='timestamp').agg({{'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}})
   Aliases: '1H'=1h, '4H'=4h, '1D'=1d.
   You CANNOT resample DOWN (e.g. 1h → 1m) — that creates fake data.

4. You have THREE possible outputs at the end:

   A) FULL TRADE SETUP — clear setup, good risk-reward (RR >= 1:1.5), confidence 60-95.
      ```json
      {{"action": "TRADE_SETUP", "symbol": "{symbol}", "direction": "BUY", "order_type": "market", "entry_price": 0.0, "stop_loss": 0.0, "take_profit": 0.0, "lot_size": {lot_size}, "reasoning": "Brief explanation", "confidence": 75}}
      ```

   B) REDUCED SETUP — setup exists but lower conviction (RR >= 1:1, confidence 40-59).
      Use half the standard lot size ({lot_size}/2) and tighter stop.
      ```json
      {{"action": "TRADE_SETUP", "symbol": "{symbol}", "direction": "BUY", "order_type": "market", "entry_price": 0.0, "stop_loss": 0.0, "take_profit": 0.0, "lot_size": {lot_size}/2, "reasoning": "Lower conviction: explain why", "confidence": 50}}
      ```

   C) NO_SETUP — no trade opportunity at all.

   Pick A if confidence >= 60 AND RR >= 1:1.5.
   Pick B if confidence 40-59 AND RR >= 1:1.
   Pick C if confidence < 40 or no valid level.

5. After resample() or dropna(), always check len(df) before accessing elements.
   Do NOT assume the resampled DataFrame has the same row count.

6. NEVER write the double-underscore __ character sequence in your code.
   ANY code containing __ (like __class__, __dict__, __name__, __version__, __init__)
   will be REJECTED by the sandbox. This includes debug prints, comments, and strings.
   If you need to check a type, use type(obj).__name__ is REJECTED — use str(type(obj)) instead.

{data_warning}
CURRENT VOLATILITY:
- ATR(14): {atr_value:.2f} | AVG ATR(20): {avg_atr_20:.2f}
- CRITICAL: Stop loss MUST be within 1.0x to 1.5x ATR distance from entry price, NEVER exceed 30 points.
  Example: ATR={atr_value:.1f}, entry=4060 → SL must be at {4060-atr_value*1.5:.1f} to {4060+atr_value*1.5:.1f} (NOT at a swing high 80+ pts away).
- You can compute ATR in your code: atr_14 = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=14).iloc[-1]
- Take profit must give minimum RR of 1:1.5 (TP distance >= 1.5x SL distance).
- If ATR > 1.5x its 20-period average (high volatility), use tighter stop (1.0x ATR) and reduce lot size by 25%.
- If ATR < 0.5x its 20-period average (low volatility), normal SL rules apply.

Strategy:
{prompt_text}
{decision_section}
{error_section}
"""

    add_log(user_id, f"AI prompt: analyze {len(market_data)} {tf} candles for strategy")

    full_raw_response = ""

    # Step 1: AI generates analysis code (with 429 retry + provider fallback)
    _last_usage = None
    _source = "sandbox"
    generated_code, _last_usage = await _call_ai_with_retry(
        messages=[{"role": "user", "content": code_prompt}],
        provider=provider,
        model=model,
        max_retries=3,
        stage="initial",
    )
    if not generated_code:
        add_log(user_id, "AI code generation failed after retries", "ERROR")
        state["stats"]["error_count"] += 1
        return
    add_log(user_id, f"AI generated code ({len(generated_code)} chars)")

    # Step 2 & 3: Execute in sandbox with self-correction
    from ..api.execute import run_python_code

    setup = None
    ai_response = generated_code  # Store generated code as AI response for DB
    # Build provider failover order: user's pick first, then known-working,
    # then the rest sorted by reliability.
    from ..core.providers import get_provider_names as _get_providers
    _priority = ("cerebras", "github")
    _all_providers = _get_providers()
    _remaining = [p for p in _all_providers if p != provider]
    _known_working = [p for p in _remaining if p in _priority]
    _others = [p for p in _remaining if p not in _priority]
    _retry_providers = [provider] + _known_working + _others
    if not _retry_providers:
        _retry_providers = [provider]

    for p_idx, retry_p in enumerate(_retry_providers):
        # First iteration uses the already-generated code from the initial call.
        # Subsequent iterations generate new code with the next provider.
        if p_idx > 0:
            add_log(user_id, f"Trying provider {retry_p} for code generation...", "INFO")
            new_code, _last_usage = await _call_ai_with_retry(
                messages=[{"role": "user", "content": code_prompt}],
                provider=retry_p,
                model=PROVIDERS[retry_p]["models"][0],
                max_retries=2,
                stage="failover",
            )
            if not new_code:
                add_log(user_id, f"{retry_p} code generation returned empty, skipping")
                continue
            generated_code = new_code
            ai_response = generated_code

        # Execute code in sandbox
        add_log(user_id, f"Executing code from {retry_p} in sandbox...", "INFO")
        try:
            sandbox_result = await run_python_code(
                code=generated_code,
                market_data=market_data,
                symbol=symbol,
                user_id=user_id,
            )
        except Exception as e:
            add_log(user_id, f"Sandbox execution error with {retry_p}: {str(e)}", "ERROR")
            continue

        if sandbox_result.get("success"):
            output = sandbox_result.get("output", "")

            # Try parse TRADE_SETUP from output (```json block or raw JSON)
            jm = re.search(r'```json\n?(.*?)```', output, re.DOTALL)
            if jm:
                try:
                    setup = json.loads(jm.group(1))
                    add_log(user_id, f"TRADE_SETUP found via {retry_p} (conf={setup.get('confidence')}%)")
                    break
                except json.JSONDecodeError:
                    pass

            if not setup:
                for line in output.strip().split("\n"):
                    line = line.strip()
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict) and obj.get("action") == "TRADE_SETUP":
                            setup = obj
                            break
                    except json.JSONDecodeError:
                        pass

            if setup:
                break

            if "NO_SETUP" in output:
                add_log(user_id, f"{retry_p} says NO_SETUP, trying next provider", "WARNING")
                continue

            # Unclear output — try self-correction once with same provider
            add_log(user_id, f"{retry_p} output unclear, trying self-correction...", "WARNING")
            corrected, _last_usage = await _call_ai_with_retry(
                messages=[
                    {"role": "user", "content": code_prompt},
                    {"role": "assistant", "content": generated_code},
                    {"role": "user", "content": f"The code ran but didn't output a valid TRADE_SETUP or NO_SETUP. Fix it to output exactly one of these formats. Output was:\n{output[:400]}"}
                ],
                provider=retry_p,
                model=PROVIDERS[retry_p]["models"][0],
                max_retries=2,
                stage="self_correct",
            )
            if corrected:
                generated_code = corrected
                ai_response = generated_code
                # Execute the corrected code
                try:
                    sandbox_result = await run_python_code(
                        code=generated_code,
                        market_data=market_data,
                        symbol=symbol,
                        user_id=user_id,
                    )
                except Exception as e:
                    add_log(user_id, f"Corrected code from {retry_p} also failed: {str(e)}", "ERROR")
                    continue
                if sandbox_result.get("success"):
                    corrected_output = sandbox_result.get("output", "")
                    jm2 = re.search(r'```json\n?(.*?)```', corrected_output, re.DOTALL)
                    if jm2:
                        try:
                            setup = json.loads(jm2.group(1))
                            add_log(user_id, f"TRADE_SETUP found via {retry_p} self-correction (conf={setup.get('confidence')}%)")
                            break
                        except json.JSONDecodeError:
                            pass
                    if not setup:
                        for line in corrected_output.strip().split("\n"):
                            try:
                                obj = json.loads(line.strip())
                                if isinstance(obj, dict) and obj.get("action") == "TRADE_SETUP":
                                    setup = obj
                                    break
                            except json.JSONDecodeError:
                                pass
                    if setup:
                        break
                    if "NO_SETUP" in corrected_output:
                        add_log(user_id, f"{retry_p} self-correction says NO_SETUP", "WARNING")
            else:
                add_log(user_id, f"{retry_p} self-correction failed", "WARNING")
        else:
            # Sandbox error — try next provider
            sand_err = sandbox_result.get("error", "Unknown error")[:200]
            add_log(user_id, f"{retry_p} code error: {sand_err}", "WARNING")

    # ── BACKUP: all sandbox providers failed, send 50 candles + indicators directly ──
    if not setup:
        add_log(user_id, "All providers failed sandbox, trying backup (50 candles + indicators)...", "WARNING")

        # Build candle text block (last 50)
        try:
            df_view = pd.DataFrame(market_data[-50:])
            rows = []
            for _, r in df_view.iterrows():
                t = str(r.get('time') or r.get('datetime') or '')[:16]
                vol = float(r.get('volume', 0) or 0)
                rows.append(
                    f"{t}  {float(r['open']):>8.2f}  {float(r['high']):>8.2f}  "
                    f"{float(r['low']):>8.2f}  {float(r['close']):>8.2f}  {vol:>6.0f}"
                )
            candle_block = "Date/Time         Open      High      Low       Close     Volume\n" + "\n".join(rows)
        except Exception as e:
            add_log(user_id, f"Failed to format candles: {str(e)}", "ERROR")
            candle_block = "(candle data unavailable)"

        # Compute indicators on full data
        try:
            full_df = pd.DataFrame(market_data)
            close_s = full_df['close'].astype(float)
            high_s = full_df['high'].astype(float)
            low_s = full_df['low'].astype(float)

            rsi_s = ta.momentum.rsi(close_s, window=14)
            sma20_s = ta.trend.sma_indicator(close_s, window=20)
            sma50_s = ta.trend.sma_indicator(close_s, window=50)
            upper_s = ta.volatility.bollinger_hband(close_s, window=20, window_dev=2)
            lower_s = ta.volatility.bollinger_lband(close_s, window=20, window_dev=2)
            atr_s = ta.volatility.average_true_range(high_s, low_s, close_s, window=14)
            stoch_s = ta.momentum.stoch(high_s, low_s, close_s, window=14)

            ind_lines = [
                f"RSI(14): {float(rsi_s.iloc[-1]):.1f}" if not pd.isna(rsi_s.iloc[-1]) else "RSI(14): N/A",
                f"SMA20: {float(sma20_s.iloc[-1]):.2f}" if not pd.isna(sma20_s.iloc[-1]) else "SMA20: N/A",
                f"SMA50: {float(sma50_s.iloc[-1]):.2f}" if not pd.isna(sma50_s.iloc[-1]) else "SMA50: N/A",
                f"BB Upper: {float(upper_s.iloc[-1]):.2f}" if not pd.isna(upper_s.iloc[-1]) else "BB Upper: N/A",
                f"BB Lower: {float(lower_s.iloc[-1]):.2f}" if not pd.isna(lower_s.iloc[-1]) else "BB Lower: N/A",
                f"ATR(14): {float(atr_s.iloc[-1]):.2f}" if not pd.isna(atr_s.iloc[-1]) else "ATR(14): N/A",
                f"Stochastic: {float(stoch_s.iloc[-1]):.1f}" if not pd.isna(stoch_s.iloc[-1]) else "Stochastic: N/A",
            ]
            indicator_block = "\n".join(ind_lines)
        except Exception as e:
            add_log(user_id, f"Failed to compute indicators: {str(e)}", "ERROR")
            indicator_block = "(indicator data unavailable)"

        backup_prompt = f"""You are a quant trader. Decide if there is a trade opportunity based on the candle data and indicators below.

Symbol: {symbol} ({tf})
Total candles loaded: {len(market_data)}

--- COMPUTED INDICATORS ---
{indicator_block}

--- RECENT 50 CANDLES ---
{candle_block}

Strategy: {prompt_text}
{error_section}

CRITICAL SL/TP RULES:
- Stop loss MUST be within 1.0x to 1.5x ATR distance from entry (ATR(14) = {atr_value:.2f}), max 30 points.
- Max stop distance = {min(atr_value * 1.5, 30):.1f} points from entry.
- Take profit must give at least 1:1.5 RR (TP distance >= 1.5x SL distance).
- If you cannot set SL within this range, output NO_SETUP.

Output ONLY one of the following (no code, no explanation outside the JSON):

1. TRADE_SETUP JSON:
```json
{{"action":"TRADE_SETUP","symbol":"{symbol}","direction":"BUY","order_type":"market","entry_price":0.0,"stop_loss":0.0,"take_profit":0.0,"lot_size":{lot_size},"reasoning":"Brief explanation","confidence":75}}
```

2. NO_SETUP"""

        add_log(user_id, "Backup: sending 50 candles + indicators to providers...", "INFO")

        for p_idx, retry_p in enumerate(_retry_providers):
            fallback_response, _last_usage = await _call_ai_with_retry(
                messages=[{"role": "user", "content": backup_prompt}],
                provider=retry_p,
                model=PROVIDERS[retry_p]["models"][0],
                max_retries=2,
                stage="backup",
            )
            if not fallback_response:
                add_log(user_id, f"Backup {retry_p} returned empty, skipping")
                continue
            ai_response = fallback_response
            _source = "backup"

            # Parse TRADE_SETUP JSON
            jm = re.search(r'```json\n?(.*?)```', fallback_response, re.DOTALL)
            if jm:
                try:
                    setup = json.loads(jm.group(1))
                    add_log(user_id, f"Backup TRADE_SETUP found via {retry_p} (conf={setup.get('confidence')}%)")
                    break
                except json.JSONDecodeError:
                    pass

            if not setup:
                for line in fallback_response.strip().split("\n"):
                    try:
                        obj = json.loads(line.strip())
                        if isinstance(obj, dict) and obj.get("action") == "TRADE_SETUP":
                            setup = obj
                            break
                    except json.JSONDecodeError:
                        pass
                if setup:
                    add_log(user_id, f"Backup TRADE_SETUP found via {retry_p} (conf={setup.get('confidence')}%)")
                    break

            if "NO_SETUP" in fallback_response:
                add_log(user_id, f"Backup {retry_p}: NO_SETUP, trying next provider", "WARNING")
                continue

            add_log(user_id, f"Backup {retry_p}: unclear response, trying next provider", "WARNING")

        if not setup:
            add_log(user_id, "All backup providers: NO_SETUP or failed", "WARNING")
            state["stats"]["skipped_count"] += 1
            return

    direction = setup.get("direction", "BUY").upper()
    order_type = setup.get("order_type", "market").lower()
    entry_price = setup.get("entry_price")
    sl = setup.get("stop_loss")
    tp = setup.get("take_profit")
    lot = setup.get("lot_size", lot_size)
    # Clamp lot to at least the configured default (MT5 rejects below 0.01)
    if lot < lot_size:
        add_log(user_id, f"Lot {lot} below minimum {lot_size}, adjusted to {lot_size}", "WARNING")
        lot = lot_size
    reasoning = setup.get("reasoning", "")
    confidence = setup.get("confidence", 70)

    # ── SL/TP post-processing: clamp unreasonably wide stops ──
    if entry_price and sl and atr_value and atr_value > 0:
        sl_dist = abs(entry_price - sl)
        max_sl_by_atr = atr_value * 1.5
        if "XAU" in symbol or "GOLD" in symbol:
            hard_cap = 30.0
        elif "JPY" in symbol:
            hard_cap = 0.50
        elif "/" in symbol:
            hard_cap = 0.0050
        else:
            hard_cap = max_sl_by_atr
        max_sl_dist = min(max_sl_by_atr, hard_cap)
        if sl_dist > max_sl_dist:
            if direction == "BUY":
                sl = entry_price - max_sl_dist
            else:
                sl = entry_price + max_sl_dist
            add_log(user_id, f"SL clamped from {sl_dist:.2f}pts to {max_sl_dist:.2f}pts (1.5x ATR={max_sl_by_atr:.2f}, hard cap={hard_cap})", "WARNING")
    if entry_price and tp and sl and atr_value and atr_value > 0:
        tp_dist = abs(entry_price - tp)
        sl_dist = abs(entry_price - sl)
        min_tp_dist = sl_dist * 1.5
        if tp_dist < min_tp_dist:
            if direction == "BUY":
                tp = entry_price + min_tp_dist
            else:
                tp = entry_price - min_tp_dist
            add_log(user_id, f"TP adjusted from {tp_dist:.2f}pts to {min_tp_dist:.2f}pts (min 1.5x SL={sl_dist:.2f})", "WARNING")

    add_log(user_id, f"TRADE SETUP - {direction} ({order_type}) | Entry: {entry_price} SL: {sl} TP: {tp} Lot: {lot} Confidence: {confidence}%")

    result = await execute_trade(user_id, symbol, direction, lot, entry_price, sl, tp, prompt_num=prompt_num, order_type=order_type)
    state["last_trade_time"] = datetime.now(timezone.utc)

    if result.get("success"):
        state["last_error_feedback"] = None
        ticket = result.get("ticket")
        exec_price = result.get("price")
        add_log(user_id, f"Trade executed - Ticket #{ticket} Price: {exec_price}", "SUCCESS")
        state["stats"]["trades_executed"] += 1
        state["stats"]["daily_trade_count"] += 1

        async with AsyncSessionLocal() as db:
            trade = AutopilotTrade(
                user_id=user_id, prompt_number=prompt_num, prompt_text=prompt_text,
                symbol=symbol, direction=direction, order_type=order_type, entry_price=entry_price,
                stop_loss=sl, take_profit=tp, lot_size=lot,
                mt5_ticket=ticket, execution_price=exec_price, execution_status="executed",
                reasoning=reasoning, confidence=confidence, ai_response=ai_response,
                raw_thinking=full_raw_response,
                market_regime=market_regime.get("regime"),
                regime_details=market_regime,
                prompt_tags=decision_context.get("selected_tags"),
                decision_score=decision_context.get("selected_score"),
                decision_context=decision_context,
                provider=(_last_usage.get("provider") if _last_usage else None),
                model=(_last_usage.get("model") if _last_usage else None),
                prompt_tokens=(_last_usage.get("prompt_tokens") if _last_usage else None),
                completion_tokens=(_last_usage.get("completion_tokens") if _last_usage else None),
                total_tokens=(_last_usage.get("total_tokens") if _last_usage else None),
                source=_source,
                call_count=_call_count if _call_count > 0 else None,
                call_tokens=_call_tokens if _call_tokens > 0 else None,
                cycle_number=state["stats"]["total_runs"],
            )
            db.add(trade)
            await db.commit()
    else:
        error_msg = result.get('error', 'Unknown error')
        add_log(user_id, f"Trade failed: {error_msg}", "ERROR")
        state["last_error_feedback"] = (
            f"OrderType={order_type}, Direction={direction}, Entry={entry_price}, SL={sl}, TP={tp}, Lot={lot}. "
            f"Error: {error_msg}"
        )
        state["stats"]["error_count"] += 1


async def _is_market_open() -> bool:
    """Check if XAUUSD market is currently open.
    XAUUSD: Sunday 23:00 UTC → Friday 22:00 UTC. Saturday fully closed.
    """
    now = datetime.now(timezone.utc)
    wd = now.weekday()
    hour = now.hour
    if wd == 5:  # Saturday
        return False
    if wd == 4 and hour >= 22:  # Friday after 22:00 UTC
        return False
    if wd == 6 and hour < 23:  # Sunday before 23:00 UTC
        return False
    return True


async def sync_all_trades_from_mt5(user_id: int, hours: int = 720):
    """Full back-sync: fetch ALL MT5 history, match by comment, create missing local records."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AutopilotSettings).where(AutopilotSettings.user_id == user_id))
            settings_obj = result.scalar_one_or_none()
            if not settings_obj:
                return
            if not connector_client.configured:
                return

            history_data = await connector_client.get_history(hours=hours)
            if not history_data or not history_data.get("success"):
                add_log(user_id, "Full sync: history fetch failed", "WARNING")
                return
            deals = history_data.get("deals", [])
            if not deals:
                return

            # Group by position_id into open/close pairs
            positions = {}
            for d in deals:
                pid = d.get("position_id")
                if pid:
                    positions.setdefault(pid, {})[d["entry"]] = d

            # Get all existing local tickets
            existing = await db.execute(
                select(AutopilotTrade.mt5_ticket).where(AutopilotTrade.user_id == user_id)
            )
            existing_tickets = {row[0] for row in existing if row[0]}

            created = 0
            updated = 0
            for pid, pair in positions.items():
                close = pair.get("CLOSE")
                opn = pair.get("OPEN")
                if not close or not opn:
                    continue
                ticket = opn.get("ticket") or close.get("ticket")
                comment = opn.get("comment", "") or close.get("comment", "")
                m = re.search(r"\[AUTOPILOT\]\s*(?:Custom-)?P?(\d+)", comment)
                if not m:
                    continue
                prompt_num = int(m.group(1))

                # Try to match existing local trade by ticket or position_id
                existing_trade = None
                result_set = await db.execute(
                    select(AutopilotTrade).where(
                        AutopilotTrade.user_id == user_id,
                        AutopilotTrade.mt5_ticket == ticket,
                    )
                )
                existing_trade = result_set.scalar_one_or_none()

                profit = close.get("profit", 0) or 0
                exit_price = close.get("price")
                closed_at_str = close.get("time")
                volume = opn.get("volume", 0) or 0
                symbol = opn.get("symbol", "")
                entry_price = opn.get("price", 0) or 0
                direction = opn.get("direction", "BUY").upper()
                entry_time_str = opn.get("time")
                close_comment = (close.get("comment", "") or "").lower()

                res_type = "MANUAL_CLOSE"
                if "sl" in close_comment:
                    res_type = "SL_HIT"
                elif "tp" in close_comment:
                    res_type = "TP_HIT"
                elif profit > 0:
                    res_type = "PROFIT"
                else:
                    res_type = "LOSS"

                def _parse_ts(s):
                    if not s:
                        return None
                    return datetime.strptime(s, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)

                if existing_trade:
                    if existing_trade.result is None or existing_trade.profit is None:
                        existing_trade.profit = profit
                        existing_trade.exit_price = exit_price
                        existing_trade.result = res_type
                        existing_trade.closed_at = _parse_ts(closed_at_str)
                        if existing_trade.executed_at and existing_trade.closed_at:
                            diff = existing_trade.closed_at - existing_trade.executed_at
                            existing_trade.duration_minutes = int(diff.total_seconds() / 60)
                        updated += 1
                else:
                    executed_at = _parse_ts(entry_time_str)
                    closed_at = _parse_ts(closed_at_str)
                    duration = int((closed_at - executed_at).total_seconds() / 60) if executed_at and closed_at else None
                    new_trade = AutopilotTrade(
                        user_id=user_id,
                        prompt_number=prompt_num,
                        prompt_text=f"(synced from MT5 - P#{prompt_num})",
                        symbol=symbol,
                        direction=direction,
                        entry_price=entry_price,
                        lot_size=volume,
                        order_type="market",
                        mt5_ticket=ticket,
                        executed_at=executed_at,
                        execution_price=entry_price,
                        execution_status="executed",
                        result=res_type,
                        profit=profit,
                        exit_price=exit_price,
                        closed_at=closed_at,
                        duration_minutes=duration,
                        source="mt5_sync",
                    )
                    db.add(new_trade)
                    created += 1

            if created > 0 or updated > 0:
                await db.commit()
                add_log(user_id, f"Full sync: {created} created, {updated} updated from MT5 history", "INFO")

    except Exception as e:
        add_log(user_id, f"Full sync failed: {str(e)}", "ERROR")


async def sync_trade_results(user_id: int):
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AutopilotSettings).where(AutopilotSettings.user_id == user_id))
            settings_obj = result.scalar_one_or_none()
            if not settings_obj:
                return
            result = await db.execute(
                select(AutopilotTrade).where(AutopilotTrade.user_id == user_id)
                .where(AutopilotTrade.execution_status == "executed").where(AutopilotTrade.result == None)
            )
            open_trades = result.scalars().all()
            if not open_trades:
                return

            # Dynamically determine the history window to check based on the oldest open trade
            try:
                oldest_trade = min(open_trades, key=lambda t: t.executed_at)
                executed_at = _ensure_aware(oldest_trade.executed_at)
                now_utc = datetime.now(timezone.utc)
                hours_diff = int((now_utc - executed_at).total_seconds() / 3600) + 12  # add 12h buffer
                sync_hours = max(hours_diff, 24)
            except Exception:
                sync_hours = 24

            try:
                history_data = await connector_client.get_history(hours=sync_hours)
            except Exception as e:
                add_log(user_id, f"History fetch failed: {str(e)}", "ERROR")
                return

            if not history_data.get("success"):
                return
            deals = history_data.get("deals", [])
            if not deals:
                return

            updated_count = 0
            state = _get_state(user_id)
            for trade in open_trades:
                close_deal = None
                for deal in deals:
                    is_match = (deal.get("position_id") == trade.mt5_ticket or deal.get("ticket") == trade.mt5_ticket)
                    if is_match and deal.get("entry") == "CLOSE":
                        close_deal = deal
                        break
                if close_deal:
                    profit = close_deal.get("profit", 0)
                    exit_price = close_deal.get("price")
                    closed_at_str = close_deal.get("time")
                    comment = close_deal.get("comment", "").lower()
                    res_type = "MANUAL_CLOSE"
                    if "sl" in comment:
                        res_type = "SL_HIT"
                    elif "tp" in comment:
                        res_type = "TP_HIT"
                    elif profit > 0:
                        res_type = "PROFIT"
                    else:
                        res_type = "LOSS"

                    trade.profit = profit
                    trade.exit_price = exit_price
                    trade.result = res_type
                    state["stats"]["daily_pnl"] = state["stats"].get("daily_pnl", 0) + profit
                    if closed_at_str:
                        trade.closed_at = _ensure_aware(datetime.strptime(closed_at_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc))
                        trade.executed_at = _ensure_aware(trade.executed_at)
                        if trade.executed_at and trade.closed_at:
                            diff = trade.closed_at - trade.executed_at
                            trade.duration_minutes = int(diff.total_seconds() / 60)
                    updated_count += 1
                    add_log(user_id, f"Trade #{trade.mt5_ticket} | Profit: ${profit:.2f} | {res_type}", "SUCCESS" if profit > 0 else "WARNING")

            if updated_count > 0:
                await db.commit()

    except Exception as e:
        add_log(user_id, f"Failed to sync trade results: {str(e)}", "ERROR")


async def autopilot_loop(user_id: int):
    state = _get_state(user_id)
    await _rebuild_daily_state(user_id)
    await _rebuild_stats(user_id)
    # Full back-sync from MT5 history at startup (captures trades that were missed)
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AutopilotSettings).where(AutopilotSettings.user_id == user_id))
            s = result.scalar_one_or_none()
            if s and connector_client.configured:
                await sync_all_trades_from_mt5(user_id)
    except Exception:
        pass
    try:
        while state["enabled"]:
            if state["running"]:
                state["stats"]["daily_pnl"] = state["stats"].get("daily_pnl", 0)
                max_loss = None
                cooldown_mins = 0
                async with AsyncSessionLocal() as db:
                    result = await db.execute(select(AutopilotSettings).where(AutopilotSettings.user_id == user_id))
                    s = result.scalar_one_or_none()
                    if s:
                        max_loss = s.max_daily_loss
                        cooldown_mins = s.cooldown_minutes or 0

                # Cooldown check
                last_trade = state.get("last_trade_time")
                if cooldown_mins > 0 and last_trade:
                    elapsed_mins = (datetime.now(timezone.utc) - last_trade).total_seconds() / 60
                    if elapsed_mins < cooldown_mins:
                        add_log(user_id, f"Cooldown ({elapsed_mins:.0f}/{cooldown_mins} min). Skipping cycle.", "INFO")
                        state["stats"]["skipped_count"] += 1
                        async with AsyncSessionLocal() as db:
                            result = await db.execute(select(AutopilotSettings).where(AutopilotSettings.user_id == user_id))
                            s = result.scalar_one_or_none()
                            interval = s.interval_seconds if s else 300
                        if not state["enabled"]:
                            break
                        await asyncio.sleep(interval)
                        continue

                hit_loss_limit = False
                if max_loss is not None and state["stats"]["daily_pnl"] <= max_loss:
                    add_log(user_id, f"Daily loss limit (${max_loss}) reached. Stopping.", "WARNING")
                    state["running"] = False
                    hit_loss_limit = True

                # Market open check — skip if XAUUSD is closed (weekend)
                if not await _is_market_open():
                    add_log(user_id, "Market closed (Sat/Sun). Skipping until Sunday 23:00 UTC.", "INFO")
                    state["stats"]["skipped_count"] += 1
                    await asyncio.sleep(3600)  # re-check every hour
                    continue

                await sync_trade_results(user_id)
                if not hit_loss_limit:
                    await run_autopilot_cycle(user_id)

            async with AsyncSessionLocal() as db:
                result = await db.execute(select(AutopilotSettings).where(AutopilotSettings.user_id == user_id))
                s = result.scalar_one_or_none()
                interval = s.interval_seconds if s else 300
            # Only sleep if we're still enabled — skip sleep if stop was requested mid-cycle
            if not state["enabled"]:
                break
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        add_log(user_id, "Autopilot loop cancelled.", "INFO")


# Pydantic models
class AutopilotConfig(BaseModel):
    enabled: bool = False
    interval_seconds: int = 300
    default_lot: float = 0.10
    max_trades_per_day: int = 10
    cooldown_minutes: int = 5
    max_daily_loss: float = -50.0
    symbol: str = "XAUUSD"
    provider: str = "nvidia"
    model: str = "qwen/qwen3.5-122b-a10b"
    selected_prompts: Optional[List] = None


class AutopilotStatus(BaseModel):
    enabled: bool
    running: bool
    settings: Optional[AutopilotConfig] = None
    stats: dict
    logs: List[dict]


class AutopilotStats(BaseModel):
    total_runs: int
    trades_executed: int
    skipped_count: int
    error_count: int
    last_run: Optional[str] = None


class UserPromptCreate(BaseModel):
    content: str


class UserPromptUpdate(BaseModel):
    content: str


class PromptResponse(BaseModel):
    id: str  # e.g., "1" or "custom_1"
    text: str
    is_custom: bool


class PromptStatus(BaseModel):
    default_prompts: List[PromptResponse]
    personal_prompts: List[PromptResponse]
    selected_ids: List


class TradeResult(BaseModel):
    id: int
    prompt_number: int
    prompt_text: str
    symbol: str
    direction: str
    entry_price: Optional[float]
    exit_price: Optional[float] = None
    stop_loss: Optional[float]
    take_profit: Optional[float]
    lot_size: float
    mt5_ticket: Optional[int]
    executed_at: str
    result: Optional[str]
    profit: Optional[float]
    closed_at: Optional[str]
    reasoning: Optional[str]
    confidence: Optional[float]
    market_regime: Optional[str] = None
    decision_score: Optional[float] = None
    prompt_tags: Optional[dict] = None


class LogEntry(BaseModel):
    id: int
    timestamp: str
    level: str
    message: str
    cycle_number: Optional[int] = None


class LogsResponse(BaseModel):
    logs: List[LogEntry]
    total: int
    page: int
    per_page: int
    has_next: bool


class PromptStatsItem(BaseModel):
    prompt_number: int
    prompt_text: str
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_profit: float
    avg_profit: float
    display_name: str = ""


# ── Internal helper: start autopilot without HTTP auth ────────────────────
async def _start_autopilot_internal(user_id: int) -> bool:
    """Start autopilot for a given user_id. Used for auto-restart on server boot."""
    state = _get_state(user_id)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(AutopilotSettings).where(AutopilotSettings.user_id == user_id))
        settings_obj = result.scalar_one_or_none()
        if not settings_obj or not settings_obj.enabled:
            return False
    if user_id not in _user_locks:
        _user_locks[user_id] = asyncio.Lock()
    async with _user_locks[user_id]:
        state["enabled"] = True
        state["running"] = True
        if state["task"] is None or state["task"].done():
            state["task"] = asyncio.create_task(autopilot_loop(user_id))
    add_log(user_id, "Autopilot auto-restarted after server boot")
    return True


# Endpoints
@router.post("/start")
async def start_autopilot(current_user: dict = Depends(require_trader)):
    user_id = current_user["id"]
    state = _get_state(user_id)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(AutopilotSettings).where(AutopilotSettings.user_id == user_id))
        settings_obj = result.scalar_one_or_none()
        if not settings_obj:
            settings_obj = AutopilotSettings(user_id=user_id)
            db.add(settings_obj)
            await db.commit()
        if not settings_obj.enabled:
            settings_obj.enabled = True
            await db.commit()

    if user_id not in _user_locks:
        _user_locks[user_id] = asyncio.Lock()

    async with _user_locks[user_id]:
        state["enabled"] = True
        state["running"] = True
        if state["task"] is None or state["task"].done():
            state["task"] = asyncio.create_task(autopilot_loop(user_id))

    add_log(user_id, "Autopilot STARTED")
    return {"success": True, "message": "Autopilot started"}


@router.post("/stop")
async def stop_autopilot(current_user: dict = Depends(require_trader)):
    user_id = current_user["id"]
    state = _get_state(user_id)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(AutopilotSettings).where(AutopilotSettings.user_id == user_id))
        s = result.scalar_one_or_none()
        if s:
            s.enabled = False
            await db.commit()
    state["enabled"] = False
    state["running"] = False
    # Cancel the background asyncio task so it cleanly exits the loop
    existing_task = state.get("task")
    if existing_task and not existing_task.done():
        existing_task.cancel()
        try:
            await existing_task
        except asyncio.CancelledError:
            pass
    state["task"] = None
    add_log(user_id, "Autopilot STOPPED")
    return {"success": True, "message": "Autopilot stopped"}


@router.get("/status", response_model=AutopilotStatus)
async def get_status(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    state = _get_state(user_id)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(AutopilotSettings).where(AutopilotSettings.user_id == user_id))
        settings_obj = result.scalar_one_or_none()
        settings = None
        if settings_obj:
            settings = {
                "enabled": settings_obj.enabled, "interval_seconds": settings_obj.interval_seconds,
                "default_lot": settings_obj.default_lot, "max_trades_per_day": settings_obj.max_trades_per_day,
                "cooldown_minutes": settings_obj.cooldown_minutes, "max_daily_loss": settings_obj.max_daily_loss,
                "symbol": settings_obj.symbol, "provider": settings_obj.provider, "model": settings_obj.model,
                "mt5_connected": settings_obj.mt5_connected,
                "selected_prompts": settings_obj.selected_prompts or []
            }
    return AutopilotStatus(enabled=state["enabled"], running=state["running"], settings=settings, stats=state["stats"], logs=state["logs"])


@router.post("/connect-mt5")
async def connect_mt5(current_user: dict = Depends(require_trader)):
    """Connect to MT5 through the server's connector."""
    user_id = current_user["id"]
    add_log(user_id, "Connecting to MT5...")
    if not await initialize_mt5_connector(user_id):
        return {"success": False, "message": "Failed to connect to MT5"}

    async with AsyncSessionLocal() as db:
        settings_obj = (await db.execute(
            select(AutopilotSettings).where(AutopilotSettings.user_id == user_id)
        )).scalar_one_or_none()
        if not settings_obj:
            settings_obj = AutopilotSettings(user_id=user_id)
            db.add(settings_obj)
        settings_obj.mt5_connected = True
        await db.commit()
    return {"success": True, "message": "Connected to MT5 successfully"}


@router.post("/settings")
async def save_settings(
    config: AutopilotConfig,
    current_user: dict = Depends(require_trader)
):
    """Save autopilot settings."""
    user_id = current_user["id"]

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AutopilotSettings).where(AutopilotSettings.user_id == user_id)
        )
        settings_obj = result.scalar_one_or_none()

        if not settings_obj:
            settings_obj = AutopilotSettings(user_id=user_id)
            db.add(settings_obj)

        settings_obj.interval_seconds = config.interval_seconds
        settings_obj.default_lot = config.default_lot
        settings_obj.max_trades_per_day = config.max_trades_per_day
        settings_obj.cooldown_minutes = config.cooldown_minutes
        settings_obj.max_daily_loss = config.max_daily_loss
        settings_obj.symbol = config.symbol
        settings_obj.provider = config.provider
        settings_obj.model = config.model
        settings_obj.selected_prompts = config.selected_prompts

        await db.commit()

    state = _get_state(user_id)
    state["settings"] = config.model_dump()
    return {"success": True}


@router.get("/prompts", response_model=PromptStatus)
async def get_prompts(current_user: dict = Depends(get_current_user)):
    """Get all available prompts and current selection."""
    user_id = current_user["id"]
    
    # 1. Load defaults
    defaults_raw = load_prompts()
    defaults = []
    for line in defaults_raw:
        try:
            parts = line.split(".", 1)
            defaults.append(PromptResponse(
                id=parts[0].strip(),
                text=parts[1].strip(),
                is_custom=False
            ))
        except Exception:
            continue
            
    # 2. Load personal from DB
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(UserPrompt).where(UserPrompt.user_id == user_id)
        )
        personal_objs = result.scalars().all()
        personal = [PromptResponse(
            id=f"custom_{p.id}",
            text=p.content,
            is_custom=True
        ) for p in personal_objs]
        
        # 3. Get selected IDs
        result = await db.execute(
            select(AutopilotSettings.selected_prompts).where(AutopilotSettings.user_id == user_id)
        )
        selected_ids = result.scalar() or []
        
    return PromptStatus(
        default_prompts=defaults,
        personal_prompts=personal,
        selected_ids=selected_ids
    )


@router.post("/prompts")
async def create_prompt(data: UserPromptCreate, current_user: dict = Depends(require_trader)):
    """Create a personal prompt."""
    user_id = current_user["id"]
    async with AsyncSessionLocal() as db:
        new_p = UserPrompt(user_id=user_id, content=data.content)
        db.add(new_p)
        await db.commit()
        await db.refresh(new_p)
        return {"success": True, "id": f"custom_{new_p.id}"}


@router.put("/prompts/{prompt_id}")
async def update_prompt(prompt_id: str, data: UserPromptUpdate, current_user: dict = Depends(require_trader)):
    """Update a personal prompt."""
    user_id = current_user["id"]
    if not prompt_id.startswith("custom_"):
        raise HTTPException(status_code=400, detail="Cannot edit default prompts")
        
    db_id = int(prompt_id.split("_")[1])
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(UserPrompt).where(UserPrompt.id == db_id, UserPrompt.user_id == user_id)
        )
        prompt = result.scalar_one_or_none()
        if not prompt:
            raise HTTPException(status_code=404, detail="Prompt not found")
            
        prompt.content = data.content
        await db.commit()
        return {"success": True}


@router.delete("/prompts/{prompt_id}")
async def delete_prompt(prompt_id: str, current_user: dict = Depends(require_trader)):
    """Delete a personal prompt."""
    user_id = current_user["id"]
    if not prompt_id.startswith("custom_"):
        raise HTTPException(status_code=400, detail="Cannot delete default prompts")
        
    db_id = int(prompt_id.split("_")[1])
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(UserPrompt).where(UserPrompt.id == db_id, UserPrompt.user_id == user_id)
        )
        prompt = result.scalar_one_or_none()
        if not prompt:
            raise HTTPException(status_code=404, detail="Prompt not found")
            
        await db.delete(prompt)
        
        # Also remove from selected_prompts if present
        result = await db.execute(
            select(AutopilotSettings).where(AutopilotSettings.user_id == user_id)
        )
        settings_obj = result.scalar_one_or_none()
        if settings_obj and settings_obj.selected_prompts:
            if prompt_id in settings_obj.selected_prompts:
                new_selected = [s for s in settings_obj.selected_prompts if s != prompt_id]
                settings_obj.selected_prompts = new_selected
                
        await db.commit()
        return {"success": True}


@router.get("/prompt-stats", response_model=List[PromptStatsItem])
async def get_prompt_stats(current_user: dict = Depends(get_current_user)):
    """Get win-rate statistics per prompt."""
    user_id = current_user["id"]

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AutopilotTrade).where(
                AutopilotTrade.user_id == user_id,
                AutopilotTrade.profit.isnot(None)
            ).order_by(AutopilotTrade.executed_at.desc())
        )
        trades = result.scalars().all()

    groups: dict[int, dict] = {}
    for t in trades:
        pn = t.prompt_number
        if pn not in groups:
            groups[pn] = {"prompt_number": pn, "prompt_text": t.prompt_text, "total_trades": 0, "wins": 0, "total_profit": 0.0}
        groups[pn]["total_trades"] += 1
        groups[pn]["total_profit"] += t.profit or 0
        if (t.profit or 0) > 0:
            groups[pn]["wins"] += 1

    stats = []
    for g in groups.values():
        g["losses"] = g["total_trades"] - g["wins"]
        g["win_rate"] = round(g["wins"] / g["total_trades"] * 100, 1) if g["total_trades"] > 0 else 0.0
        g["avg_profit"] = round(g["total_profit"] / g["total_trades"], 2) if g["total_trades"] > 0 else 0.0
        g["total_profit"] = round(g["total_profit"], 2)
        pn = g["prompt_number"]
        g["display_name"] = f"Custom-{abs(pn)}" if pn < 0 else f"#{pn}"
        stats.append(PromptStatsItem(**g))

    stats.sort(key=lambda x: x.total_profit, reverse=True)
    return stats


@router.get("/logs")
async def get_autopilot_logs(
    level: Optional[str] = None,
    cycle_number: Optional[int] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    page: int = 1,
    per_page: int = 50,
    current_user: dict = Depends(get_current_user),
):
    """Get autopilot logs from DB with filters and pagination."""
    user_id = current_user["id"]
    per_page = min(per_page, 200)

    async with AsyncSessionLocal() as db:
        query = select(AutopilotLog).where(AutopilotLog.user_id == user_id)

        if level:
            query = query.where(AutopilotLog.level == level.upper())
        if cycle_number is not None:
            query = query.where(AutopilotLog.cycle_number == cycle_number)
        if from_date:
            try:
                fd = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                query = query.where(AutopilotLog.timestamp >= fd)
            except ValueError:
                pass
        if to_date:
            try:
                td = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
                query = query.where(AutopilotLog.timestamp < td)
            except ValueError:
                pass

        total_result = await db.execute(select(func.count()).select_from(query.subquery()))
        total = total_result.scalar() or 0

        query = query.order_by(AutopilotLog.timestamp.desc())
        query = query.offset((page - 1) * per_page).limit(per_page)
        result = await db.execute(query)
        rows = result.scalars().all()

        logs = [
            LogEntry(
                id=r.id,
                timestamp=r.timestamp.strftime("%Y-%m-%d %H:%M:%S") if r.timestamp else "",
                level=r.level,
                message=r.message,
                cycle_number=r.cycle_number,
            )
            for r in rows
        ]

    return LogsResponse(
        logs=logs,
        total=total,
        page=page,
        per_page=per_page,
        has_next=(page * per_page) < total,
    )


@router.get("/results", response_model=List[TradeResult])
async def get_trade_results(
    skip: int = 0,
    limit: int = 50,
    prompt_number: Optional[int] = None,
    prompt_text: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Get trade results history."""
    limit = min(limit, 500)
    user_id = current_user["id"]

    async with AsyncSessionLocal() as db:
        query = (
            select(AutopilotTrade)
            .where(AutopilotTrade.user_id == user_id)
            .order_by(AutopilotTrade.executed_at.desc())
        )
        if prompt_number is not None:
            query = query.where(AutopilotTrade.prompt_number == prompt_number)
        if prompt_text is not None:
            query = query.where(AutopilotTrade.prompt_text == prompt_text)
        result = await db.execute(query.offset(skip).limit(limit))
        trades = result.scalars().all()

        return [
            TradeResult(
                id=t.id,
                prompt_number=t.prompt_number,
                prompt_text=t.prompt_text,
                symbol=t.symbol,
                direction=t.direction,
                entry_price=t.entry_price,
                exit_price=t.exit_price,
                stop_loss=t.stop_loss,
                take_profit=t.take_profit,
                lot_size=t.lot_size,
                mt5_ticket=t.mt5_ticket,
                executed_at=t.executed_at.isoformat() if t.executed_at else "",
                result=t.result,
                profit=t.profit,
                closed_at=t.closed_at.isoformat() if t.closed_at else None,
                reasoning=t.reasoning,
                confidence=t.confidence,
                market_regime=t.market_regime,
                decision_score=t.decision_score,
                prompt_tags=t.prompt_tags,
            )
            for t in trades
        ]


@router.get("/results/export")
async def export_trades_csv(current_user: dict = Depends(get_current_user)):
    """Export trade history as CSV."""
    import io, csv
    user_id = current_user["id"]

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AutopilotTrade)
            .where(AutopilotTrade.user_id == user_id)
            .order_by(AutopilotTrade.executed_at.desc())
        )
        trades = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Prompt #", "Prompt Text", "Symbol", "Direction", "Entry Price",
                      "Stop Loss", "Take Profit", "Lot Size", "Ticket", "Executed At",
                      "Result", "Profit", "Closed At", "Duration (min)", "Reasoning", "Confidence"])
    for t in trades:
        writer.writerow([
            t.id, t.prompt_number, t.prompt_text, t.symbol, t.direction,
            t.entry_price or "", t.stop_loss or "", t.take_profit or "",
            t.lot_size, t.mt5_ticket or "",
            t.executed_at.isoformat() if t.executed_at else "",
            t.result or "", t.profit or "",
            t.closed_at.isoformat() if t.closed_at else "",
            t.duration_minutes or "", t.reasoning or "", t.confidence or ""
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=autopilot_trades.csv"}
    )
