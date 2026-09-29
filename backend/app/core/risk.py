"""
The risk gate. Every new order goes through submit_order.

It reads the limits in force, the account, the symbol and the open positions,
decides, records the decision, and only then sends the order to the broker.
Closing a position never comes through here: getting out is always allowed.
Changing a stop comes through check_modify, which refuses only removing it.

evaluate() holds every rule and touches nothing outside its arguments, so each
rule can be tested with plain numbers. submit_order() gathers those numbers.

The limits are rows in risk_settings, changed through /api/risk/settings. Each
change adds a row; the newest is in force. The connector has its own lot cap,
MT5_MAX_VOLUME, set on its machine, which nothing here can raise.
"""
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .database import AsyncSessionLocal
from .mt5_connector import ConnectorError, connector_client
from ..models.risk import RiskDay, RiskDecision, RiskSettings

log = logging.getLogger("risk")

DEFAULTS: Dict[str, Any] = {
    "autopilot_risk_pct": 1.0,
    "max_trade_risk_pct": 2.0,
    "max_open_positions": 0,
    "daily_loss_pct": 3.0,
    "min_margin_level": 200.0,
    "max_pending_distance_pct": 2.0,
    "require_stop_loss": True,
}

# Accepted range for each number when saving. Wide on purpose: these are for research.
RANGES = {
    "autopilot_risk_pct": (0.01, 10.0),
    "max_trade_risk_pct": (0.01, 10.0),
    "max_open_positions": (0, 1000),
    "daily_loss_pct": (0.0, 100.0),
    "min_margin_level": (0.0, 10000.0),
    "max_pending_distance_pct": (0.0, 100.0),
}

MARKET_ACTIONS = frozenset({"BUY", "SELL"})
PENDING_ACTIONS = frozenset({"BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"})


class RiskRefused(Exception):
    """A risk rule stopped the order. `code` names the rule."""

    def __init__(self, code: str, message: str, decision_id: Optional[int] = None):
        self.code = code
        self.message = message
        self.decision_id = decision_id
        super().__init__(f"{code}: {message}")


@dataclass
class Evaluation:
    allowed: bool
    code: Optional[str] = None
    message: Optional[str] = None
    volume: Optional[float] = None
    price: Optional[float] = None
    stop_distance: Optional[float] = None
    risk_amount: Optional[float] = None
    risk_pct: Optional[float] = None
    equity: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)


# ── Settings ───────────────────────────────────────────────────────────────
def settings_dict(row: RiskSettings) -> Dict[str, Any]:
    out = {key: getattr(row, key) for key in DEFAULTS}
    out.update(id=row.id, created_at=row.created_at.isoformat() if row.created_at else None,
               changed_by_name=row.changed_by_name, reason=row.reason)
    return out


def validate_settings(values: Dict[str, Any]) -> List[str]:
    errors = []
    for key, (low, high) in RANGES.items():
        value = values.get(key)
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value):
            errors.append(f"{key} must be a number")
        elif not low <= value <= high:
            errors.append(f"{key} must be between {low} and {high}")
    if not isinstance(values.get("require_stop_loss"), bool):
        errors.append("require_stop_loss must be true or false")
    if not errors and values["autopilot_risk_pct"] > values["max_trade_risk_pct"]:
        errors.append("autopilot_risk_pct cannot be above max_trade_risk_pct, or every autopilot order would be refused")
    return errors


async def current_settings(db) -> RiskSettings:
    row = (await db.execute(select(RiskSettings).order_by(RiskSettings.id.desc()).limit(1))).scalar_one_or_none()
    if row is None:
        row = RiskSettings(reason="Starting values", **DEFAULTS)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


async def save_settings(changes: Dict[str, Any], user: Dict[str, Any], reason: str) -> RiskSettings:
    """Add a new version: the current values with `changes` applied. Raises ValueError listing problems."""
    async with AsyncSessionLocal() as db:
        current = await current_settings(db)
        values = {key: getattr(current, key) for key in DEFAULTS}
        unknown = set(changes) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
        values.update(changes)
        errors = validate_settings(values)
        if not reason or not reason.strip():
            errors.append("a reason is required, so the history says why the limits changed")
        if errors:
            raise ValueError("; ".join(errors))
        row = RiskSettings(changed_by=user.get("id"), changed_by_name=user.get("username"),
                           reason=reason.strip(), **values)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


# ── Rules ──────────────────────────────────────────────────────────────────
def value_per_price_unit(info: Dict[str, Any]) -> Optional[float]:
    """Account currency gained or lost per lot when the price moves by 1.0."""
    tick_value, tick_size = info.get("trade_tick_value"), info.get("trade_tick_size")
    if not tick_value or not tick_size or tick_value <= 0 or tick_size <= 0:
        return None
    return tick_value / tick_size


def _step_decimals(step: float) -> int:
    return max(0, -int(math.floor(math.log10(step)))) if step > 0 else 2


def _floor_to_step(volume: float, step: float) -> float:
    if step <= 0:
        return volume
    return round(math.floor(volume / step + 1e-9) * step, _step_decimals(step))


def evaluate(order: Dict[str, Any], info: Dict[str, Any], account: Dict[str, Any],
             positions: List[Dict[str, Any]], settings: Dict[str, Any], start_equity: Optional[float],
             size_from_risk: bool) -> Evaluation:
    """Apply every rule to one order. Pure: no I/O, no clock."""
    action = order.get("action")
    if action not in MARKET_ACTIONS | PENDING_ACTIONS:
        return Evaluation(False, "invalid_action", f"Unknown action {action}")
    buy = action.startswith("BUY")
    digits = info.get("digits") or 5

    # The price the order is judged at: the market for market orders, its own price for pending ones.
    market = info.get("ask") if buy else info.get("bid")
    if not market:
        return Evaluation(False, "market_data_unavailable", f"No price for {order.get('symbol')}")
    if action in PENDING_ACTIONS:
        price = order.get("price")
        if not price:
            return Evaluation(False, "invalid_order", "A pending order needs a price")
    else:
        price = market

    equity = account.get("equity") or 0.0
    ev = Evaluation(False, price=price, equity=equity)
    if equity <= 0:
        ev.code, ev.message = "market_data_unavailable", "Account equity unavailable"
        return ev

    # Account-wide limits.
    limit = settings["daily_loss_pct"]
    if limit and start_equity and start_equity > 0:
        loss_pct = (start_equity - equity) / start_equity * 100
        ev.extra["daily_loss_pct"] = round(loss_pct, 4)
        if loss_pct >= limit:
            ev.code = "daily_loss_limit"
            ev.message = (f"Down {loss_pct:.2f}% today, from {start_equity:.2f} to {equity:.2f}. "
                          f"The daily limit is {limit}%. New orders resume tomorrow (UTC).")
            return ev
    limit = settings["min_margin_level"]
    margin, margin_level = account.get("margin") or 0, account.get("margin_level") or 0
    if limit and margin > 0 and margin_level < limit:
        ev.code = "margin_level_low"
        ev.message = f"Margin level {margin_level:.0f}% is below the {limit:.0f}% minimum"
        return ev
    limit = settings["max_open_positions"]
    if limit and len(positions) >= limit:
        ev.code = "max_open_positions"
        ev.message = f"{len(positions)} positions open, the limit is {limit}"
        return ev

    # Pending price.
    limit = settings["max_pending_distance_pct"]
    if action in PENDING_ACTIONS and limit:
        away = abs(price - market) / market * 100
        if away > limit:
            ev.code = "pending_too_far"
            ev.message = f"Pending price {price} is {away:.2f}% from the market {market}; the limit is {limit}%"
            return ev

    # Stop and target placement.
    sl, tp = order.get("sl"), order.get("tp")
    if not sl:
        if size_from_risk:
            ev.code, ev.message = "no_stop_loss", "The autopilot sizes each order from its stop loss, and this one has none"
            return ev
        if settings["require_stop_loss"]:
            ev.code, ev.message = "no_stop_loss", "Every order needs a stop loss"
            return ev
    min_dist = info.get("min_stop_distance") or 0.0
    gap = round(min_dist, digits)
    if sl:
        if (buy and sl > price - min_dist) or (not buy and sl < price + min_dist):
            ev.code = "bad_stop_loss"
            ev.message = f"Stop loss {sl} must be at least {gap} {'below' if buy else 'above'} the price {price}"
            return ev
    if tp:
        if (buy and tp < price + min_dist) or (not buy and tp > price - min_dist):
            ev.code = "bad_take_profit"
            ev.message = f"Take profit {tp} must be at least {gap} {'above' if buy else 'below'} the price {price}"
            return ev

    # Size and risk.
    requested = order.get("volume")
    if not sl:
        # No stop, allowed by the settings: the loss has no bound, so it cannot be measured.
        ev.allowed, ev.volume = True, requested
        return ev
    per_unit = value_per_price_unit(info)
    if per_unit is None:
        ev.code, ev.message = "market_data_unavailable", f"No tick value for {order.get('symbol')}, cannot measure risk"
        return ev
    ev.stop_distance = round(abs(price - sl), digits)
    step = info.get("volume_step") or 0.01
    if size_from_risk:
        budget = equity * settings["autopilot_risk_pct"] / 100
        volume = _floor_to_step(budget / (ev.stop_distance * per_unit), step)
        caps = [c for c in (info.get("volume_max"), info.get("max_volume")) if c]
        if caps and volume > min(caps):
            volume = _floor_to_step(min(caps), step)
        if volume < (info.get("volume_min") or step):
            ev.code = "risk_budget_too_small"
            ev.message = (f"{settings['autopilot_risk_pct']}% of {equity:.2f} is {budget:.2f}, less than the smallest "
                          f"lot {info.get('volume_min')} risks with a stop {ev.stop_distance} away")
            return ev
    else:
        volume = requested
    if not volume or volume <= 0:
        ev.code, ev.message = "invalid_order", "Volume must be above zero"
        return ev
    ev.volume = volume
    ev.risk_amount = round(ev.stop_distance * per_unit * volume, 2)
    ev.risk_pct = round(ev.risk_amount / equity * 100, 4)
    limit = settings["max_trade_risk_pct"]
    if ev.risk_pct > limit + 1e-9:
        ev.code = "trade_risk_too_high"
        ev.message = (f"{volume} lots with a stop {ev.stop_distance} away risks {ev.risk_amount:.2f}, "
                      f"{ev.risk_pct:.4g}% of equity; the limit is {limit}%")
        return ev
    ev.allowed = True
    return ev


# ── Gathering, recording, sending ──────────────────────────────────────────
def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def start_of_day_equity(db, account: Dict[str, Any]) -> Optional[float]:
    """The equity at the first check today (UTC), recorded now if this is the first."""
    login, equity = account.get("login"), account.get("equity")
    if login is None or not equity:
        return None
    day = _today()
    query = select(RiskDay).where(RiskDay.day == day, RiskDay.account_login == login)
    row = (await db.execute(query)).scalar_one_or_none()
    if row is None:
        db.add(RiskDay(day=day, account_login=login, start_equity=equity))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()  # another request recorded it first
        row = (await db.execute(query)).scalar_one()
    return row.start_equity


async def _record(**fields) -> Optional[int]:
    try:
        async with AsyncSessionLocal() as db:
            row = RiskDecision(**fields)
            db.add(row)
            await db.commit()
            return row.id
    except Exception:
        log.exception("Could not record a risk decision: %s", fields.get("reason_code") or fields.get("outcome"))
        return None


def _decision_fields(order, ev: Evaluation, source, user_id, settings_id, context) -> Dict[str, Any]:
    return dict(user_id=user_id, source=source, action=order.get("action") or "", symbol=order.get("symbol") or "",
                requested_volume=order.get("volume"), volume=ev.volume, price=ev.price,
                sl=order.get("sl"), tp=order.get("tp"), stop_distance=ev.stop_distance,
                risk_amount=ev.risk_amount, risk_pct=ev.risk_pct, equity=ev.equity,
                settings_id=settings_id, context={**(context or {}), **ev.extra} or None)


async def submit_order(order: Dict[str, Any], *, source: str, user_id: Optional[int],
                       context: Optional[Dict[str, Any]] = None, size_from_risk: bool = False) -> Dict[str, Any]:
    """Check one new order and send it if it passes. The only caller of connector_client.place_order.

    Raises RiskRefused when a rule stops it, and ConnectorError when the connector
    is unreachable or the broker refuses it. Returns the connector's reply plus `risk`.
    """
    async with AsyncSessionLocal() as db:
        settings_row = await current_settings(db)
        settings = settings_dict(settings_row)
        try:
            account = await connector_client.get_account()
            info = await connector_client.get_symbol(order["symbol"])
            positions = (await connector_client.get_positions()).get("positions", [])
        except ConnectorError as exc:
            # Not a risk rule: the connector could not answer. Record it and pass it on.
            fields = _decision_fields(order, Evaluation(False), source, user_id, settings_row.id, context)
            await _record(outcome="failed", reason_code="market_data_unavailable", message=exc.detail, **fields)
            raise
        start_equity = await start_of_day_equity(db, account)

    ev = evaluate(order, info, account, positions, settings, start_equity, size_from_risk)
    fields = _decision_fields(order, ev, source, user_id, settings_row.id, context)
    if not ev.allowed:
        decision_id = await _record(outcome="refused", reason_code=ev.code, message=ev.message, **fields)
        log.info("Refused %s %s from %s: %s", order.get("action"), order.get("symbol"), source, ev.message)
        raise RiskRefused(ev.code, ev.message, decision_id)

    payload = {k: v for k, v in order.items() if v is not None}
    payload["volume"] = ev.volume
    try:
        result = await connector_client.place_order(payload)
    except ConnectorError as exc:
        await _record(outcome="failed", reason_code="broker_refused", message=exc.detail, **fields)
        raise
    decision_id = await _record(outcome="sent", mt5_ticket=result.get("ticket"), **fields)
    result["risk"] = {"decision_id": decision_id, "volume": ev.volume, "risk_amount": ev.risk_amount,
                      "risk_pct": ev.risk_pct, "settings_id": settings_row.id}
    return result


async def check_modify(ticket: int, symbol: str, sl: Optional[float], *, source: str,
                       user_id: Optional[int]) -> None:
    """Refuse removing a stop loss while stops are required. Moving one is always allowed."""
    if sl is None or sl > 0:
        return  # unchanged, or moved
    async with AsyncSessionLocal() as db:
        settings_row = await current_settings(db)
    if not settings_row.require_stop_loss:
        return
    message = "Removing the stop loss is not allowed while every position needs one"
    decision_id = await _record(user_id=user_id, source=source, action="MODIFY", symbol=symbol or "",
                                outcome="refused", reason_code="removes_stop_loss", message=message,
                                sl=sl, settings_id=settings_row.id, mt5_ticket=ticket)
    raise RiskRefused("removes_stop_loss", message, decision_id)
