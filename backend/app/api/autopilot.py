"""
Autopilot API - Automatic trading based on AI analysis
"""

import logging
import hashlib
import uuid

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
import time
from sqlalchemy import select, func, or_, and_
from openai import AsyncOpenAI
import pandas as pd
import ta
import numpy as np

from ..core.config import settings
from ..core.security import get_current_user, require_trader
from ..core.database import AsyncSessionLocal
from ..core.providers import PROVIDERS, get_api_key as _get_api_key, get_base_url, resolve_api_key, resolve_all_api_keys
from ..core.models_cache import get_live_models as _get_live_models
from ..models.ai_memory import AutopilotTrade, AutopilotSettings, UserPrompt, AutopilotLog, ModelUsage, AiCallLog, AutopilotExecutionAttempt, AutopilotCycle, AutopilotOrderEvent
from ..core.providers import estimate_cost
from ..core.mt5_connector import ConnectorError, connector_client
from ..core.risk import RiskRefused, submit_order, trading_halted
from ..core.trade_outcome import close_result

router = APIRouter(prefix="/autopilot", tags=["Autopilot"])

logger = logging.getLogger("autopilot")

def _capture_raw_response(response) -> dict | None:
    try:
        return response.model_dump(mode='json')
    except Exception:  # swallow-ok: older SDK objects; .dict() is tried next
        try:
            return response.dict()
        except Exception:  # swallow-ok: the raw copy is only kept for debugging
            return None

# How an AI provider's error is told apart. A model problem is checked first: Gemini
# answers an unusable model with 400 INVALID_ARGUMENT, and the word "invalid" used to
# make it look like a refused key, so the same broken model was retried every cycle.
_MODEL_UNUSABLE = ("only supports interactions api", "model_not_supported", "model not supported",
                   "is not supported", "404", "not found", "does not exist", "unknown model")
# Rate limited or overloaded: worth waiting for, not worth asking again at once.
_BUSY = ("429", "too_many_requests", "queue_exceeded", "resource_exhausted",
         "503", "unavailable", "overloaded", "high demand")
_KEY_REFUSED = ("api key not valid", "api_key_invalid", "invalid api key", "invalid_api_key",
                "incorrect api key", "unauthorized", "401", "expired")

_user_states: Dict[int, dict] = {}
_user_locks: Dict[int, asyncio.Lock] = {}
_trade_sync_locks: Dict[int, asyncio.Lock] = {}

def _get_state(user_id: int) -> dict:
    if user_id not in _user_states:
        _user_states[user_id] = {
            "enabled": False,
            "running": False,
            "logs": [],
            "task": None,
            "active_cycle_id": None,
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


async def _daily_totals(user_id: int) -> tuple[int, float]:
    """Today's autopilot trades opened, and profit from trades closed, in UTC. Read from the database.

    These used to be counters in memory, rebuilt at start-up from trades opened
    today but added to during the day from trades closed today, so a trade could
    count on the wrong day, and closes found by the start-up back-sync were never
    counted at all.
    """
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today_start + timedelta(days=1)
    async with AsyncSessionLocal() as db:
        opened = (await db.execute(select(func.count(AutopilotTrade.id)).where(
            AutopilotTrade.user_id == user_id,
            AutopilotTrade.executed_at >= today_start, AutopilotTrade.executed_at < tomorrow,
        ))).scalar_one()
        closed_pnl = (await db.execute(select(func.coalesce(func.sum(AutopilotTrade.profit), 0)).where(
            AutopilotTrade.user_id == user_id, AutopilotTrade.result.is_not(None),
            AutopilotTrade.closed_at >= today_start, AutopilotTrade.closed_at < tomorrow,
        ))).scalar_one()
    return int(opened), round(float(closed_pnl), 2)


async def _refresh_daily_stats(user_id: int) -> tuple[int, float]:
    state = _get_state(user_id)
    count, pnl = await _daily_totals(user_id)
    state["stats"]["daily_trade_count"] = count
    state["stats"]["daily_pnl"] = pnl
    state["stats"]["daily_reset_date"] = datetime.now(timezone.utc).date().isoformat()
    return count, pnl


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

            # Count terminal attempt outcomes from the durable ledger. Log text
            # is best-effort telemetry and must not define persisted totals.
            skipped_outcomes = (
                "daily_trade_limit", "daily_loss_limit", "skipped_cooldown",
                "skipped_no_connector", "skipped_stale_market_data", "no_setup",
                "no_eligible_prompts", "not_configured",
            )
            result = await db.execute(
                select(func.count(AutopilotCycle.cycle_id)).where(
                    AutopilotCycle.user_id == user_id,
                    AutopilotCycle.outcome.in_(skipped_outcomes),
                )
            )
            state["stats"]["skipped_count"] = result.scalar() or 0

            error_outcomes = (
                "no_market_data", "ai_generation_failed", "ai_provider_or_response_failed",
                "execution_rejected", "cycle_crashed", "mt5_connection_failed",
            )
            result = await db.execute(
                select(func.count(AutopilotCycle.cycle_id)).where(
                    AutopilotCycle.user_id == user_id,
                    AutopilotCycle.outcome.in_(error_outcomes),
                )
            )
            state["stats"]["error_count"] = result.scalar() or 0

            # Total runs = sum of all three (every cycle ends as trade, skip, or error)
            state["stats"]["total_runs"] = (
                state["stats"]["trades_executed"] +
                state["stats"]["skipped_count"] +
                state["stats"]["error_count"]
            )
            latest_cycle_number = (await db.execute(
                select(func.max(AutopilotCycle.cycle_number)).where(AutopilotCycle.user_id == user_id)
            )).scalar()
            state["stats"]["total_runs"] = max(
                state["stats"]["total_runs"], latest_cycle_number or 0
            )
    except Exception:
        logger.warning("[user=%d] Could not restore the autopilot's counters", user_id, exc_info=True)


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
    except OSError:
        logger.exception("Could not read the prompt list %s", PROMPT_FILE)
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
    cycle_id = state.get("active_cycle_id")
    asyncio.create_task(_persist_log(user_id, level, message, cycle_number, cycle_id))


async def _persist_log(user_id: int, level: str, message: str, cycle_number: int | None = None, cycle_id: str | None = None):
    try:
        async with AsyncSessionLocal() as db:
            entry = AutopilotLog(
                user_id=user_id,
                level=level,
                message=message,
                cycle_number=cycle_number,
                cycle_id=cycle_id,
            )
            db.add(entry)
            await db.commit()
    except Exception:
        # Must never crash the caller, and must not call add_log, which called us.
        logger.warning("[user=%d] Could not save an autopilot log line", user_id, exc_info=True)


async def _update_autopilot_cycle(cycle_id: str, **values):
    """Best-effort update of a cycle ledger row; telemetry must not stop trading."""
    try:
        async with AsyncSessionLocal() as db:
            cycle = await db.get(AutopilotCycle, cycle_id)
            if cycle:
                for key, value in values.items():
                    setattr(cycle, key, value)
                await db.commit()
    except Exception as exc:
        logger.warning("Failed to update autopilot cycle telemetry: %s", type(exc).__name__)


async def _add_order_event(db, event: dict) -> bool:
    """Insert one broker lifecycle event unless its stable key was already seen."""
    exists = await db.execute(
        select(AutopilotOrderEvent.id).where(AutopilotOrderEvent.event_key == event["event_key"])
    )
    if exists.scalar_one_or_none() is not None:
        return False
    db.add(AutopilotOrderEvent(**event))
    return True


def _parse_broker_datetime(value):
    if isinstance(value, datetime):
        return _ensure_aware(value)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 10_000_000_000 else value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _classify_exit_reason(close_deals: list[dict], profit: float):
    """Prefer MT5's deal reason; keep comment and P&L fallbacks explicitly labeled."""
    reason_map = {
        "SL": "SL_HIT", "TP": "TP_HIT", "STOP_OUT": "STOP_OUT",
        "CLIENT": "MANUAL_CLOSE", "MOBILE": "MANUAL_CLOSE", "WEB": "MANUAL_CLOSE",
        "EXPERT": "EXPERT_CLOSE", "ROLLOVER": "ROLLOVER", "VMARGIN": "MARGIN_CLOSE", "VARIATION_MARGIN": "MARGIN_CLOSE",
    }
    # Only the broker's own deal reason decides. Guessing from the comment text
    # misread any comment containing "sl" or "tp" (finding 10).
    classifications = []
    unclassified_count = 0
    for deal in close_deals:
        reason = (deal.get("reason") or "UNKNOWN").upper()
        normalized = reason_map.get(reason)
        if normalized:
            classifications.append(normalized)
        else:
            unclassified_count += 1
    distinct = set(classifications)
    if len(distinct) > 1 or (distinct and unclassified_count):
        classification = "MIXED_EXIT"
        source = "mixed"
    elif distinct:
        classification = next(iter(distinct))
        source = "broker"
    else:
        classification = "PROFIT" if profit > 0 else "LOSS"
        source = "unavailable"
    exit_reason = classification if classification not in ("PROFIT", "LOSS") else "UNKNOWN"
    return classification, exit_reason, source


BRIEF_SYSTEM = """You are a disciplined quant trader. The backend measured the market and
shortlisted strategies. Pick the one strategy whose conditions the numbers meet, and give its setup,
or NO_SETUP if none is clearly met. Use only the numbers given. Stop loss required, beyond a nearby
swing, about 1 to 1.5 ATR away. Target at least 1.5 times the stop distance. Reply only in the
requested line format."""


def _brief_user_message(symbol: str, signals: dict, shortlist_items: list[dict]) -> str:
    from ..core.market_signals import brief_text
    menu = [f"STRATEGY {item['prompt']['id']}: {' '.join(item['prompt']['text'].split())}" for item in shortlist_items]
    return (brief_text(symbol, signals) + "\n\n" + "\n".join(menu) + """

Reply with these lines only:
DECISION: TRADE_SETUP or NO_SETUP
STRATEGY: id
DIRECTION: BUY or SELL
ORDER: market, limit or stop
ENTRY: price
STOP: price
TARGET: price
CONFIDENCE: 0-100
REASON: under 30 words, with the numbers
For NO_SETUP only DECISION, STRATEGY, REASON.""")


_FIELD_NAMES = {
    "decision": "decision", "action": "decision",
    "strategy": "strategy", "strategy_id": "strategy", "prompt": "strategy", "prompt_id": "strategy",
    "direction": "direction", "side": "direction",
    "order": "order", "order_type": "order", "type": "order",
    "entry": "entry", "entry_price": "entry",
    "stop": "stop", "stop_loss": "stop", "sl": "stop",
    "target": "target", "take_profit": "target", "tp": "target",
    "confidence": "confidence",
    "reason": "reason", "reasoning": "reason",
}


def _brief_fields(reply: str) -> dict:
    """The answer's fields, from KEY: value lines, or from a JSON object if that is what came back."""
    fields = {}
    match = re.search(r"\{.*\}", reply, re.S)
    if match:
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict):
                for key, value in obj.items():
                    name = _FIELD_NAMES.get(str(key).strip().lower().replace(" ", "_"))
                    if name and value not in (None, ""):
                        fields[name] = str(value)
        except json.JSONDecodeError:
            pass
    # A JSON answer cut off before its closing brace: take the pairs that did arrive.
    for key, value in re.findall(r'"([A-Za-z_ ]+)"\s*:\s*"?([^",}\n]+)', reply):
        name = _FIELD_NAMES.get(key.strip().lower().replace(" ", "_"))
        if name and name not in fields:
            fields[name] = value.strip()
    for line in reply.splitlines():
        m = re.match(r"^[\s*#>\-`]*([A-Za-z][A-Za-z _]*?)[\s*`]*[:=]\s*(.+?)\s*$", line)
        if not m:
            continue
        name = _FIELD_NAMES.get(m.group(1).strip().lower().replace(" ", "_"))
        if name and name not in fields:
            fields[name] = m.group(2).strip().strip("*`\"'")
    return fields


def _number(value) -> float | None:
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(value or "").replace(",", ""))
    return float(m.group(0)) if m else None


def _parse_brief_decision(reply: str, allowed_ids: list[str], price: float) -> dict:
    """The AI's answer to the brief, checked. kind is trade, no_setup or invalid (with a reason).

    Accepts the requested KEY: value lines, a JSON object, or plain text that says
    NO_SETUP. Bold markers, extra words after a number and missing optional lines
    are tolerated, so a model that does not follow the format exactly still works.
    """
    reply = reply or ""
    fields = _brief_fields(reply)
    decision = re.sub(r"[^A-Z]", "", fields.get("decision", "").upper())
    if not decision:
        if re.search(r"\bNO[\s_-]?SETUP\b", reply, re.I):
            decision = "NOSETUP"
        elif re.search(r"\bTRADE[\s_-]?SETUP\b", reply, re.I):
            decision = "TRADESETUP"
    sid_match = re.search(r"[A-Za-z_]*\d+", fields.get("strategy", ""))
    sid = sid_match.group(0).lstrip("#") if sid_match else ""
    if sid.lower().startswith("strategy"):
        sid = sid[len("strategy"):]
    reasoning = fields.get("reason", "")[:2000]

    if decision == "NOSETUP":
        return {"kind": "no_setup", "strategy_id": sid if sid in allowed_ids else None,
                "reasoning": reasoning or " ".join(reply.split())[:300]}
    if decision != "TRADESETUP":
        return {"kind": "invalid", "reason": "no TRADE_SETUP or NO_SETUP decision in the reply"}
    if sid not in allowed_ids:
        return {"kind": "invalid", "reason": f"strategy {sid!r} was not on the shortlist {allowed_ids}"}
    direction = fields.get("direction", "").upper()
    direction = "BUY" if "BUY" in direction or "LONG" in direction else "SELL" if "SELL" in direction or "SHORT" in direction else ""
    order = fields.get("order", "market").lower()
    order_type = "limit" if "limit" in order else "stop" if "stop" in order else "market"
    sl, tp, entry = _number(fields.get("stop")), _number(fields.get("target")), _number(fields.get("entry"))
    if not direction:
        return {"kind": "invalid", "reason": "no BUY or SELL direction"}
    if sl is None:
        return {"kind": "invalid", "reason": "no stop loss price"}
    if order_type != "market" and entry is None:
        return {"kind": "invalid", "reason": f"a {order_type} order needs an entry price"}
    confidence = _number(fields.get("confidence"))
    return {"kind": "trade", "strategy_id": sid, "setup": {
        "action": "TRADE_SETUP", "direction": direction, "order_type": order_type,
        "entry_price": price if order_type == "market" else entry, "stop_loss": sl, "take_profit": tp,
        "confidence": int(confidence) if confidence is not None else 50, "reasoning": reasoning}}


def _parse_trade_setup(output: str):
    """A TRADE_SETUP from sandbox output: a ```json block, or a JSON line. None if absent."""
    jm = re.search(r'```json\n?(.*?)```', output or "", re.DOTALL)
    if jm:
        try:
            return json.loads(jm.group(1))
        except json.JSONDecodeError:
            pass
    for line in (output or "").strip().split("\n"):
        try:
            obj = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("action") == "TRADE_SETUP":
            return obj
    return None


def _classify_execution_error(message: str) -> str:
    """Return a safe, stable category for analysis without exporting raw errors."""
    text = (message or "").lower()
    if "wrong side" in text:
        return "INVALID_LEVEL_GEOMETRY"
    if "risk cap" in text or "stop-distance" in text:
        return "STOP_DISTANCE_LIMIT"
    if "reward/risk" in text:
        return "REWARD_RISK_LIMIT"
    if "http 400" in text or "order failed" in text:
        return "BROKER_REJECTED"
    if any(marker in text for marker in ("timeout", "connect", "network", "http 5")):
        return "CONNECTOR_UNAVAILABLE"
    return "EXECUTION_ERROR"


async def _finish_autopilot_cycle(user_id: int, cycle_id: str, outcome: str, reason: str = None, **values):
    values.update(
        status="completed",
        outcome=outcome,
        outcome_reason=reason,
        completed_at=datetime.now(timezone.utc),
    )
    await _update_autopilot_cycle(cycle_id, **values)
    state = _get_state(user_id)
    if state.get("active_cycle_id") == cycle_id:
        state["active_cycle_id"] = None




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


async def execute_trade(user_id: int, symbol: str, direction: str, volume: float = None, entry_price: float = None,
                       sl: float = None, tp: float = None, comment: str = "[AUTOPILOT]", prompt_num: int = None,
                       order_type: str = "market", context: dict = None, cycle_id: str = None,
                       max_sl_distance: float = None, min_reward_risk: float = None):
    """Send one autopilot order through the risk gate, which sizes it from the stop loss.

    `volume`, the AI's suggested lot, is ignored: the size is set so that hitting
    the stop loses the autopilot risk percent of equity. It is kept in the context.
    """
    try:
        if prompt_num:
            if isinstance(prompt_num, int) and prompt_num < 0:
                trade_comment = f"[AUTOPILOT] C{abs(prompt_num)}"
            else:
                trade_comment = f"[AUTOPILOT] P{prompt_num}"
            if cycle_id:
                # Compact UUID fragment fits typical MT5 comment limits; prompt parsing remains compatible.
                trade_comment = f"{trade_comment} X{cycle_id[:12]}"
        else:
            trade_comment = comment

        action = _build_order_action(direction, order_type)
        is_pending = order_type.lower() in ("limit", "stop")

        # Fetch symbol info for min stop distance + current price
        price = None
        submitted_quote = None
        min_dist = None
        digits = None
        try:
            sym_data = await connector_client.get_symbol(symbol)
            # A buy fills at the ask and a sell at the bid; stops are measured from there.
            price = sym_data.get("ask") if direction.upper() == "BUY" else sym_data.get("bid")
            submitted_quote = price
            min_dist = sym_data.get("min_stop_distance")
            digits = sym_data.get("digits")
        except Exception as e:
            add_log(user_id, f"Could not fetch symbol info for {symbol}: {str(e)}", "ERROR")

        # Reference price for stop distance checks (current market for pending orders too)
        ref_price = (entry_price if is_pending else price) or entry_price

        # A missing stop is not filled in here: the risk gate either sets one by
        # ATR (default_stop_atr_mult) or refuses the order. No trade is sent naked.
        sl = sl or None
        tp = tp or None

        # Validate before sending. Do not silently repair a model-provided SL
        # or TP that is on the wrong side of the trade.
        if ref_price is not None:
            is_buy = direction.upper() == "BUY"
            if sl is not None and ((is_buy and sl >= ref_price) or (not is_buy and sl <= ref_price)):
                return {"success": False, "error": "Stop loss is on the wrong side of the order price"}
            if tp is not None and ((is_buy and tp <= ref_price) or (not is_buy and tp >= ref_price)):
                return {"success": False, "error": "Take profit is on the wrong side of the order price"}

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

        risk_ref_price = (entry_price if is_pending else submitted_quote) or ref_price
        if sl and max_sl_distance is not None and risk_ref_price is not None:
            if abs(risk_ref_price - sl) > max_sl_distance + 10 ** (-(digits or 5)):
                return {"success": False, "error": "Stop loss is further than the configured ATR risk cap allows"}
        if sl and tp and min_reward_risk is not None and risk_ref_price is not None:
            risk_distance = abs(risk_ref_price - sl)
            reward_distance = abs(tp - risk_ref_price)
            if risk_distance <= 0 or reward_distance < risk_distance * min_reward_risk:
                return {"success": False, "error": "Take profit is below the minimum reward/risk ratio"}

        payload = {"symbol": symbol, "action": action, "comment": trade_comment}
        if is_pending:
            payload["price"] = entry_price
        if sl and sl > 0:
            payload["sl"] = sl
        if tp and tp > 0:
            payload["tp"] = tp
        if max_sl_distance is not None:
            payload["max_sl_distance"] = max_sl_distance
        if min_reward_risk is not None:
            payload["min_reward_risk"] = min_reward_risk

        ctx = {**(context or {}), "prompt_number": prompt_num, "ai_lot": volume}
        data = await submit_order(payload, source="autopilot", user_id=user_id, context=ctx, size_from_risk=True)
        if data.get("success"):
            risk = data.get("risk") or {}
            add_log(user_id, f"Sized {risk.get('volume')} lots: risks {risk.get('risk_amount')} "
                             f"({risk.get('risk_pct')}% of equity) if the stop is hit")
            return {
                "success": True,
                "ticket": data.get("ticket"),
                "order_ticket": data.get("order_ticket", data.get("ticket")),
                "deal_ticket": data.get("deal_ticket", data.get("deal")),
                "position_ticket": data.get("position"),
                "order_status": data.get("order_status") or ("placed" if is_pending else "filled"),
                "price": data.get("price"),
                "volume": data.get("volume") or risk.get("volume"),
                "requested_price": None if is_pending else data.get("requested_price"),
                "submitted_quote": data.get("submitted_quote"),
                "stop_loss": data.get("sl"),
                "take_profit": data.get("tp"),
            }
        return {"success": False, "error": "Order failed", "submitted_quote": submitted_quote}
    except RiskRefused as e:
        add_log(user_id, f"Risk check refused the order ({e.code}): {e.message}", "WARNING")
        return {"success": False, "error": e.message, "refused": e.code}
    except ConnectorError as e:
        add_log(user_id, f"Trade execution failed: {e.detail}", "ERROR")
        return {"success": False, "error": e.detail, "submitted_quote": submitted_quote}
    except Exception as e:
        add_log(user_id, f"Trade execution failed: {str(e)}", "ERROR")
        return {
            "success": False,
            "error": str(e),
            "submitted_quote": locals().get("submitted_quote"),
        }


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
        logger.warning("[user=%d] Could not update model usage", user_id, exc_info=True)


async def _log_ai_call(
    user_id: int, prompt_number: int, cycle_number: int,
    provider: str, model: str, stage: str,
    outcome: str = "pending",
    prompt_tokens: int = 0, completion_tokens: int = 0, total_tokens: int = 0,
    error_message: str = None,
    latency_ms: int = None,
    cycle_id: str | None = None,
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
                cycle_id=cycle_id,
                provider=provider, model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                stage=stage, outcome=outcome,
                error_message=error_message, cost=cost,
                latency_ms=latency_ms,
            )
            db.add(log)
            await db.commit()
            await db.refresh(log)
            return log.id
    except Exception:
        logger.warning("[user=%s] Could not record an AI call", user_id, exc_info=True)
        return None


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
        except Exception:  # swallow-ok: ADX needs enough candles; the regime is judged without it
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
        logger.warning("Could not classify the market regime", exc_info=True)
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
    signals: dict | None = None,
    labels: dict | None = None,
    shortlist: int = 0,
):
    """Rank prompts by regime fit, label fit and this user's outcomes in this regime.

    With shortlist=0 one prompt is drawn at random, weighted by score (the code
    path). With shortlist=k the top k are returned, in order, for the AI to choose
    from (the brief path): deterministic, so the record shows exactly why each was there.
    `labels` are the stored prompt labels; `signals` the backend's market signals.
    """
    from ..core import prompt_labels as _labels
    from ..core.strategy_scorer import MIN_TRADES_FOR_BEST

    history_by_prompt: dict[str, dict] = {}
    current_regime = market_regime.get("regime") or "unknown"

    try:
        async with AsyncSessionLocal() as db:
            recent_result = await db.execute(
                select(AutopilotTrade)
                .where(
                    AutopilotTrade.user_id == user_id,
                    AutopilotTrade.symbol == symbol,
                    AutopilotTrade.profit.isnot(None),
                    AutopilotTrade.market_regime == current_regime,
                )
                .order_by(AutopilotTrade.executed_at.desc())
                .limit(500)
            )
            for trade in recent_result.scalars().all():
                bucket = history_by_prompt.setdefault(
                    trade.prompt_text,
                    {"trades": 0, "wins": 0, "pnl": 0.0, "gross_profit": 0.0, "gross_loss": 0.0},
                )
                bucket["trades"] += 1
                bucket["pnl"] += trade.profit or 0.0
                if (trade.profit or 0.0) > 0:
                    bucket["wins"] += 1
                    bucket["gross_profit"] += trade.profit
                elif (trade.profit or 0.0) < 0:
                    bucket["gross_loss"] += abs(trade.profit)
    except Exception:
        logger.warning("Could not read recent results per prompt; prompts are ranked without them", exc_info=True)

    ranked = []
    for prompt in prompt_pool:
        stored = None if prompt["is_custom"] else (labels or {}).get(str(prompt["id"]))
        if stored:
            tags = {**stored, "avoid_when": _infer_prompt_tags(prompt["text"])["avoid_when"], "labelled": True}
        else:
            tags = _infer_prompt_tags(prompt["text"])
        regime_score, fit_reasons = _score_prompt_regime_fit(tags, market_regime)
        score = 50.0 + regime_score
        if signals:
            label_score, label_reasons = _labels.fit(stored or _labels.draft_by_keyword(prompt["text"]), signals)
            score += label_score
            fit_reasons = fit_reasons + label_reasons
        history_reasons = []

        recent = history_by_prompt.get(prompt["text"])
        if recent and recent["trades"] >= MIN_TRADES_FOR_BEST:
            recent_wr = recent["wins"] / recent["trades"] * 100
            profit_factor = (recent["gross_profit"] / recent["gross_loss"]
                             if recent["gross_loss"] else (float("inf") if recent["gross_profit"] else 0.0))
            # Bounded contributions keep lot size/P&L scale from overwhelming regime fit.
            score += min(18, max(-18, (recent_wr - 50) * 0.4))
            if profit_factor != float("inf"):
                score += min(8, max(-8, (profit_factor - 1.0) * 5))
            elif recent["gross_profit"]:
                score += 8
            score += min(6, max(-6, recent["pnl"] / 50))
            history_reasons.append(
                f"same-regime {recent['trades']} trades, win {recent_wr:.1f}%, "
                f"pf {'inf' if profit_factor == float('inf') else f'{profit_factor:.2f}'}, pnl {recent['pnl']:+.2f}"
            )
        elif recent:
            history_reasons.append(f"same-regime sample too small ({recent['trades']}/{MIN_TRADES_FOR_BEST}); neutral performance weight")

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
    if shortlist:
        top = ranked[:shortlist]
        return top, {
            "selection_mode": "shortlist_for_ai",
            "market_regime": market_regime,
            "signals": {k: v for k, v in (signals or {}).items() if k != "recent_candles"},
            "candidate_count": len(ranked),
            "shortlist": [{"id": item["prompt"]["id"], "is_custom": item["prompt"]["is_custom"],
                           "score": item["score"], "tags": item["tags"], "reasons": item["reasons"]}
                          for item in top],
            "labelled_prompts": sum(1 for item in ranked if item["tags"].get("labelled")),
        }
    weighted = []
    for item in ranked:
        weighted.extend([item] * item["weight"])

    total_weight = sum(item["weight"] for item in ranked)
    for item in ranked:
        item["selection_probability"] = round(item["weight"] / total_weight, 8) if total_weight else 0.0
    selected = random.choice(weighted) if weighted else random.choice(ranked)
    context = {
        "selection_mode": "regime_score_weighted",
        "selected_score": selected["score"],
        "selected_tags": selected["tags"],
        "selected_reasons": selected["reasons"],
        "selected_probability": selected["selection_probability"],
        "market_regime": market_regime,
        "candidate_count": len(ranked),
        "candidates": [
            {"id": item["prompt"]["id"], "is_custom": item["prompt"]["is_custom"],
             "score": item["score"], "weight": item["weight"],
             "selection_probability": item["selection_probability"], "tags": item["tags"],
             "reasons": item["reasons"][:3]}
            for item in ranked
        ],
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


async def run_autopilot_cycle(user_id: int, cycle_id: str | None = None):
    state = _get_state(user_id)
    # The loop creates the durable cycle before its cooldown/health gates. Keep
    # direct callers backwards-compatible by creating a cycle here when needed.
    if cycle_id is None:
        state["stats"]["total_runs"] += 1
        state["stats"]["last_run"] = datetime.now(timezone.utc).isoformat()
        cycle_id = str(uuid.uuid4())
        state["active_cycle_id"] = cycle_id
        async with AsyncSessionLocal() as db:
            db.add(AutopilotCycle(
                cycle_id=cycle_id,
                user_id=user_id,
                cycle_number=state["stats"]["total_runs"],
                symbol="unknown",
                status="running",
            ))
            await db.commit()
    cycle_number = state["stats"]["total_runs"]
    add_log(user_id, f"=== Starting Cycle #{cycle_number} ===")

    default_prompts = load_prompts()
    
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AutopilotSettings).where(AutopilotSettings.user_id == user_id)
        )
        settings_obj = result.scalar_one_or_none()
        if settings_obj:
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

    if not settings_obj:
        add_log(user_id, "Autopilot not configured", "ERROR")
        await _finish_autopilot_cycle(user_id, cycle_id, "not_configured", "No autopilot settings row exists")
        return

    await _update_autopilot_cycle(
        cycle_id,
        symbol=symbol,
        provider=provider,
        model=model,
    )

    prompt_pool = []
    for line in default_prompts:
        try:
            p_num = int(line.split(".")[0].strip())
            if not selected_ids or p_num in selected_ids:
                prompt_pool.append({"id": p_num, "text": line.split(".", 1)[1].strip(), "is_custom": False})
        except (ValueError, IndexError):  # not an "N. text" line
            continue

    for p in personal_prompts:
        custom_id = f"custom_{p.id}"
        if not selected_ids or custom_id in selected_ids:
            prompt_pool.append({"id": custom_id, "text": p.content, "is_custom": True})

    if not prompt_pool:
        add_log(user_id, "No prompts selected in settings", "ERROR")
        await _finish_autopilot_cycle(user_id, cycle_id, "no_eligible_prompts", "The configured selection produced no eligible prompts")
        return

    if not mt5_connected:
        add_log(user_id, "Initializing MT5 connection...")
        conn_ok = await initialize_mt5_connector(user_id)
        if not conn_ok:
            add_log(user_id, "Failed to connect to MT5. Check MT5_CONNECTOR_URL and MT5_API_TOKEN on the server.", "ERROR")
            await _finish_autopilot_cycle(user_id, cycle_id, "mt5_connection_failed", "Could not initialize the configured MT5 connection")
            return
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AutopilotSettings).where(AutopilotSettings.user_id == user_id))
            s = result.scalar_one_or_none()
            if s:
                s.mt5_connected = True
                await db.commit()

    # Read from the database every cycle, so restarts and back-syncs cannot skew them.
    await _refresh_daily_stats(user_id)
    if state["stats"]["daily_trade_count"] >= max_trades:
        add_log(user_id, f"Daily trade limit ({max_trades}) reached. Skipping.", "WARNING")
        state["stats"]["skipped_count"] += 1
        await _finish_autopilot_cycle(user_id, cycle_id, "daily_trade_limit", f"Daily trade limit ({max_trades}) reached")
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

    brief_mode = settings.AUTOPILOT_DECISION_MODE.lower() == "brief"
    signals: dict = {}
    shortlist_items: list[dict] = []
    if brief_mode:
        # The backend measures the market and shortlists the prompts that fit;
        # one AI call then picks among them (see _brief_user_message).
        from ..core import market_signals, prompt_labels
        signals = market_signals.compute_signals(baseline_market_data or [], market_regime,
                                                 datetime.now(timezone.utc), timeframe="15m")
        shortlist_items, decision_context = await _choose_prompt_with_context(
            user_id=user_id, prompt_pool=prompt_pool, symbol=symbol, market_regime=market_regime,
            signals=signals, labels=prompt_labels.load(), shortlist=max(1, settings.AUTOPILOT_SHORTLIST),
        )
        chosen = shortlist_items[0]["prompt"]  # provisional, until the AI picks
        decision_context["selected_score"] = shortlist_items[0]["score"]
        decision_context["selected_tags"] = shortlist_items[0]["tags"]
        if signals.get("ok"):
            add_log(user_id, f"Signals: session {signals['session']}, volatility {signals['volatility']['label']}, "
                             f"volume {signals['volume']['label']} ({signals['volume']['ratio']}), "
                             f"RSI {signals['momentum']['rsi14']}, price {signals['price']}")
        add_log(user_id, "Shortlist for the AI: " + ", ".join(
            f"#{item['prompt']['id']} ({item['score']})" for item in shortlist_items))
    else:
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
    if not brief_mode:
        add_log(
            user_id,
            f"Using Strategy {display_id} | score={selected_score} | styles={selected_styles}: {prompt_text[:50]}...",
        )
    prompt_version = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    await _update_autopilot_cycle(
        cycle_id,
        prompt_number=prompt_num,
        prompt_text=prompt_text,
        prompt_version=prompt_version,
        market_regime=market_regime.get("regime"),
        regime_details=market_regime,
        selection_context=decision_context,
    )

    # ── Model routing (Phase 4b): try the best-performing provider/model for
    #    this symbol first; the user's configured provider stays in the list
    #    as fallback. Only activates once a pair has >=10 closed trades. ──────
    model_routing = None
    try:
        from ..core.strategy_scorer import get_best_model_for_symbol
        model_routing = await get_best_model_for_symbol(symbol)
    except Exception:
        logger.warning("Model routing unavailable for %s; using the configured provider", symbol, exc_info=True)
        model_routing = None
    if model_routing:
        await _update_autopilot_cycle(
            cycle_id,
            provider=model_routing["provider"],
            model=model_routing["model"],
        )
        add_log(
            user_id,
            f"Model routing: {model_routing['provider']}/{model_routing['model']} "
            f"(win {model_routing['win_rate']:.0f}%, {model_routing['trades']} trades) -> tried first",
            "INFO",
        )

    # ── AI call helper with 429 retry + provider fallback ────────────────────
    _call_count = 0
    _call_tokens = 0
    _call_log_ids: list[int] = []

    async def _log_call(outcome: str, p: str, m: str, st: str, pt: int = 0, ct: int = 0, tt: int = 0, err: str = None, lt: int = None):
        nonlocal _call_count, _call_tokens
        _call_count += 1
        if tt:
            _call_tokens += tt
        log_id = await _log_ai_call(
            user_id, prompt_num, state["stats"]["total_runs"],
            p, m, st, outcome=outcome,
            prompt_tokens=pt, completion_tokens=ct, total_tokens=tt,
            error_message=err, latency_ms=lt,
            cycle_id=cycle_id,
        )
        if log_id:
            _call_log_ids.append(log_id)

    # Why the last _call_ai_with_retry returned nothing, for the cycle's log line.
    _ai_failure = {"reason": ""}

    async def _call_ai_with_retry(messages: list, provider: str, model: str, max_retries: int = 3, stage: str = "initial",
                                  single_provider: bool = False, max_tokens: int = 2500,
                                  extract_code: bool = True) -> tuple[str | None, dict | None, dict | None]:
        """Call AI with multi-key fallback + provider fallback.

        For each provider, resolves ALL available API keys (comma-separated).
        If key #1 fails with auth/rate-limit, tries key #2, then #3, etc.
        If all keys for a provider are exhausted, moves to the next provider.

        Returns (content, usage_dict) where usage_dict has prompt_tokens, completion_tokens, total_tokens.
        On failure returns (None, None) and leaves the reason in _ai_failure.

        single_provider=True asks only `provider`, as the failover, self-correction
        and backup steps do. They used to fall through the whole provider list, so
        with one key every one of them called the same provider again.
        """
        from ..core.providers import PROVIDERS, get_provider_names, get_base_url, resolve_all_api_keys

        providers = get_provider_names()
        no_key: list[str] = []
        last_error = ""
        _ai_failure["reason"] = "the reply was empty"  # if a provider answers with nothing
        _ai_failure["rate_limited"] = False

        if single_provider:
            providers = [provider]
            provider_idx = 0
        elif model_routing and model_routing["provider"] in providers:
            providers = [model_routing["provider"]] + [
                p for p in providers if p != model_routing["provider"]
            ]
            provider_idx = 0
        else:
            provider_idx = providers.index(provider) if provider in providers else 0

        async def get_best_model(p: str, all_keys: List[str]) -> str:
            """Get the best available model for provider p.
            Always fetches live models from provider API (cached 24h).
            Only falls back to hardcoded list if API fails.
            """
            cfg = PROVIDERS.get(p, {})
            base_url = cfg.get("base_url", "")
            needs_prefix = cfg.get("needs_nvapi_prefix", False)

            # Fetch live models via shared cache (24h TTL)
            for api_key in all_keys:
                models = await _get_live_models(p, api_key, base_url, needs_prefix)
                if models:
                    return models[0]

            # Fallback to hardcoded if API failed
            fallback = cfg.get("models", ["unknown"])[0] if cfg.get("models") else "unknown"
            return fallback

        for attempt in range(max_retries):
            for p_idx in range(provider_idx, len(providers)):
                p = providers[p_idx]
                all_keys = await resolve_all_api_keys(p, settings, user_id, AsyncSessionLocal)
                if not all_keys:
                    if p not in no_key:
                        no_key.append(p)
                        await _log_call("no_key", p, model if p == provider else PROVIDERS[p]["models"][0], stage)
                    continue

                from ..core.models_cache import is_blacklisted
                if model_routing and p == model_routing["provider"]:
                    actual_model = model_routing["model"]
                elif p == provider and model and not is_blacklisted(p, model):
                    # The model chosen on the Autopilot page. It used to be ignored:
                    # the provider's first listed model was taken instead.
                    actual_model = model
                else:
                    actual_model = await get_best_model(p, all_keys)
                model_unusable = False

                # Try each key for this provider
                for key_idx, api_key in enumerate(all_keys):
                    if len(all_keys) > 1:
                        add_log(user_id, f"Provider {p}: trying key {key_idx + 1}/{len(all_keys)}", "INFO")

                    try:
                        client = AsyncOpenAI(base_url=get_base_url(p), api_key=api_key)
                        _t0 = time.time()
                        response = await client.chat.completions.create(
                            model=actual_model,
                            messages=messages,
                            temperature=0.2,
                            max_tokens=max_tokens,
                            timeout=60
                        )
                        latency_ms = int((time.time() - _t0) * 1000)
                        content = response.choices[0].message.content or ""
                        match = re.search(r'```(?:python)?\n?(.*?)```', content, re.DOTALL) if extract_code else None
                        result = match.group(1).strip() if match else content.strip()
                        usage = None
                        if hasattr(response, 'usage') and response.usage:
                            usage = {
                                "provider": p,
                                "model": actual_model,
                                "prompt_tokens": response.usage.prompt_tokens or 0,
                                "completion_tokens": response.usage.completion_tokens or 0,
                                "total_tokens": response.usage.total_tokens or 0,
                                "latency_ms": latency_ms,
                            }
                        if usage:
                            asyncio.create_task(_update_model_usage(user_id, usage))
                            await _log_call("success", p, actual_model, stage,
                                usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"],
                                lt=latency_ms)
                        else:
                            await _log_call("no_usage", p, actual_model, stage)
                        if len(all_keys) > 1:
                            add_log(user_id, f"Provider {p} key {key_idx + 1} succeeded")
                        return result, usage, _capture_raw_response(response)
                    except Exception as e:
                        err_str = str(e).lower()
                        if any(m in err_str for m in _BUSY):
                            # Rate limited — try next key for this provider
                            add_log(user_id, f"Provider {p} key {key_idx + 1} is busy (rate limited or overloaded): {str(e)[:80]}", "WARNING")
                            await _log_call("rate_limited", p, actual_model, stage, err=str(e)[:200])
                            last_error = f"{p} is rate limiting requests or overloaded"
                            _ai_failure["rate_limited"] = True
                            continue
                        elif any(m in err_str for m in _MODEL_UNUSABLE):
                            # This model cannot be used this way (for example one that "only
                            # supports Interactions API"). The key is fine: set the model aside
                            # for 24 hours and let the next attempt pick another, without waiting.
                            from ..core.models_cache import blacklist_model
                            blacklist_model(p, actual_model)
                            add_log(user_id, f"Provider {p} model {actual_model} cannot be used here "
                                             f"({str(e)[:120]}). Set aside; trying another model.", "WARNING")
                            await _log_call("model_not_found", p, actual_model, stage, err=str(e)[:200])
                            last_error = f"{p} model {actual_model} cannot be used: {str(e)[:120]}"
                            model_unusable = True
                            break
                        elif any(m in err_str for m in _KEY_REFUSED):
                            # Auth failed — try next key for this provider
                            add_log(user_id, f"Provider {p} key {key_idx + 1} auth failed, trying next key...", "WARNING")
                            await _log_call("auth_failed", p, actual_model, stage, err=str(e)[:200])
                            last_error = f"{p} refused the key: {str(e)[:120]}"
                            continue
                        elif "402" in err_str or "payment_required" in err_str or "payment required" in err_str:
                            # Payment required — provider account has no credits, skip ENTIRE provider
                            add_log(user_id, f"Provider {p} payment required (402), skipping provider...", "WARNING")
                            await _log_call("payment_required", p, actual_model, stage, err=str(e)[:200])
                            last_error = f"{p} needs payment (402)"
                            break  # Skip to next provider
                        elif "403" in err_str or "forbidden" in err_str or "tier_not_allowed" in err_str or "subscription tier" in err_str:
                            # Tier not allowed — provider account can't access model, skip ENTIRE provider
                            add_log(user_id, f"Provider {p} tier/forbidden (403), skipping provider...", "WARNING")
                            await _log_call("tier_not_allowed", p, actual_model, stage, err=str(e)[:200])
                            last_error = f"{p} does not allow this model on the account (403)"
                            break  # Skip to next provider
                        else:
                            # Other error — try next key for this provider
                            err_msg = str(e)[:200]
                            add_log(user_id, f"Provider {p} key {key_idx + 1} error: {err_msg}", "WARNING")
                            await _log_call("error", p, actual_model, stage, err=err_msg)
                            last_error = f"{p} answered with an error: {err_msg}"
                            continue

                # All keys for this provider exhausted — wait before trying next provider
                if not last_error and len(no_key) == len(providers) - provider_idx:
                    continue  # no key at all: nothing to wait for
                if model_unusable:
                    continue  # another model is tried at once; waiting would not help
                if attempt < max_retries - 1:
                    wait = min(2 ** attempt * 10, 60)
                    await asyncio.sleep(wait)

            if not last_error and len(no_key) == len(providers) - provider_idx:
                break  # no provider has a key: retrying cannot help
        if last_error:
            _ai_failure["reason"] = last_error
        elif no_key:
            _ai_failure["reason"] = "no AI key is set for any provider. Add one in Settings, AI Providers"
        else:
            _ai_failure["reason"] = "no provider answered"
        return None, None, None

    if brief_mode:
        # ── One AI call: the market brief plus the shortlist. ─────────────────
        setup = None
        saw_no_setup = False
        _source = "brief"
        _last_usage = None
        full_raw_response = None
        ai_response = ""
        atr_value = float((signals.get("volatility") or {}).get("atr") or 0.0)
        if not signals.get("ok"):
            add_log(user_id, f"Not enough market data for the brief ({signals.get('reason')}). Skipping.", "WARNING")
            state["stats"]["skipped_count"] += 1
            await _finish_autopilot_cycle(user_id, cycle_id, "no_market_data", signals.get("reason"))
            return
        allowed_ids = [str(item["prompt"]["id"]) for item in shortlist_items]
        brief_message = _brief_user_message(symbol, signals, shortlist_items)
        add_log(user_id, f"AI brief: {len(brief_message)} characters, {len(allowed_ids)} strategies, one call")
        reply, _last_usage, full_raw_response = await _call_ai_with_retry(
            messages=[{"role": "system", "content": BRIEF_SYSTEM}, {"role": "user", "content": brief_message}],
            provider=provider, model=model, max_retries=2, stage="brief",
            # Thinking models spend part of the limit before answering; 2500 cut answers off.
            max_tokens=8000, extract_code=False,
        )
        if not reply:
            busy = _ai_failure.get("rate_limited")
            add_log(user_id, f"AI decision failed: {_ai_failure['reason']}", "WARNING" if busy else "ERROR")
            if busy:
                state["stats"]["skipped_count"] += 1
            else:
                state["stats"]["error_count"] += 1
            await _finish_autopilot_cycle(user_id, cycle_id, "ai_provider_busy" if busy else "ai_generation_failed",
                                          _ai_failure["reason"])
            return
        ai_response = reply
        decision = _parse_brief_decision(reply, allowed_ids, signals["price"])
        top_id = allowed_ids[0]
        if decision["kind"] == "invalid":
            snippet = " ".join(reply.split())[:200]
            add_log(user_id, f"AI answer not usable: {decision['reason']}. It began: {snippet!r}", "WARNING")
            decision_context["ai_choice"] = {"kind": "invalid", "reason": decision["reason"]}
            await _update_autopilot_cycle(cycle_id, selection_context=decision_context)
            state["stats"]["error_count"] += 1
            await _finish_autopilot_cycle(user_id, cycle_id, "ai_provider_or_response_failed", decision["reason"][:500])
            return
        picked_id = decision.get("strategy_id") or top_id
        picked = next(item for item in shortlist_items if str(item["prompt"]["id"]) == picked_id)
        chosen = picked["prompt"]
        prompt_id_val, prompt_text = chosen["id"], chosen["text"]
        if chosen["is_custom"]:
            prompt_num = -int(str(prompt_id_val).split('_')[1])
            display_id = f"Custom-{str(prompt_id_val).split('_')[1]}"
        else:
            prompt_num, display_id = prompt_id_val, f"#{prompt_id_val}"
        decision_context.update(
            selected_score=picked["score"], selected_tags=picked["tags"], selected_reasons=picked["reasons"],
            ai_choice={"kind": decision["kind"], "strategy_id": picked_id, "top_ranked_id": top_id,
                       "chose_top_ranked": picked_id == top_id, "reasoning": decision.get("reasoning", "")[:1000]},
        )
        await _update_autopilot_cycle(
            cycle_id, prompt_number=prompt_num, prompt_text=prompt_text,
            prompt_version=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
            selection_context=decision_context,
        )
        add_log(user_id, f"AI chose Strategy {display_id}"
                         f"{'' if picked_id == top_id else f' (ranked below #{top_id})'}: {prompt_text[:60]}...")
        if decision["kind"] == "no_setup":
            saw_no_setup = True
            add_log(user_id, f"AI: NO_SETUP. {decision.get('reasoning', '')[:160]}", "INFO")
        else:
            setup = decision["setup"]
            add_log(user_id, f"TRADE_SETUP via the brief (conf={setup.get('confidence')}%)")
    else:
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
            await _finish_autopilot_cycle(
                user_id, cycle_id, "no_market_data", f"No {tf} candle data available",
                market_timeframe=tf, candles_loaded=0,
            )
            return
        await _update_autopilot_cycle(cycle_id, market_timeframe=tf, candles_loaded=len(market_data))
        market_data_hash = hashlib.sha256(
            json.dumps(market_data, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        await _update_autopilot_cycle(cycle_id, market_data_hash=market_data_hash)
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
        except Exception as e:
            add_log(user_id, f"Could not compute ATR, using 0: {e}", "WARNING")
        add_log(user_id, f"ATR(14): {atr_value:.2f} | Avg(20): {avg_atr_20:.2f}")
        await _update_autopilot_cycle(cycle_id, atr_14=atr_value, avg_atr_20=avg_atr_20)

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

        # ── RAG: inject the track record (similar past analyses + best/losing
        #    strategies for this symbol) so the AI trades on its own history.
        #    Best-effort: any failure degrades to no-context, never blocks a cycle.
        rag_section = ""
        try:
            from ..core.rag_service import build_rag_context
            rag_ctx = await build_rag_context(
                symbol, prompt_text, user_id=user_id, source="autopilot", cycle_id=cycle_id
            )
            if rag_ctx:
                rag_section = f"""
    PAST PERFORMANCE (your own track record on {symbol}):
    {rag_ctx}
    Use this: repeat what worked, propose an alternative to anything listed as underperforming, and do NOT simply re-run losing approaches.
    """
                add_log(user_id, f"RAG context attached ({len(rag_ctx)} chars)")
        except Exception as e:
            add_log(user_id, f"RAG context unavailable: {e}", "WARNING")

        candle_count = len(market_data)
        data_warning = ""
        if candle_count < 100:
            data_warning = f"\nWARNING: Limited historical data ({candle_count} candles). Indicators with large windows (like SMA 200) will fail. RSI(14), ATR(14), and Bollinger Bands(20) are safe above 30 candles.\n"
        elif candle_count < 500:
            data_warning = f"\nNOTE: {candle_count} candles available. SMA 200 may produce NaNs. Use .dropna() before accessing results.\n"

        code_prompt = f"""You are a quant trader. Write Python code to analyze market data.

    IMPORTANT RULES:
    0. NEVER write any name with double underscores (no __name__, __main__, __len__, __class__, __import__).
       Code containing them is rejected before it runs.
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
         df_4h = df.resample('4h', on='timestamp').agg({{'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}})
       Aliases: '1h', '4h', '1D' (pandas 3 removed '1H'/'4H' — never use uppercase H or bare 'm').
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
    {rag_section}
    {decision_section}
    {error_section}
    """

        analysis_prompt_hash = hashlib.sha256(code_prompt.encode("utf-8")).hexdigest()
        await _update_autopilot_cycle(
            cycle_id,
            analysis_prompt_hash=analysis_prompt_hash,
            rag_context_included=bool(rag_section),
            rag_context_chars=len(rag_section),
        )

        add_log(user_id, f"AI prompt: analyze {len(market_data)} {tf} candles for strategy")

        full_raw_response = ""

        # Step 1: AI generates analysis code (with 429 retry + provider fallback)
        _last_usage = None
        _source = "sandbox"
        generated_code, _last_usage, _raw_resp = await _call_ai_with_retry(
            messages=[{"role": "user", "content": code_prompt}],
            provider=provider,
            model=model,
            max_retries=3,
            stage="initial",
        )
        if _raw_resp:
            full_raw_response = _raw_resp
        if not generated_code:
            add_log(user_id, f"AI code generation failed after retries: {_ai_failure['reason']}", "ERROR")
            state["stats"]["error_count"] += 1
            await _finish_autopilot_cycle(user_id, cycle_id, "ai_generation_failed", "No code returned after initial provider retries")
            return
        add_log(user_id, f"AI generated code ({len(generated_code)} chars)")

        # Step 2 & 3: Execute in sandbox with self-correction
        from ..api.execute import run_python_code

        setup = None
        saw_no_setup = False
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
        # Only providers that have a key: asking the rest only fell through to the
        # one provider that does, multiplying calls to it.
        from ..core.providers import resolve_all_api_keys as _keys_for
        _retry_providers = [p for p in dict.fromkeys(_retry_providers)
                            if p == provider or await _keys_for(p, settings, user_id, AsyncSessionLocal)]

        def _model_for(p: str) -> str:
            return model if p == provider else PROVIDERS[p]["models"][0]

        _busy = False

        for p_idx, retry_p in enumerate(_retry_providers):
            # First iteration uses the already-generated code from the initial call.
            # Subsequent iterations generate new code with the next provider.
            if _busy:
                break  # the provider is rate limiting: stop here, the next cycle tries again
            if p_idx > 0:
                add_log(user_id, f"Trying provider {retry_p} for code generation...", "INFO")
                new_code, _last_usage, _raw_resp = await _call_ai_with_retry(
                    messages=[{"role": "user", "content": code_prompt}],
                    provider=retry_p,
                    model=_model_for(retry_p),
                    max_retries=2,
                    stage="failover",
                    single_provider=True,
                )
                if _raw_resp:
                    full_raw_response = _raw_resp
                if _ai_failure.get("rate_limited"):
                    _busy = True
                if not new_code:
                    add_log(user_id, f"{retry_p} code generation returned nothing ({_ai_failure['reason']}), skipping")
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
                    # The AI's answer stands. Asking other providers until one says yes
                    # would be shopping for a trade, and costs calls.
                    saw_no_setup = True
                    add_log(user_id, f"{retry_p} says NO_SETUP", "INFO")
                    break

                # Unclear output — try self-correction once with same provider
                add_log(user_id, f"{retry_p} output unclear, trying self-correction...", "WARNING")
                corrected, _last_usage, _raw_resp = await _call_ai_with_retry(
                    messages=[
                        {"role": "user", "content": code_prompt},
                        {"role": "assistant", "content": generated_code},
                        {"role": "user", "content": f"The code ran but didn't output a valid TRADE_SETUP or NO_SETUP. Fix it to output exactly one of these formats. Output was:\n{output[:400]}"}
                    ],
                    provider=retry_p,
                    model=_model_for(retry_p),
                    max_retries=2,
                    stage="self_correct",
                    single_provider=True,
                )
                if _raw_resp:
                    full_raw_response = _raw_resp
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
                            saw_no_setup = True
                            add_log(user_id, f"{retry_p} self-correction says NO_SETUP", "WARNING")
                else:
                    add_log(user_id, f"{retry_p} self-correction failed", "WARNING")
            else:
                # Sandbox error: give the same AI one chance to fix its code before
                # moving on. It used to go straight to the next provider.
                sand_err = sandbox_result.get("error", "Unknown error")[:300]
                add_log(user_id, f"{retry_p} code error: {sand_err[:200]}. Asking it to fix the code once.", "WARNING")
                fixed, _last_usage, _raw_resp = await _call_ai_with_retry(
                    messages=[
                        {"role": "user", "content": code_prompt},
                        {"role": "assistant", "content": generated_code},
                        {"role": "user", "content": f"The code failed with this error:\n{sand_err}\n"
                                                    "Fix it. Never use names with double underscores. "
                                                    "Output ONLY the corrected Python code."},
                    ],
                    provider=retry_p,
                    model=_model_for(retry_p),
                    max_retries=1,
                    stage="self_correct",
                    single_provider=True,
                )
                if _ai_failure.get("rate_limited"):
                    _busy = True
                if fixed:
                    generated_code = fixed
                    ai_response = generated_code
                    try:
                        sandbox_result = await run_python_code(code=generated_code, market_data=market_data,
                                                               symbol=symbol, user_id=user_id)
                    except Exception as e:
                        add_log(user_id, f"Corrected code from {retry_p} also failed: {str(e)}", "ERROR")
                        continue
                    if sandbox_result.get("success"):
                        fixed_output = sandbox_result.get("output", "")
                        setup = _parse_trade_setup(fixed_output)
                        if setup:
                            add_log(user_id, f"TRADE_SETUP found via {retry_p} after fixing its code (conf={setup.get('confidence')}%)")
                            break
                        if "NO_SETUP" in fixed_output:
                            saw_no_setup = True
                            add_log(user_id, f"{retry_p} fixed its code: NO_SETUP", "INFO")
                            break
                    else:
                        add_log(user_id, f"{retry_p} fixed code still failed: {sandbox_result.get('error', '')[:200]}", "WARNING")

        # ── BACKUP: all sandbox providers failed, send 50 candles + indicators directly ──
        if not setup and _busy:
            add_log(user_id, "The AI provider is busy (rate limited or overloaded). Skipping the backup; "
                             "the next cycle tries again.", "WARNING")
            state["stats"]["skipped_count"] += 1
            await _finish_autopilot_cycle(user_id, cycle_id, "ai_provider_busy", _ai_failure["reason"])
            return
        if not setup and not saw_no_setup:
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
    {rag_section}
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
                fallback_response, _last_usage, _raw_resp = await _call_ai_with_retry(
                    messages=[{"role": "user", "content": backup_prompt}],
                    provider=retry_p,
                    model=_model_for(retry_p),
                    max_retries=2,
                    stage="backup",
                    single_provider=True,
                )
                if _raw_resp:
                    full_raw_response = _raw_resp
                if _ai_failure.get("rate_limited"):
                    add_log(user_id, "The AI provider is busy; stopping the backup. The next cycle tries again.", "WARNING")
                    break
                if not fallback_response:
                    add_log(user_id, f"Backup {retry_p} returned nothing ({_ai_failure['reason']}), skipping")
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
                    saw_no_setup = True
                    add_log(user_id, f"Backup {retry_p}: NO_SETUP", "INFO")
                    break

                add_log(user_id, f"Backup {retry_p}: unclear response, trying next provider", "WARNING")

    # No setup, whether the AI said NO_SETUP or every attempt failed: record it and end the cycle.
    if not setup:
        add_log(user_id, "No setup this cycle: the AI said NO_SETUP" if saw_no_setup
                else "No setup this cycle: every attempt failed", "INFO" if saw_no_setup else "WARNING")
        state["stats"]["skipped_count"] += 1
        async with AsyncSessionLocal() as db:
            no_setup_record = AutopilotTrade(
                user_id=user_id, prompt_number=prompt_num, prompt_text=prompt_text,
                symbol=symbol, direction="NONE", order_type="market", lot_size=lot_size,
                execution_status="skipped", decision_type="NO_SETUP",
                reasoning="The AI returned NO_SETUP" if saw_no_setup else "Every attempt failed",
                market_regime=market_regime.get("regime") if market_regime else None,
                regime_details=market_regime,
                prompt_tags=decision_context.get("selected_tags"),
                decision_score=decision_context.get("selected_score"),
                decision_context=decision_context,
                source=_source,
                cycle_number=state["stats"]["total_runs"],
                cycle_id=cycle_id,
            )
            db.add(no_setup_record)
            await db.commit()
        await _finish_autopilot_cycle(
            user_id,
            cycle_id,
            "no_setup" if saw_no_setup else "ai_provider_or_response_failed",
            "Model explicitly returned NO_SETUP" if saw_no_setup else "No valid setup produced after provider and sandbox fallbacks",
            execution_status="skipped",
            decision_source=_source,
        )
        return

    direction = setup.get("direction", "BUY").upper()
    order_type = setup.get("order_type", "market").lower()
    entry_price = setup.get("entry_price")
    sl = setup.get("stop_loss")
    tp = setup.get("take_profit")
    # The AI's lot is recorded but not used: the risk gate sizes the order from its stop.
    lot = setup.get("lot_size", lot_size)
    requested_lot = lot  # the AI's suggestion; the risk gate sets the real size from the stop
    await _update_autopilot_cycle(
        cycle_id,
        setup={key: setup.get(key) for key in ("action", "symbol", "direction", "order_type", "entry_price", "stop_loss", "take_profit", "lot_size", "reasoning", "confidence") if key in setup},
        requested_lot_size=requested_lot,
        decision_source=_source,
    )
    reasoning = setup.get("reasoning", "")
    confidence = setup.get("confidence", 70)

    # Capture the AI-proposed values before any safety adjustments.
    proposed_entry_price = entry_price
    proposed_sl = sl
    proposed_tp = tp

    raw_geometry_error = None
    if entry_price and entry_price > 0:
        is_buy = direction == "BUY"
        if sl not in (None, 0) and ((is_buy and sl >= entry_price) or (not is_buy and sl <= entry_price)):
            raw_geometry_error = "AI stop loss is on the wrong side of its proposed entry"
        elif tp not in (None, 0) and ((is_buy and tp <= entry_price) or (not is_buy and tp >= entry_price)):
            raw_geometry_error = "AI take profit is on the wrong side of its proposed entry"

    # ── SL/TP post-processing: clamp unreasonably wide stops ──
    max_sl_distance = None
    min_reward_risk = None
    if not raw_geometry_error and atr_value and atr_value > 0:
        min_reward_risk = 1.5
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
        max_sl_distance = max_sl_dist
        if entry_price and sl and abs(entry_price - sl) > max_sl_dist:
            sl_dist = abs(entry_price - sl)
            if direction == "BUY":
                sl = entry_price - max_sl_dist
            else:
                sl = entry_price + max_sl_dist
            add_log(user_id, f"SL clamped from {sl_dist:.2f}pts to {max_sl_dist:.2f}pts (1.5x ATR={max_sl_by_atr:.2f}, hard cap={hard_cap})", "WARNING")
    if not raw_geometry_error and entry_price and tp and sl and atr_value and atr_value > 0:
        tp_dist = abs(entry_price - tp)
        sl_dist = abs(entry_price - sl)
        min_tp_dist = sl_dist * 1.5
        if tp_dist < min_tp_dist:
            if direction == "BUY":
                tp = entry_price + min_tp_dist
            else:
                tp = entry_price - min_tp_dist
            add_log(user_id, f"TP adjusted from {tp_dist:.2f}pts to {min_tp_dist:.2f}pts (min 1.5x SL={sl_dist:.2f})", "WARNING")

    add_log(user_id, f"TRADE SETUP - {direction} ({order_type}) | Entry: {entry_price} SL: {sl} TP: {tp} Confidence: {confidence}%")

    if raw_geometry_error:
        result = {"success": False, "error": raw_geometry_error}
        add_log(user_id, f"Trade setup rejected: {raw_geometry_error}", "WARNING")
    else:
        result = await execute_trade(
            user_id, symbol, direction, lot, entry_price, sl, tp,
            prompt_num=prompt_num, order_type=order_type, cycle_id=cycle_id,
            max_sl_distance=max_sl_distance, min_reward_risk=min_reward_risk,
            context={"market_regime": market_regime.get("regime"), "confidence": confidence},
        )
    state["last_trade_time"] = datetime.now(timezone.utc)

    if result.get("success"):
        state["last_error_feedback"] = None
        ticket = result.get("ticket")
        order_ticket = result.get("order_ticket") or ticket
        order_status = result.get("order_status") or ("placed" if order_type in ("limit", "stop") else "filled")
        is_pending_order = order_status in ("placed", "started", "request_add")
        position_ticket = result.get("position_ticket")
        exec_price = result.get("price")
        add_log(user_id, f"Order {order_status} - Order #{order_ticket} Price: {exec_price}", "SUCCESS")
        if not is_pending_order:
            state["stats"]["trades_executed"] += 1
            state["stats"]["daily_trade_count"] += 1

        # For market orders, use actual MT5 fill price as entry_price
        submitted_quote = result.get("submitted_quote")
        requested_sl = sl
        requested_tp = tp
        broker_sl = result.get("stop_loss")
        broker_tp = result.get("take_profit")
        requested_entry = submitted_quote if order_type == "market" else entry_price
        slippage = None
        if submitted_quote is not None and exec_price is not None:
            # Positive values mean the fill was worse for the strategy direction.
            slippage = round(
                (exec_price - submitted_quote) if direction == "BUY" else (submitted_quote - exec_price),
                8,
            )
        if order_type == "market":
            entry_price = exec_price
        # Persist the levels reported by the connector as broker-accepted.
        sl = broker_sl
        tp = broker_tp

        market_snap = {
            "regime": market_regime.get("regime") if market_regime else None,
            "trend": market_regime.get("trend") if market_regime else None,
            "volatility": market_regime.get("volatility") if market_regime else None,
            "atr_14": market_regime.get("atr_14") if market_regime else None,
            "entry_price": entry_price,
            "sl": sl,
            "tp": tp,
            "submitted_quote": submitted_quote,
        }

        async with AsyncSessionLocal() as db:
            trade = AutopilotTrade(
                user_id=user_id, prompt_number=prompt_num, prompt_text=prompt_text,
                symbol=symbol, direction=direction, order_type=order_type, entry_price=entry_price,
                proposed_entry_price=proposed_entry_price,
                stop_loss=sl, take_profit=tp, lot_size=result.get("volume") or lot,
                mt5_ticket=position_ticket or (None if is_pending_order else ticket),
                mt5_order_ticket=order_ticket, order_status=order_status,
                execution_price=exec_price,
                requested_price=result.get("requested_price"),
                requested_entry_price=requested_entry,
                submitted_quote=submitted_quote,
                slippage_price=slippage,
                requested_stop_loss=requested_sl,
                requested_take_profit=requested_tp,
                broker_stop_loss=broker_sl,
                broker_take_profit=broker_tp,
                execution_status="pending" if is_pending_order else "executed",
                reasoning=reasoning, confidence=confidence, ai_response=ai_response,
                raw_thinking=full_raw_response if isinstance(full_raw_response, dict) else None,
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
                decision_type="TRADE",
                market_snapshot=market_snap,
                cycle_number=state["stats"]["total_runs"],
                cycle_id=cycle_id,
            )
            db.add(trade)
            await db.flush()
            await _add_order_event(db, {
                "event_key": f"user:{user_id}:order:{order_ticket}:submitted",
                "user_id": user_id,
                "autopilot_trade_id": trade.id,
                "cycle_id": cycle_id,
                "prompt_number": prompt_num,
                "symbol": symbol,
                "event_type": "ORDER_SUBMITTED",
                "status": order_status,
                "order_ticket": order_ticket,
                "position_id": position_ticket,
                "volume": lot,
                "price": submitted_quote if submitted_quote is not None else entry_price,
                "broker_time": datetime.now(timezone.utc),
                "comment": f"[AUTOPILOT] prompt={prompt_num} cycle={cycle_id}",
            })
            await db.commit()
        await _finish_autopilot_cycle(
            user_id, cycle_id,
            "order_pending" if is_pending_order else "trade_executed",
            "Broker accepted pending order" if is_pending_order else "Broker filled the market order",
            execution_status="pending" if is_pending_order else "executed",
            mt5_ticket=position_ticket or (None if is_pending_order else ticket),
            mt5_order_ticket=order_ticket, order_status=order_status,
            provider=(_last_usage.get("provider") if _last_usage else provider),
            model=(_last_usage.get("model") if _last_usage else model),
        )
    else:
        error_msg = result.get('error', 'Unknown error')
        add_log(user_id, f"Trade failed: {error_msg}", "ERROR")
        state["last_error_feedback"] = (
            f"OrderType={order_type}, Direction={direction}, Entry={entry_price}, SL={sl}, TP={tp}, Lot={lot}. "
            f"Error: {error_msg}"
        )
        state["stats"]["error_count"] += 1

        async with AsyncSessionLocal() as db:
            attempt = AutopilotExecutionAttempt(
                user_id=user_id, cycle_number=state["stats"]["total_runs"], cycle_id=cycle_id,
                symbol=symbol, direction=direction, order_type=order_type,
                entry_price=entry_price, stop_loss=sl, take_profit=tp, lot_size=lot,
                outcome="rejected", error_message=error_msg[:500],
                error_category=_classify_execution_error(error_msg),
                source=_source,
                proposed_entry_price=proposed_entry_price,
                proposed_stop_loss=proposed_sl,
                proposed_take_profit=proposed_tp,
                requested_entry_price=entry_price,
                requested_stop_loss=sl,
                requested_take_profit=tp,
                requested_lot_size=requested_lot,
                submitted_quote=result.get("submitted_quote"),
                broker_stop_loss=result.get("broker_stop_loss"),
                broker_take_profit=result.get("broker_take_profit"),
                market_regime=market_regime.get("regime") if market_regime else None,
                provider=(_last_usage.get("provider") if _last_usage else None),
                model=(_last_usage.get("model") if _last_usage else None),
            )
            db.add(attempt)
            await db.commit()
        await _finish_autopilot_cycle(
            user_id, cycle_id, "execution_rejected", error_msg[:500],
            execution_status="rejected",
            provider=(_last_usage.get("provider") if _last_usage else provider),
            model=(_last_usage.get("model") if _last_usage else model),
        )


async def _is_market_open() -> bool:
    """Whether XAUUSD normally trades now: Sunday 23:00 UTC to Friday 22:00 UTC.

    Not used to decide whether to trade (live ticks do that); the heartbeat uses it
    to know when missing prices are a problem rather than a weekend.
    """
    now = datetime.now(timezone.utc)
    wd, hour = now.weekday(), now.hour
    if wd == 5:  # Saturday
        return False
    if wd == 4 and hour >= 22:  # Friday after 22:00 UTC
        return False
    if wd == 6 and hour < 23:  # Sunday before 23:00 UTC
        return False
    return True


async def _has_live_ticks(user_id: int, symbol: str) -> bool:
    """True if the latest one-minute candle is under 3 minutes old: the market is live.

    From the upstream branch; replaces fixed weekday hours, so holidays and early
    closes are seen too. Candle times arrive in UTC from the connector client.
    """
    try:
        data = await connector_client.get_latest_data(symbol, timeframe="1m", count=1)
    except ConnectorError as e:
        logger.warning("[user=%d] Live tick check failed: %s", user_id, e.detail)
        return False
    candles = data.get("data") or []
    last_time = candles[-1].get("time") if candles else None
    if not last_time:
        return False
    age_seconds = (datetime.now(timezone.utc) - datetime.fromtimestamp(last_time, tz=timezone.utc)).total_seconds()
    return age_seconds < 180


async def _resolve_prompt_text(db, user_id: int, prompt_num: int) -> str:
    """Resolve the actual prompt text for a given prompt_number.

    Tries in order:
      1. Default prompts from prompt_list.txt (numbered 1-N)
      2. User's custom prompts from user_prompts table
      3. Fallback placeholder
    """
    # Custom prompt IDs are negative in autopilot records; resolve them by row ID.
    if prompt_num < 0:
        result = await db.execute(
            select(UserPrompt.content).where(
                UserPrompt.user_id == user_id,
                UserPrompt.id == abs(prompt_num),
            )
        )
        content = result.scalar_one_or_none()
        if content:
            return content

    # 1. Try default prompts from prompt_list.txt
    default_prompts = load_prompts()
    for p in default_prompts:
        # Format: "1. Analyze XAUUSD..."
        try:
            num_str = p.split(".")[0].strip()
            if int(num_str) == prompt_num:
                return p
        except (ValueError, IndexError):
            continue

    # 2. Try user's custom prompts from DB
    try:
        from sqlalchemy import text as _txt
        result = await db.execute(_txt(
            "SELECT content FROM user_prompts WHERE user_id = :uid LIMIT 1 OFFSET :offset"
        ), {"uid": user_id, "offset": prompt_num - 1})
        row = result.fetchone()
        if row and row[0]:
            return row[0]
    except Exception:
        logger.warning("Could not look up custom prompt #%s for user %s", prompt_num, user_id, exc_info=True)

    # 3. Fallback
    return f"(synced from MT5 - P#{prompt_num})"


async def sync_all_trades_from_mt5(user_id: int, hours: int = 2160):
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

            positions_data = await connector_client.get_positions()
            if not positions_data or not positions_data.get("success"):
                add_log(user_id, "Full sync: open-position check failed; close results deferred", "WARNING")
                return
            open_position_ids = {
                str(position.get("ticket"))
                for position in positions_data.get("positions", [])
                if position.get("ticket") is not None
            }

            # Keep every deal: a position can have multiple partial-close deals.
            position_deals: dict[str, list[dict]] = {}
            for deal in deals:
                pid = deal.get("position_id")
                if pid is not None:
                    position_deals.setdefault(str(pid), []).append(deal)

            created = 0
            updated = 0
            for pid, grouped_deals in position_deals.items():
                open_deals = [deal for deal in grouped_deals if deal.get("entry") == "OPEN"]
                close_deals = [deal for deal in grouped_deals if deal.get("entry") in ("CLOSE", "INOUT", "OUT_BY")]
                if not open_deals:
                    continue
                open_deals.sort(key=lambda item: str(item.get("time") or ""))
                close_deals.sort(key=lambda item: str(item.get("time") or ""))
                opn = open_deals[0]
                close = close_deals[-1] if close_deals else {}
                # Position ID is the stable key used by MT5's open-position list
                # and all related deal rows; an order/deal ticket may differ.
                ticket = int(pid)
                is_open = pid in open_position_ids or str(ticket) in open_position_ids
                comment = opn.get("comment", "") or close.get("comment", "")
                custom_prompt_match = re.search(r"\[AUTOPILOT\]\s+C(\d+)\b", comment, re.I)
                prompt_match = re.search(r"\[AUTOPILOT\]\s*(?:Custom-|P)?(\d+)", comment, re.I)
                if not custom_prompt_match and not prompt_match:
                    continue
                prompt_num = -int(custom_prompt_match.group(1)) if custom_prompt_match else int(prompt_match.group(1))
                cycle_token_match = re.search(r"\bX([0-9a-f]{12})\b", comment, re.I)
                linked_cycle_id = None
                if cycle_token_match:
                    linked_cycle_id = (await db.execute(
                        select(AutopilotCycle.cycle_id).where(
                            AutopilotCycle.user_id == user_id,
                            AutopilotCycle.cycle_id.like(f"{cycle_token_match.group(1).lower()}%"),
                        )
                    )).scalars().first()

                # Try to match existing local trade by ticket or position_id
                existing_trade = None
                if linked_cycle_id:
                    result_set = await db.execute(
                        select(AutopilotTrade).where(
                            AutopilotTrade.user_id == user_id,
                            AutopilotTrade.cycle_id == linked_cycle_id,
                        )
                    )
                    existing_trade = result_set.scalar_one_or_none()
                if existing_trade is None:
                    result_set = await db.execute(
                        select(AutopilotTrade).where(
                            AutopilotTrade.user_id == user_id,
                            AutopilotTrade.mt5_ticket == ticket,
                        )
                    )
                    existing_trade = result_set.scalar_one_or_none()
                if existing_trade is None and opn.get("order_ticket") is not None:
                    result_set = await db.execute(
                        select(AutopilotTrade).where(
                            AutopilotTrade.user_id == user_id,
                            AutopilotTrade.mt5_order_ticket == int(opn["order_ticket"]),
                        )
                    )
                    existing_trade = result_set.scalar_one_or_none()

                profit = None
                if close_deals and not is_open:
                    profit = sum(
                        float(deal.get("profit") or 0.0)
                        + float(deal.get("swap") or 0.0)
                        + float(deal.get("commission") or 0.0)
                        for deal in close_deals
                    )
                exit_price = close.get("price")
                closed_at_str = close.get("time") if profit is not None else None
                volume = opn.get("volume", 0) or 0
                symbol = opn.get("symbol", "")
                entry_price = opn.get("price", 0) or 0
                direction = opn.get("direction", "BUY").upper()
                entry_time_str = opn.get("time")
                if profit is None:
                    res_type, exit_reason, exit_reason_source = None, None, None
                else:
                    res_type, exit_reason, exit_reason_source = _classify_exit_reason(close_deals, profit)

                def _parse_ts(s):
                    if not s:
                        return None
                    return datetime.strptime(s, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)

                if existing_trade:
                    tracked_trade = existing_trade
                    if linked_cycle_id and not existing_trade.cycle_id:
                        existing_trade.cycle_id = linked_cycle_id
                    if profit is None:
                        # The position is still open, possibly after a partial exit.
                        was_closed = existing_trade.result is not None or existing_trade.profit is not None
                        existing_trade.profit = None
                        existing_trade.exit_price = None
                        existing_trade.result = None
                        existing_trade.exit_reason = None
                        existing_trade.exit_reason_source = None
                        existing_trade.closed_at = None
                        existing_trade.duration_minutes = None
                        if was_closed:
                            updated += 1
                    elif (
                        existing_trade.result is None
                        or existing_trade.profit is None
                        or existing_trade.profit != profit
                        or existing_trade.exit_reason != exit_reason
                        or existing_trade.exit_reason_source != exit_reason_source
                    ):
                        existing_trade.profit = profit
                        existing_trade.exit_price = exit_price
                        existing_trade.result = res_type
                        existing_trade.exit_reason = exit_reason
                        existing_trade.exit_reason_source = exit_reason_source
                        existing_trade.closed_at = _parse_ts(closed_at_str)
                        if existing_trade.executed_at and existing_trade.closed_at:
                            diff = existing_trade.closed_at - existing_trade.executed_at
                            existing_trade.duration_minutes = int(diff.total_seconds() / 60)
                        updated += 1
                    if existing_trade.cycle_id:
                        cycle = await db.get(AutopilotCycle, existing_trade.cycle_id)
                        if cycle:
                            cycle.trade_result = existing_trade.result
                            cycle.exit_reason = existing_trade.exit_reason
                            cycle.exit_reason_source = existing_trade.exit_reason_source
                            cycle.realized_profit = existing_trade.profit
                            cycle.trade_closed_at = existing_trade.closed_at
                            cycle.duration_minutes = existing_trade.duration_minutes
                else:
                    # Resolve actual prompt text from prompt_number
                    resolved_text = await _resolve_prompt_text(db, user_id, prompt_num)

                    executed_at = _parse_ts(entry_time_str)
                    closed_at = _parse_ts(closed_at_str)
                    duration = int((closed_at - executed_at).total_seconds() / 60) if executed_at and closed_at else None
                    new_trade = AutopilotTrade(
                        user_id=user_id,
                        prompt_number=prompt_num,
                        prompt_text=resolved_text,
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
                        exit_reason=exit_reason,
                        exit_reason_source=exit_reason_source,
                        profit=profit,
                        exit_price=exit_price,
                        closed_at=closed_at,
                        duration_minutes=duration,
                        source="mt5_sync",
                        cycle_id=linked_cycle_id,
                    )
                    db.add(new_trade)
                    await db.flush()
                    tracked_trade = new_trade
                    if linked_cycle_id and profit is not None:
                        cycle = await db.get(AutopilotCycle, linked_cycle_id)
                        if cycle:
                            cycle.trade_result = res_type
                            cycle.exit_reason = exit_reason
                            cycle.exit_reason_source = exit_reason_source
                            cycle.realized_profit = profit
                            cycle.trade_closed_at = closed_at
                            cycle.duration_minutes = duration
                    created += 1

                for deal in grouped_deals:
                    deal_ticket = deal.get("deal_ticket")
                    if deal_ticket is None:
                        continue
                    entry_type = deal.get("entry") or "UNKNOWN"
                    event_type = {
                        "OPEN": "DEAL_OPEN", "CLOSE": "DEAL_CLOSE",
                        "INOUT": "DEAL_REVERSAL", "OUT_BY": "DEAL_CLOSE_BY",
                    }.get(entry_type, "DEAL_OTHER")
                    await _add_order_event(db, {
                        "event_key": f"user:{user_id}:deal:{deal_ticket}",
                        "user_id": user_id,
                        "autopilot_trade_id": tracked_trade.id,
                        "cycle_id": tracked_trade.cycle_id,
                        "prompt_number": tracked_trade.prompt_number,
                        "symbol": deal.get("symbol") or tracked_trade.symbol,
                        "event_type": event_type,
                        "status": "recorded",
                        "order_ticket": deal.get("order_ticket", deal.get("ticket")),
                        "deal_ticket": int(deal_ticket),
                        "position_id": deal.get("position_id"),
                        "entry_type": entry_type,
                        "reason_code": deal.get("reason_code"),
                        "reason": deal.get("reason") or "UNKNOWN",
                        "volume": deal.get("volume"),
                        "price": deal.get("price"),
                        "profit": deal.get("profit"),
                        "swap": deal.get("swap"),
                        "commission": deal.get("commission"),
                        "broker_time": _parse_broker_datetime(deal.get("time_msc") or deal.get("time")),
                        "comment": (deal.get("comment") or "")[:256],
                    })

            await db.commit()
            if created > 0 or updated > 0:
                add_log(user_id, f"Full sync: {created} created, {updated} updated from MT5 history", "INFO")

    except Exception as e:
        add_log(user_id, f"Full sync failed: {str(e)}", "ERROR")


async def _sync_trade_results_unlocked(user_id: int) -> int:
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AutopilotSettings).where(AutopilotSettings.user_id == user_id))
            settings_obj = result.scalar_one_or_none()
            if not settings_obj:
                return
            reconcile_since = datetime.now(timezone.utc) - timedelta(days=90)
            result = await db.execute(
                select(AutopilotTrade).where(
                    AutopilotTrade.user_id == user_id,
                    or_(
                        AutopilotTrade.execution_status == "pending",
                        and_(
                            AutopilotTrade.execution_status == "executed",
                            AutopilotTrade.order_status == "partially_filled_active",
                        ),
                        and_(
                            AutopilotTrade.execution_status == "executed",
                            or_(AutopilotTrade.result.is_(None), AutopilotTrade.executed_at >= reconcile_since),
                        ),
                    ),
                )
            )
            open_trades = result.scalars().all()
            if not open_trades:
                return 0

            # Dynamically determine the history window to check based on the oldest open trade
            try:
                oldest_trade = min(open_trades, key=lambda t: t.executed_at)
                executed_at = _ensure_aware(oldest_trade.executed_at)
                now_utc = datetime.now(timezone.utc)
                hours_diff = int((now_utc - executed_at).total_seconds() / 3600) + 12  # add 12h buffer
                sync_hours = max(hours_diff, 24)
            except Exception:  # swallow-ok: no usable trade time; the last 24 hours are checked instead
                sync_hours = 24

            try:
                history_data = await connector_client.get_history(hours=sync_hours)
            except Exception as e:
                add_log(user_id, f"History fetch failed: {str(e)}", "ERROR")
                return 0

            deals = history_data.get("deals", []) if history_data.get("success") else []

            pending_trades = [
                t for t in open_trades
                if t.execution_status == "pending" or t.order_status == "partially_filled_active"
            ]
            active_orders, historical_orders = [], []
            if pending_trades:
                try:
                    active_data = await connector_client.get_orders()
                    if active_data.get("success"):
                        active_orders = active_data.get("orders", [])
                except Exception as e:
                    add_log(user_id, f"Active-order fetch failed: {type(e).__name__}", "WARNING")
                try:
                    history_orders_data = await connector_client.get_order_history(hours=sync_hours)
                    if history_orders_data.get("success"):
                        historical_orders = history_orders_data.get("orders", [])
                except Exception as e:
                    add_log(user_id, f"Order-history fetch failed: {type(e).__name__}", "WARNING")

            # Apply the latest broker state for each pending order. The order
            # ticket is intentionally distinct from a resulting position ID.
            orders_by_ticket = {}
            for order in active_orders + historical_orders:
                ticket_value = order.get("order_ticket", order.get("ticket"))
                if ticket_value is not None:
                    orders_by_ticket[str(ticket_value)] = order
            order_terminal_states = {
                "filled": "executed", "partially_filled": "executed",
                "cancelled": "cancelled", "canceled": "cancelled",
                "expired": "expired", "rejected": "rejected",
            }
            for trade in pending_trades:
                broker_order = orders_by_ticket.get(str(trade.mt5_order_ticket)) if trade.mt5_order_ticket is not None else None
                if not broker_order:
                    continue
                if broker_order.get("sl") is not None:
                    trade.broker_stop_loss = trade.stop_loss = float(broker_order["sl"])
                if broker_order.get("tp") is not None:
                    trade.broker_take_profit = trade.take_profit = float(broker_order["tp"])
                broker_status = (broker_order.get("status") or "unknown").lower()
                if broker_status == "partially_filled" and broker_order.get("is_active"):
                    broker_status = "partially_filled_active"
                was_unfilled = trade.execution_status == "pending"
                trade.order_status = broker_status
                state_marker = broker_order.get("done_time") or broker_order.get("setup_time") or "active"
                remaining_volume = broker_order.get("volume_current")
                event_key = (
                    f"user:{user_id}:order:{trade.mt5_order_ticket}:{broker_status}:"
                    f"{remaining_volume}:{state_marker}"
                )
                await _add_order_event(db, {
                    "event_key": event_key[:180],
                    "user_id": user_id,
                    "autopilot_trade_id": trade.id,
                    "cycle_id": trade.cycle_id,
                    "prompt_number": trade.prompt_number,
                    "symbol": trade.symbol,
                    "event_type": "ORDER_STATE",
                    "status": broker_status,
                    "order_ticket": trade.mt5_order_ticket,
                    "position_id": broker_order.get("position_id"),
                    "volume": broker_order.get("volume_initial"),
                    "price": broker_order.get("price_open"),
                    "broker_time": _parse_broker_datetime(broker_order.get("done_time") or broker_order.get("setup_time")),
                    "comment": (broker_order.get("comment") or "")[:256],
                })
                terminal_execution_status = order_terminal_states.get(broker_status)
                cycle_prefix = (trade.cycle_id or "")[:12].lower()
                matching_opens = [
                    deal for deal in deals
                    if deal.get("entry") == "OPEN"
                    and (
                        (deal.get("order_ticket") is not None and str(deal.get("order_ticket")) == str(trade.mt5_order_ticket))
                        or (cycle_prefix and f"x{cycle_prefix}" in (deal.get("comment") or "").lower())
                    )
                ]
                matching_open = matching_opens[0] if matching_opens else None
                if terminal_execution_status:
                    # A broker may cancel/expire the unfilled remainder after
                    # a partial fill. Preserve its real position for P&L sync.
                    trade.execution_status = (
                        "executed" if matching_opens and terminal_execution_status in ("cancelled", "expired")
                        else terminal_execution_status
                    )
                    done_time = broker_order.get("done_time")
                    try:
                        trade.order_completed_at = _ensure_aware(
                            datetime.strptime(done_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                        ) if done_time else _ensure_aware(datetime.now(timezone.utc))
                    except (TypeError, ValueError):
                        trade.order_completed_at = _ensure_aware(datetime.now(timezone.utc))
                    pos_id = broker_order.get("position_id")
                    if pos_id is not None and terminal_execution_status == "executed":
                        trade.mt5_ticket = int(pos_id)
                    if matching_open:
                        if matching_open.get("position_id") is not None:
                            trade.mt5_ticket = int(matching_open["position_id"])
                        if matching_open.get("price") is not None:
                            trade.execution_price = float(matching_open["price"])
                            if trade.order_type != "market" and trade.requested_entry_price is not None:
                                trade.slippage_price = round(
                                    (trade.execution_price - trade.requested_entry_price)
                                    if trade.direction == "BUY"
                                    else (trade.requested_entry_price - trade.execution_price),
                                    8,
                                )
                        opened_at = matching_open.get("time")
                        if opened_at:
                            try:
                                trade.executed_at = _ensure_aware(
                                    datetime.strptime(opened_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                                )
                            except ValueError:
                                pass
                    if trade.cycle_id:
                        cycle = await db.get(AutopilotCycle, trade.cycle_id)
                        if cycle:
                            cycle.order_status = broker_status
                            cycle.order_completed_at = trade.order_completed_at
                            cycle.execution_status = trade.execution_status
                            cycle.mt5_ticket = trade.mt5_ticket
                            cycle.outcome = f"order_{broker_status}"
                elif broker_status in ("partially_filled", "partially_filled_active"):
                    trade.execution_status = "executed"
                    if matching_open:
                        if matching_open.get("position_id") is not None:
                            trade.mt5_ticket = int(matching_open["position_id"])
                        if matching_open.get("price") is not None:
                            trade.execution_price = float(matching_open["price"])
                            if trade.order_type != "market" and trade.requested_entry_price is not None:
                                trade.slippage_price = round(
                                    (trade.execution_price - trade.requested_entry_price)
                                    if trade.direction == "BUY"
                                    else (trade.requested_entry_price - trade.execution_price),
                                    8,
                                )
                if was_unfilled and trade.execution_status == "executed":
                    state = _get_state(user_id)
                    today = datetime.now(timezone.utc).date().isoformat()
                    if state["stats"].get("daily_reset_date") == today:
                        state["stats"]["daily_trade_count"] += 1
                    state["stats"]["trades_executed"] += 1

            if not deals:
                await db.commit()
                return 0

            # A CLOSE deal may be only a partial close. Confirm the position is
            # absent before treating accumulated close deals as its final P&L.
            try:
                positions_data = await connector_client.get_positions()
            except Exception as e:
                add_log(user_id, f"Open-position check failed; close results deferred: {type(e).__name__}", "WARNING")
                await db.commit()
                return 0
            if not positions_data or not positions_data.get("success"):
                add_log(user_id, "Open-position check failed; close results deferred", "WARNING")
                await db.commit()
                return 0
            open_position_ids = {
                str(position.get("ticket"))
                for position in positions_data.get("positions", [])
                if position.get("ticket") is not None
            }
            positions_by_ticket = {
                str(position.get("ticket")): position
                for position in positions_data.get("positions", [])
                if position.get("ticket") is not None
            }

            updated_count = 0
            for trade in open_trades:
                trade_ids = {str(trade.mt5_ticket)} if trade.mt5_ticket is not None else set()
                if trade.mt5_order_ticket is not None:
                    trade_ids.add(str(trade.mt5_order_ticket))
                cycle_prefix = (trade.cycle_id or "")[:12].lower()
                if cycle_prefix:
                    for deal in deals:
                        if deal.get("entry") != "OPEN":
                            continue
                        deal_comment = (deal.get("comment") or "").lower()
                        if f"x{cycle_prefix}" in deal_comment:
                            if deal.get("position_id") is not None:
                                trade_ids.add(str(deal["position_id"]))
                            if deal.get("ticket") is not None:
                                trade_ids.add(str(deal["ticket"]))
                if trade.mt5_order_ticket is not None:
                    for deal in deals:
                        if (
                            deal.get("entry") == "OPEN"
                            and str(deal.get("order_ticket", deal.get("ticket"))) == str(trade.mt5_order_ticket)
                            and deal.get("position_id") is not None
                        ):
                            trade_ids.add(str(deal["position_id"]))
                matching_closes = [
                    deal for deal in deals
                    if deal.get("entry") in ("CLOSE", "INOUT", "OUT_BY")
                    and trade_ids.intersection({str(deal.get("position_id")), str(deal.get("ticket"))})
                ]
                matching_deals = [
                    deal for deal in deals
                    if trade_ids.intersection({str(deal.get("position_id")), str(deal.get("order_ticket", deal.get("ticket")))})
                ]
                if cycle_prefix:
                    matching_deals.extend(
                        deal for deal in deals
                        if f"x{cycle_prefix}" in (deal.get("comment") or "").lower()
                        and deal not in matching_deals
                    )
                for deal in matching_deals:
                    deal_ticket = deal.get("deal_ticket")
                    if deal_ticket is None:
                        continue
                    entry_type = deal.get("entry") or "UNKNOWN"
                    event_type = {
                        "OPEN": "DEAL_OPEN", "CLOSE": "DEAL_CLOSE",
                        "INOUT": "DEAL_REVERSAL", "OUT_BY": "DEAL_CLOSE_BY",
                    }.get(entry_type, "DEAL_OTHER")
                    await _add_order_event(db, {
                        "event_key": f"user:{user_id}:deal:{deal_ticket}",
                        "user_id": user_id,
                        "autopilot_trade_id": trade.id,
                        "cycle_id": trade.cycle_id,
                        "prompt_number": trade.prompt_number,
                        "symbol": deal.get("symbol") or trade.symbol,
                        "event_type": event_type,
                        "status": "recorded",
                        "order_ticket": deal.get("order_ticket", deal.get("ticket")),
                        "deal_ticket": int(deal_ticket),
                        "position_id": deal.get("position_id"),
                        "entry_type": entry_type,
                        "reason_code": deal.get("reason_code"),
                        "reason": deal.get("reason") or "UNKNOWN",
                        "volume": deal.get("volume"),
                        "price": deal.get("price"),
                        "profit": deal.get("profit"),
                        "swap": deal.get("swap"),
                        "commission": deal.get("commission"),
                        "broker_time": _parse_broker_datetime(deal.get("time_msc") or deal.get("time")),
                        "comment": (deal.get("comment") or "")[:256],
                    })
                if trade.execution_status == "pending":
                    continue
                if trade.order_status == "partially_filled_active":
                    # The currently filled position may have closed while the
                    # residual volume is still waiting to fill.
                    continue
                if trade_ids.intersection(open_position_ids):
                    matching_position = next(
                        (positions_by_ticket[ticket_id] for ticket_id in trade_ids if ticket_id in positions_by_ticket),
                        None,
                    )
                    if matching_position:
                        actual_position_entry = matching_position.get("entry_price")
                        if actual_position_entry is not None:
                            trade.execution_price = float(actual_position_entry)
                            if trade.order_type != "market" and trade.requested_entry_price is not None:
                                trade.slippage_price = round(
                                    (trade.execution_price - trade.requested_entry_price)
                                    if trade.direction == "BUY"
                                    else (trade.requested_entry_price - trade.execution_price),
                                    8,
                                )
                        if matching_position.get("sl") is not None:
                            trade.broker_stop_loss = trade.stop_loss = float(matching_position["sl"])
                        if matching_position.get("tp") is not None:
                            trade.broker_take_profit = trade.take_profit = float(matching_position["tp"])
                    # Clear any stale partial-close classification from earlier sync versions.
                    if trade.result is not None or trade.profit is not None:
                        trade.profit = None
                        trade.exit_price = None
                        trade.result = None
                        trade.closed_at = None
                        trade.duration_minutes = None
                        if trade.cycle_id:
                            cycle = await db.get(AutopilotCycle, trade.cycle_id)
                            if cycle:
                                cycle.trade_result = None
                                cycle.exit_reason = None
                                cycle.exit_reason_source = None
                                cycle.realized_profit = None
                                cycle.trade_closed_at = None
                                cycle.duration_minutes = None
                        updated_count += 1
                    continue

                if matching_closes:
                    matching_closes.sort(key=lambda item: str(item.get("time") or ""))
                    latest_close = matching_closes[-1]
                    # Include broker-reported swap/commission when supplied.
                    profit = sum(
                        float(deal.get("profit") or 0.0)
                        + float(deal.get("swap") or 0.0)
                        + float(deal.get("commission") or 0.0)
                        for deal in matching_closes
                    )
                    exit_price = latest_close.get("price")
                    closed_at_str = latest_close.get("time")
                    res_type, exit_reason, exit_reason_source = _classify_exit_reason(matching_closes, profit)

                    was_changed = (
                        trade.result != res_type
                        or trade.exit_reason != exit_reason
                        or trade.exit_reason_source != exit_reason_source
                        or trade.profit != profit
                        or trade.closed_at is None
                    )
                    trade.profit = profit
                    trade.exit_price = exit_price
                    trade.result = res_type
                    trade.exit_reason = exit_reason
                    trade.exit_reason_source = exit_reason_source
                    if closed_at_str:
                        trade.closed_at = _ensure_aware(datetime.strptime(closed_at_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc))
                        trade.executed_at = _ensure_aware(trade.executed_at)
                        if trade.executed_at and trade.closed_at:
                            diff = trade.closed_at - trade.executed_at
                            trade.duration_minutes = int(diff.total_seconds() / 60)
                    if trade.cycle_id:
                        cycle = await db.get(AutopilotCycle, trade.cycle_id)
                        if cycle:
                            cycle.trade_result = res_type
                            cycle.exit_reason = exit_reason
                            cycle.exit_reason_source = exit_reason_source
                            cycle.realized_profit = profit
                            cycle.trade_closed_at = trade.closed_at
                            cycle.duration_minutes = trade.duration_minutes
                    if was_changed:
                        updated_count += 1
                        add_log(user_id, f"Trade #{trade.mt5_ticket} | Profit: ${profit:.2f} | {res_type}", "SUCCESS" if profit > 0 else "WARNING")

            await db.commit()
            if updated_count > 0:
                today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                daily_pnl = await db.execute(
                    select(func.coalesce(func.sum(AutopilotTrade.profit), 0.0)).where(
                        AutopilotTrade.user_id == user_id,
                        AutopilotTrade.closed_at >= today_start,
                        AutopilotTrade.result.is_not(None),
                    )
                )
                _get_state(user_id)["stats"]["daily_pnl"] = float(daily_pnl.scalar() or 0.0)
            return updated_count

    except Exception as e:
        add_log(user_id, f"Failed to sync trade results: {str(e)}", "ERROR")
        return 0


async def sync_trade_results(user_id: int) -> int:
    """Serialize periodic and Autopilot-loop syncs so outcomes are applied once."""
    lock = _trade_sync_locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        return await _sync_trade_results_unlocked(user_id)


async def _settings_row(user_id: int):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(AutopilotSettings).where(AutopilotSettings.user_id == user_id))
        return result.scalar_one_or_none()


def _loss_limit_hit(settings_row, daily_pnl: float) -> bool:
    """The autopilot's own daily loss brake, a dollar amount on its own trades. Can be switched off."""
    if settings_row is None or not getattr(settings_row, "daily_loss_limit_enabled", True):
        return False
    return settings_row.max_daily_loss is not None and daily_pnl <= settings_row.max_daily_loss


async def _start_cycle_record(user_id: int) -> Optional[str]:
    """Record that an attempt started (upstream's cycle ledger). None if it cannot be saved."""
    state = _get_state(user_id)
    state["stats"]["total_runs"] += 1
    state["stats"]["last_run"] = datetime.now(timezone.utc).isoformat()
    cycle_id = str(uuid.uuid4())
    try:
        async with AsyncSessionLocal() as db:
            db.add(AutopilotCycle(cycle_id=cycle_id, user_id=user_id, cycle_number=state["stats"]["total_runs"],
                                  symbol="unknown", status="running"))
            await db.commit()
    except Exception:
        logger.exception("[user=%d] Could not record the start of an autopilot attempt", user_id)
        return None
    state["active_cycle_id"] = cycle_id
    return cycle_id


async def _loop_iteration(user_id: int) -> None:
    """One pass: cooldown, live market, result sync, the daily brake, then a cycle.

    Every pass is recorded in autopilot_cycles with its outcome, including passes
    that stop at a gate, so reports can say why nothing traded.
    """
    state = _get_state(user_id)
    s = await _settings_row(user_id)
    cooldown_mins = (s.cooldown_minutes or 0) if s else 0
    symbol = (s.symbol if s else None) or "XAUUSD"

    cycle_id = await _start_cycle_record(user_id)
    if cycle_id is None:
        # Never place an order whose attempt cannot be audited.
        state["stats"]["error_count"] += 1
        add_log(user_id, "Attempt tracking unavailable; skipping this cycle", "ERROR")
        return
    await _update_autopilot_cycle(cycle_id, symbol=symbol)

    last_trade = state.get("last_trade_time")
    if cooldown_mins > 0 and last_trade:
        elapsed_mins = (datetime.now(timezone.utc) - last_trade).total_seconds() / 60
        if elapsed_mins < cooldown_mins:
            add_log(user_id, f"Cooldown ({elapsed_mins:.0f}/{cooldown_mins} min). Skipping cycle.", "INFO")
            state["stats"]["skipped_count"] += 1
            await _finish_autopilot_cycle(user_id, cycle_id, "skipped_cooldown",
                                          f"Cooldown active ({elapsed_mins:.0f}/{cooldown_mins} minutes)")
            return

    if not connector_client.configured:
        add_log(user_id, "No MT5 connector is configured on the server. Skipping cycle.", "WARNING")
        state["stats"]["skipped_count"] += 1
        await _finish_autopilot_cycle(user_id, cycle_id, "skipped_no_connector", "No MT5 connector configured")
        return

    # Live prices, not fixed weekday hours, decide whether the market is open (upstream).
    if not await _has_live_ticks(user_id, symbol):
        if not state.get("market_closed"):
            add_log(user_id, f"Market paused: no live prices for {symbol}. Waiting for them to return.", "INFO")
            state["market_closed"] = True
        state["stats"]["skipped_count"] += 1
        await _finish_autopilot_cycle(user_id, cycle_id, "skipped_stale_market_data",
                                      f"No fresh one-minute candle for {symbol}")
        return
    if state.get("market_closed"):
        add_log(user_id, "Live prices are back. Resuming.", "INFO")
    state["market_closed"] = False

    await sync_trade_results(user_id)
    _, daily_pnl = await _refresh_daily_stats(user_id)
    today = datetime.now(timezone.utc).date().isoformat()

    if _loss_limit_hit(s, daily_pnl):
        # Pause for the rest of the UTC day, log it once, and carry on tomorrow by itself.
        # It used to switch the autopilot off for good while the page still said running.
        if state.get("paused_day") != today:
            add_log(user_id, f"Daily loss limit reached: {daily_pnl:+.2f} against {s.max_daily_loss:+.2f}. "
                             "Paused until 00:00 UTC.", "WARNING")
            state["paused_day"] = today
        state["stats"]["paused_reason"] = f"Daily loss limit reached ({daily_pnl:+.2f}). Resumes at 00:00 UTC."
        state["stats"]["skipped_count"] += 1
        await _finish_autopilot_cycle(user_id, cycle_id, "daily_loss_limit",
                                      f"Daily realized P&L {daily_pnl:.2f} reached limit {s.max_daily_loss:.2f}")
        return
    if state.get("paused_day"):
        add_log(user_id, "Daily loss pause over. Resuming.", "INFO")
        state["paused_day"] = None
    state["stats"]["paused_reason"] = None

    try:
        from ..core.strategy_scorer import update_strategy_scores
        await update_strategy_scores()
    except Exception:
        logger.warning("Could not update the strategy scoreboard", exc_info=True)

    try:
        await run_autopilot_cycle(user_id, cycle_id=cycle_id)
    except Exception as e:
        if state.get("active_cycle_id") == cycle_id:
            await _finish_autopilot_cycle(user_id, cycle_id, "cycle_crashed", type(e).__name__)
        raise


async def autopilot_loop(user_id: int):
    """Run cycles until stopped. An error in one cycle is logged and the next cycle still runs.

    Any unexpected error used to end this loop silently, leaving the page saying
    running while nothing ran.
    """
    state = _get_state(user_id)
    # A process restart can interrupt a cycle after its durable start record.
    # Close those rows explicitly so reporting never mistakes them for active work.
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AutopilotCycle).where(
                    AutopilotCycle.user_id == user_id,
                    AutopilotCycle.status == "running",
                )
            )
            for cycle in result.scalars().all():
                cycle.status = "completed"
                cycle.outcome = "interrupted_by_restart"
                cycle.outcome_reason = "Process restarted before cycle completion"
                cycle.completed_at = datetime.now(timezone.utc)
            await db.commit()
    except Exception as exc:
        logger.warning("Could not reconcile interrupted autopilot cycles: %s", type(exc).__name__)
    # Full back-sync from MT5 history at startup (captures trades that were missed)
    try:
        await _refresh_daily_stats(user_id)
        await _rebuild_stats(user_id)
        if connector_client.configured and await _settings_row(user_id):
            await sync_all_trades_from_mt5(user_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        add_log(user_id, f"Start-up sync failed, continuing: {e}", "ERROR")

    try:
        while state["enabled"]:
            # The heartbeat reads this: a pass that skipped its cycle still counts as alive.
            state["last_beat"] = datetime.now(timezone.utc)
            if await trading_halted():
                # The kill switch also stops autopilots directly; this catches any it missed.
                add_log(user_id, "Trading is stopped (kill switch). Autopilot stopping.", "WARNING")
                await _disable_in_db(user_id)
                state["enabled"] = False
                state["running"] = False
                state["stats"]["stopped_reason"] = "Trading was stopped with the kill switch."
                break
            if state["running"]:
                try:
                    await _loop_iteration(user_id)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    import traceback as _tb
                    state["stats"]["error_count"] += 1
                    add_log(user_id, f"Cycle failed, the next one will still run: {e}\n{_tb.format_exc()}", "ERROR")
            if not state["enabled"]:
                break
            s = None
            try:
                s = await _settings_row(user_id)
            except Exception:
                logger.warning("[user=%d] Could not read the cycle interval, waiting 300s", user_id, exc_info=True)
            await asyncio.sleep(s.interval_seconds if s and s.interval_seconds else 300)
    except asyncio.CancelledError:
        interrupted_cycle_id = state.get("active_cycle_id")
        if interrupted_cycle_id:
            await _finish_autopilot_cycle(
                user_id,
                interrupted_cycle_id,
                "cycle_cancelled",
                "Autopilot stopped while the cycle was running",
            )
        add_log(user_id, "Autopilot loop cancelled.", "INFO")
        raise


def _watch_loop(user_id: int, task: "asyncio.Task") -> None:
    """If the loop ever ends other than by Stop, say so and show it as stopped."""
    def done(t: "asyncio.Task") -> None:
        state = _get_state(user_id)
        if t.cancelled():
            return
        error = t.exception()
        if error is None and not state["enabled"]:
            return
        state["running"] = False
        state["stats"]["stopped_reason"] = f"The autopilot loop stopped unexpectedly: {error or 'ended'}"
        add_log(user_id, state["stats"]["stopped_reason"], "ERROR")
    task.add_done_callback(done)


# Pydantic models
class AutopilotConfig(BaseModel):
    enabled: bool = False
    interval_seconds: int = 300
    default_lot: float = 0.10
    max_trades_per_day: int = 10
    cooldown_minutes: int = 5
    max_daily_loss: float = -50.0
    daily_loss_limit_enabled: bool = True
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
    mt5_order_ticket: Optional[int] = None
    order_status: Optional[str] = None
    execution_status: Optional[str] = None
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
async def _disable_in_db(user_id: int) -> None:
    """Mark the autopilot off in the database, so it is not restarted at boot."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(AutopilotSettings).where(AutopilotSettings.user_id == user_id))
        s = result.scalar_one_or_none()
        if s and s.enabled:
            s.enabled = False
            await db.commit()


async def _stop_autopilot_internal(user_id: int, message: str = "Autopilot STOPPED",
                                   stopped_reason: Optional[str] = None) -> None:
    """Stop one user's autopilot: off in the database, loop cancelled."""
    state = _get_state(user_id)
    await _disable_in_db(user_id)
    state["enabled"] = False
    state["running"] = False
    if stopped_reason:
        state["stats"]["stopped_reason"] = stopped_reason
    # Cancel the background asyncio task so it cleanly exits the loop
    existing_task = state.get("task")
    if existing_task and not existing_task.done() and existing_task is not asyncio.current_task():
        existing_task.cancel()
        try:
            await existing_task
        except asyncio.CancelledError:
            pass
    state["task"] = None
    add_log(user_id, message)


async def stop_all_autopilots(reason: str) -> list[int]:
    """The kill switch: stop every autopilot, running or merely enabled. Returns the user ids."""
    async with AsyncSessionLocal() as db:
        enabled = (await db.execute(select(AutopilotSettings.user_id).where(AutopilotSettings.enabled == True))).scalars().all()  # noqa: E712
    user_ids = sorted(set(enabled) | {uid for uid, st in _user_states.items() if st.get("enabled") or st.get("task")})
    for uid in user_ids:
        try:
            await _stop_autopilot_internal(uid, f"Autopilot STOPPED by the kill switch: {reason}",
                                           stopped_reason="Trading was stopped with the kill switch.")
        except Exception:
            logger.exception("[user=%s] Could not stop the autopilot for the kill switch", uid)
    return user_ids


async def _start_autopilot_internal(user_id: int) -> bool:
    """Start autopilot for a given user_id. Used for auto-restart on server boot."""
    if await trading_halted():
        logger.warning("[user=%d] Not restarting the autopilot: trading is stopped (kill switch)", user_id)
        return False
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
            state["stats"]["stopped_reason"] = None
            state["task"] = asyncio.create_task(autopilot_loop(user_id))
            _watch_loop(user_id, state["task"])
    add_log(user_id, "Autopilot auto-restarted after server boot")
    return True


# Endpoints
@router.post("/start")
async def start_autopilot(current_user: dict = Depends(require_trader)):
    user_id = current_user["id"]
    state = _get_state(user_id)
    if await trading_halted():
        raise HTTPException(status_code=409, detail="Trading is stopped (kill switch). An admin must resume it first.")

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
            state["stats"]["stopped_reason"] = None
            state["task"] = asyncio.create_task(autopilot_loop(user_id))
            _watch_loop(user_id, state["task"])

    add_log(user_id, "Autopilot STARTED")
    return {"success": True, "message": "Autopilot started"}


@router.post("/stop")
async def stop_autopilot(current_user: dict = Depends(require_trader)):
    await _stop_autopilot_internal(current_user["id"])
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
                "daily_loss_limit_enabled": settings_obj.daily_loss_limit_enabled,
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
        settings_obj.daily_loss_limit_enabled = config.daily_loss_limit_enabled
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
        except IndexError:  # not an "N. text" line
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
                mt5_order_ticket=t.mt5_order_ticket,
                order_status=t.order_status,
                execution_status=t.execution_status,
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


@router.get("/cycles")
async def get_cycle_history(
    skip: int = 0, limit: int = 50, outcome: Optional[str] = None,
    symbol: Optional[str] = None, current_user: dict = Depends(get_current_user),
):
    """Return each scheduled cycle with its linked AI/execution/broker trail."""
    user_id = current_user["id"]
    limit, skip = max(1, min(limit, 200)), max(0, skip)
    async with AsyncSessionLocal() as db:
        query = select(AutopilotCycle).where(AutopilotCycle.user_id == user_id)
        if outcome:
            query = query.where(AutopilotCycle.outcome == outcome)
        if symbol:
            query = query.where(AutopilotCycle.symbol == symbol.upper())
        total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
        cycles = list((await db.execute(query.order_by(AutopilotCycle.started_at.desc()).offset(skip).limit(limit))).scalars().all())
        ids = [c.cycle_id for c in cycles]
        if not ids:
            return {"cycles": [], "total": total, "skip": skip, "limit": limit}
        async def linked(model, timestamp):
            return (await db.execute(select(model).where(model.user_id == user_id, model.cycle_id.in_(ids)).order_by(timestamp))).scalars().all()
        calls = await linked(AiCallLog, AiCallLog.created_at)
        attempts = await linked(AutopilotExecutionAttempt, AutopilotExecutionAttempt.created_at)
        events = await linked(AutopilotOrderEvent, AutopilotOrderEvent.observed_at)
        logs = await linked(AutopilotLog, AutopilotLog.timestamp)
        trades = await linked(AutopilotTrade, AutopilotTrade.executed_at)
        def group(rows):
            result = {}
            for row in rows:
                result.setdefault(row.cycle_id, []).append(row)
            return result
        calls, attempts, events, logs, trades = map(group, (calls, attempts, events, logs, trades))
        def iso(value): return value.isoformat() if value else None
        output = []
        for c in cycles:
            timeline = [{"timestamp": iso(c.started_at), "stage": "cycle_started", "outcome": None,
                         "details": {"cycle_number": c.cycle_number, "symbol": c.symbol}}]
            for row in calls.get(c.cycle_id, []):
                timeline.append({"timestamp": iso(row.created_at), "stage": f"ai:{row.stage or 'call'}", "outcome": row.outcome,
                    "details": {"provider": row.provider, "model": row.model, "latency_ms": row.latency_ms, "error": row.error_message}})
            for row in attempts.get(c.cycle_id, []):
                timeline.append({"timestamp": iso(row.created_at), "stage": "execution", "outcome": row.outcome,
                    "details": {"category": row.error_category, "message": row.error_message, "ticket": row.mt5_ticket, "direction": row.direction, "volume": row.lot_size}})
            for row in events.get(c.cycle_id, []):
                timeline.append({"timestamp": iso(row.broker_time or row.observed_at), "stage": f"broker:{row.event_type}", "outcome": row.status,
                    "details": {"reason": row.reason, "reason_code": row.reason_code, "order_ticket": row.order_ticket,
                        "deal_ticket": row.deal_ticket, "position_id": row.position_id, "price": row.price, "volume": row.volume,
                        "profit": row.profit, "swap": row.swap, "commission": row.commission}})
            for row in logs.get(c.cycle_id, []):
                timeline.append({"timestamp": iso(row.timestamp), "stage": f"log:{row.level}", "outcome": None, "details": {"message": row.message}})
            for row in trades.get(c.cycle_id, []):
                timeline.append({"timestamp": iso(row.closed_at or row.order_completed_at or row.executed_at), "stage": "trade_status",
                    "outcome": row.result or row.execution_status, "details": {"trade_id": row.id, "profit": row.profit,
                    "closed_at": iso(row.closed_at), "exit_reason": row.exit_reason, "ticket": row.mt5_ticket}})
            timeline.append({"timestamp": iso(c.completed_at), "stage": "cycle_finished", "outcome": c.outcome,
                             "details": {"reason": c.outcome_reason}})
            timeline.sort(key=lambda item: item["timestamp"] or "")
            output.append({"cycle_id": c.cycle_id, "cycle_number": c.cycle_number, "symbol": c.symbol,
                "status": c.status, "outcome": c.outcome, "outcome_reason": c.outcome_reason,
                "started_at": iso(c.started_at), "completed_at": iso(c.completed_at), "prompt_number": c.prompt_number,
                "prompt_text": c.prompt_text, "prompt_version": c.prompt_version, "provider": c.provider, "model": c.model,
                "market_regime": c.market_regime, "market_timeframe": c.market_timeframe, "candles_loaded": c.candles_loaded,
                "market_data_hash": c.market_data_hash, "analysis_prompt_hash": c.analysis_prompt_hash,
                "decision_source": c.decision_source, "selection_context": c.selection_context, "regime_details": c.regime_details,
                "setup": c.setup, "requested_lot_size": c.requested_lot_size, "final_lot_size": c.final_lot_size,
                "execution_status": c.execution_status, "order_status": c.order_status, "mt5_order_ticket": c.mt5_order_ticket,
                "mt5_ticket": c.mt5_ticket, "trade_result": c.trade_result, "realized_profit": c.realized_profit,
                "exit_reason": c.exit_reason, "exit_reason_source": c.exit_reason_source,
                "trade_closed_at": iso(c.trade_closed_at), "duration_minutes": c.duration_minutes, "timeline": timeline})
        return {"cycles": output, "total": total, "skip": skip, "limit": limit}


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
