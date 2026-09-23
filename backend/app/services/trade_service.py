"""
Manual trading from the Terminal page. Every broker action goes through the
connector client; this module records what happened.

The connector validates volume, rounds prices, and moves stops that sit too
close to the market, so none of that is repeated here.
"""
import logging
import traceback
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from ..core.database import AsyncSessionLocal
from ..core.mt5_connector import ConnectorError, connector_client
from ..models.ai_memory import PositionAudit, TradeRecord
from ..models.schemas import OrderRequest

logger = logging.getLogger(__name__)

PENDING_ACTIONS = frozenset({"BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"})
VALID_ACTIONS = frozenset({"BUY", "SELL"}) | PENDING_ACTIONS


class TradeError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def _as_trade_error(exc: ConnectorError, what: str) -> TradeError:
    # Unreachable connector or server-side trouble becomes 502; the connector's
    # own refusals (bad symbol, not demo, invalid volume) keep their status.
    status = exc.status_code if exc.status_code < 500 else 502
    return TradeError(f"{what} failed: {exc.detail}", status)


async def place_order(order: OrderRequest, user_id: Optional[int]) -> dict:
    if order.action not in VALID_ACTIONS:
        raise TradeError(f"Invalid action: {order.action}")
    is_pending = order.action in PENDING_ACTIONS
    if is_pending and order.price is None:
        raise TradeError("Price required for pending orders")

    payload = {"symbol": order.symbol, "action": order.action, "volume": order.volume,
               "comment": order.comment, "magic": order.magic}
    for key in ("price", "sl", "tp"):
        if getattr(order, key) is not None:
            payload[key] = getattr(order, key)

    try:
        result = await connector_client.place_order(payload)
    except ConnectorError as exc:
        raise _as_trade_error(exc, "Order")

    try:
        async with AsyncSessionLocal() as db:
            db.add(TradeRecord(
                user_id=user_id or 0,
                symbol=order.symbol,
                direction="BUY" if "BUY" in order.action else "SELL",
                entry_price=result.get("price"),
                stop_loss=result.get("sl"),
                take_profit=result.get("tp"),
                volume=result.get("volume"),
                order_type="pending" if is_pending else "market",
                status="open",
                mt5_ticket=result.get("ticket"),
                executed_at=datetime.now(timezone.utc),
                comment=order.comment,
                ai_message=str(order.chat_memory_id) if order.chat_memory_id else None,
            ))
            await db.commit()
    except Exception:
        logger.warning(f"Failed to save trade record for ticket {result.get('ticket')}:\n{traceback.format_exc()}")

    return {
        "success": True,
        "ticket": result.get("ticket"),
        "symbol": order.symbol,
        "action": order.action,
        "volume": result.get("volume"),
        "price": result.get("price"),
        "sl": result.get("sl"),
        "tp": result.get("tp"),
        "comment": result.get("comment"),
    }


async def _open_position(ticket: int) -> Optional[dict]:
    try:
        positions = (await connector_client.get_positions()).get("positions", [])
    except ConnectorError:
        return None
    return next((p for p in positions if p.get("ticket") == ticket), None)


async def _closing_deal(ticket: int) -> Optional[dict]:
    try:
        deals = (await connector_client.get_history(hours=1)).get("deals", [])
    except ConnectorError:
        return None
    return next((d for d in deals if d.get("position_id") == ticket and d.get("entry") == "CLOSE"), None)


async def close_position(ticket: int, close_volume: Optional[float], user_id: Optional[int]) -> dict:
    before = await _open_position(ticket)
    try:
        result = await connector_client.close_position(ticket, close_volume)
    except ConnectorError as exc:
        raise _as_trade_error(exc, "Close")

    deal = await _closing_deal(ticket)
    close_price = deal.get("price") if deal else result.get("close_price")
    try:
        async with AsyncSessionLocal() as db:
            rec = (await db.execute(select(TradeRecord).where(TradeRecord.mt5_ticket == ticket))).scalar_one_or_none()
            if rec:
                rec.status = "closed"
                rec.closed_at = datetime.now(timezone.utc)
                rec.exit_price = close_price
                rec.profit_loss = deal.get("profit") if deal else None
            db.add(PositionAudit(
                user_id=user_id, mt5_ticket=ticket, action="close",
                symbol=(before or {}).get("symbol") or (rec.symbol if rec else ""),
                original_sl=(before or {}).get("sl"), original_tp=(before or {}).get("tp"),
                close_volume=result.get("closed_volume"), close_price=close_price,
            ))
            await db.commit()
    except Exception:
        logger.warning(f"Close audit failed for ticket {ticket}:\n{traceback.format_exc()}")

    return {
        "success": True,
        "ticket": ticket,
        "closed_volume": result.get("closed_volume"),
        "close_price": close_price,
        "comment": result.get("comment"),
    }


async def modify_position(ticket: int, new_sl: Optional[float], new_tp: Optional[float], user_id: Optional[int]) -> dict:
    before = await _open_position(ticket)
    try:
        result = await connector_client.modify_position(ticket, new_sl, new_tp)
    except ConnectorError as exc:
        raise _as_trade_error(exc, "Modify")

    try:
        async with AsyncSessionLocal() as db:
            db.add(PositionAudit(
                user_id=user_id, mt5_ticket=ticket, action="modify",
                symbol=(before or {}).get("symbol", ""),
                original_sl=(before or {}).get("sl"), original_tp=(before or {}).get("tp"),
                new_sl=result.get("sl"), new_tp=result.get("tp"),
            ))
            await db.commit()
    except Exception:
        logger.warning(f"PositionAudit modify failed for ticket {ticket}:\n{traceback.format_exc()}")

    return {"success": True, "ticket": ticket, "sl": result.get("sl"), "tp": result.get("tp"),
            "comment": result.get("comment")}
