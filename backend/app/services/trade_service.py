import asyncio
import logging
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import select

from ..core.config import settings
from ..core.database import AsyncSessionLocal
from ..core.mt5_connector import connector_client
from ..core.trade_reconcile import fetch_position_close
from ..core.utils import apply_default_sl_tp
from ..models.ai_memory import TradeRecord, PositionAudit, ChatMemory
from ..models.schemas import OrderRequest, CloseRequest, ModifyRequest

logger = logging.getLogger(__name__)


def _get_mt5():
    try:
        import MetaTrader5 as mt5
        return mt5
    except ImportError:
        return None


def _use_connector() -> bool:
    """Route orders to the external MT5 connector when appropriate.

    The explicit flag wins; otherwise auto-detect so hosts without the
    Windows MetaTrader5 package (the Linux production server) use the
    connector whenever a URL is configured.
    """
    if settings.MT5_USE_EXTERNAL_CONNECTOR and settings.MT5_CONNECTOR_URL:
        return True
    return bool(settings.MT5_CONNECTOR_URL) and _get_mt5() is None


def _connector_error(action: str, exc: Exception) -> "TradeError":
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        try:
            detail = exc.response.json().get("detail") or exc.response.text[:300]
        except Exception:
            detail = exc.response.text[:300]
        return TradeError(
            f"{action} failed: {detail}",
            status if 400 <= status < 500 else 502,
        )
    return TradeError(f"{action} failed: MT5 connector unreachable ({exc})", 502)


def _get_safe_attr(attr, fallback):
    mt5 = _get_mt5()
    return getattr(mt5, attr) if hasattr(mt5, attr) else fallback


ORDER_FILLING_FOK = _get_safe_attr('ORDER_FILLING_FOK', 1)
ORDER_FILLING_IOC = _get_safe_attr('ORDER_FILLING_IOC', 2)
ORDER_FILLING_RETURN = _get_safe_attr('ORDER_FILLING_RETURN', 3)
TRADE_ACTION_DEAL = _get_safe_attr('TRADE_ACTION_DEAL', 1)
TRADE_ACTION_PENDING = _get_safe_attr('TRADE_ACTION_PENDING', 5)
TRADE_ACTION_SLTP = _get_safe_attr('TRADE_ACTION_SLTP', 6)
ORDER_TYPE_BUY = _get_safe_attr('ORDER_TYPE_BUY', 0)
ORDER_TYPE_SELL = _get_safe_attr('ORDER_TYPE_SELL', 1)

ACTION_MAP = {
    "BUY": ORDER_TYPE_BUY,
    "SELL": ORDER_TYPE_SELL,
    "BUY_LIMIT": _get_safe_attr('ORDER_TYPE_BUY_LIMIT', 2),
    "SELL_LIMIT": _get_safe_attr('ORDER_TYPE_SELL_LIMIT', 3),
    "BUY_STOP": _get_safe_attr('ORDER_TYPE_BUY_STOP', 4),
    "SELL_STOP": _get_safe_attr('ORDER_TYPE_SELL_STOP', 5),
}

PENDING_ACTIONS = frozenset({"BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"})


class TradeError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


async def init_mt5() -> None:
    if _use_connector():
        return  # the remote Windows connector manages its own MT5 session
    mt5 = _get_mt5()
    if mt5 is None:
        raise TradeError("MT5 not installed on this server", 500)
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, mt5.initialize)
    if not ok:
        error = mt5.last_error() if hasattr(mt5, 'last_error') else "Unknown"
        raise TradeError(f"MT5 initialization failed: {error}", 500)


async def _select_symbol(symbol: str):
    mt5 = _get_mt5()
    loop = asyncio.get_running_loop()
    if not await loop.run_in_executor(None, mt5.symbol_select, symbol, True):
        raise TradeError(f"Symbol {symbol} not found", 404)

    symbol_info = await loop.run_in_executor(None, mt5.symbol_info, symbol)
    tick = await loop.run_in_executor(None, mt5.symbol_info_tick, symbol)

    if symbol_info is None or tick is None:
        raise TradeError(f"Broker data unavailable for {symbol}", 400)

    return symbol_info, tick


async def _resolve_chat_link(
    user_id: Optional[int], symbol: str, explicit_id: Optional[int]
) -> Optional[int]:
    """Attach the AI analysis behind this trade (backlog item A).

    The Execute-Trade button passes chat_memory_id explicitly; Terminal
    orders don't. Fall back to the user's latest assistant analysis of the
    same symbol (broker-suffix-insensitive) from the last 24h so every
    trade_record feeds the RAG profit/feedback scores.
    """
    if explicit_id:
        return explicit_id
    if not user_id or not symbol:
        return None
    base = symbol.split(".")[0].upper()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(ChatMemory.id, ChatMemory.symbol)
                .where(
                    ChatMemory.user_id == user_id,
                    ChatMemory.role == "assistant",
                    ChatMemory.created_at >= cutoff,
                )
                .order_by(ChatMemory.created_at.desc())
                .limit(100)
            )
            for cid, chat_symbol in result.all():
                if (chat_symbol or "").split(".")[0].upper() == base:
                    return cid
    except Exception:
        logger.warning(
            f"Chat link lookup failed for {symbol}:\n{traceback.format_exc()}"
        )
    return None


async def _save_trade_record(
    user_id: Optional[int],
    order: OrderRequest,
    *,
    price: float,
    volume: float,
    sl: Optional[float],
    tp: Optional[float],
    ticket: Optional[int],
    resolved_chat_id: Optional[int],
) -> None:
    try:
        async with AsyncSessionLocal() as db:
            db.add(TradeRecord(
                user_id=user_id or 0,
                symbol=order.symbol,
                direction="BUY" if "BUY" in order.action else "SELL",
                entry_price=price,
                stop_loss=sl,
                take_profit=tp,
                volume=volume,
                order_type="market" if order.action in ("BUY", "SELL") else "pending",
                status="open",
                mt5_ticket=ticket,
                executed_at=datetime.now(timezone.utc),
                comment=order.comment,
                ai_message=str(resolved_chat_id) if resolved_chat_id else None,
            ))
            await db.commit()
    except Exception:
        logger.warning(
            f"Failed to save trade record for ticket {ticket}:\n{traceback.format_exc()}"
        )


async def place_order(order: OrderRequest, user_id: Optional[int]) -> dict:
    if order.action not in ACTION_MAP:
        raise TradeError(f"Invalid action: {order.action}")
    if _use_connector():
        return await _place_order_connector(order, user_id)
    await init_mt5()
    mt5 = _get_mt5()
    loop = asyncio.get_running_loop()

    symbol_info, tick = await _select_symbol(order.symbol)

    if order.volume < symbol_info.volume_min:
        raise TradeError(f"Volume {order.volume} below minimum {symbol_info.volume_min}")

    volume = round(order.volume / symbol_info.volume_step) * symbol_info.volume_step
    volume = round(volume, 2)
    point = symbol_info.point
    digits = symbol_info.digits

    order_type = ACTION_MAP[order.action]
    is_pending = order.action in PENDING_ACTIONS

    if is_pending and order.price is None:
        raise TradeError("Price required for pending orders")

    if is_pending:
        price = order.price
    elif order.action == "BUY":
        price = tick.ask
    else:
        price = tick.bid

    if price is None:
        raise TradeError("Cannot get current price", 500)

    price = round(price, digits)

    # Default SL/TP (0.2% of price) when the request has none or zero.
    order.sl, order.tp = apply_default_sl_tp(order.action, price, order.sl, order.tp, digits)

    min_dist = max(symbol_info.trade_stops_level, 10) * point

    sl = None
    if order.sl is not None:
        sl = round(order.sl, digits)
        if "BUY" in order.action:
            if sl >= price - min_dist:
                sl = round(price - min_dist, digits)
        else:
            if sl <= price + min_dist:
                sl = round(price + min_dist, digits)

    tp = None
    if order.tp is not None:
        tp = round(order.tp, digits)
        if "BUY" in order.action:
            if tp <= price + min_dist:
                tp = round(price + min_dist, digits)
        else:
            if tp >= price - min_dist:
                tp = round(price - min_dist, digits)

    request = {
        "action": TRADE_ACTION_DEAL if not is_pending else TRADE_ACTION_PENDING,
        "symbol": order.symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "deviation": 20,
        "magic": order.magic,
        "comment": order.comment,
        "type_time": mt5.ORDER_TIME_GTC if hasattr(mt5, 'ORDER_TIME_GTC') else 0,
        "type_filling": ORDER_FILLING_IOC,
    }

    if sl is not None:
        request["sl"] = sl
    if tp is not None:
        request["tp"] = tp

    if not is_pending:
        filling_mode = symbol_info.filling_mode
        if filling_mode & 1:
            request["type_filling"] = ORDER_FILLING_FOK
        elif filling_mode & 2:
            request["type_filling"] = ORDER_FILLING_IOC
        else:
            request["type_filling"] = ORDER_FILLING_RETURN

    result = await loop.run_in_executor(None, mt5.order_send, request)

    if result is None or result.retcode != getattr(mt5, 'TRADE_RETCODE_DONE', 10009):
        detail = result.comment if result else "Unknown error"
        raise TradeError(f"Order failed: {detail}")

    chat_id = await _resolve_chat_link(user_id, order.symbol, order.chat_memory_id)
    await _save_trade_record(
        user_id, order,
        price=price, volume=volume, sl=sl, tp=tp,
        ticket=result.order, resolved_chat_id=chat_id,
    )

    return {
        "success": True,
        "ticket": result.order,
        "symbol": order.symbol,
        "action": order.action,
        "volume": volume,
        "price": price,
        "sl": sl,
        "tp": tp,
        "comment": result.comment,
    }


async def _place_order_connector(order: OrderRequest, user_id: Optional[int]) -> dict:
    """Place an order through the external MT5 connector (backlog item A).

    The connector (Windows box with the terminal) performs all broker
    validation — min volume, step rounding, stop distance — and returns the
    final adjusted price/sl/tp/volume, which we store in trade_records.
    """
    if order.action in PENDING_ACTIONS and order.price is None:
        raise TradeError("Price required for pending orders")

    symbol_info = {}
    try:
        symbol_info = await connector_client.get_symbol(order.symbol)
    except Exception as e:
        logger.debug(f"Symbol info unavailable for {order.symbol}: {e}")
    vol_min = symbol_info.get("volume_min")
    if vol_min and order.volume < vol_min:
        raise TradeError(f"Volume {order.volume} below minimum {vol_min}")

    # Default SL/TP (0.2% of price) when the request has none or zero.
    # Reference price: requested price for pendings, else current bid/ask.
    ref_price = order.price
    if ref_price is None and symbol_info:
        ref_price = symbol_info.get("ask") if "BUY" in order.action else symbol_info.get("bid")
    order.sl, order.tp = apply_default_sl_tp(
        order.action, ref_price, order.sl, order.tp,
        digits=symbol_info.get("digits") if symbol_info else None,
    )

    payload = {
        "symbol": order.symbol,
        "action": order.action,
        "volume": order.volume,
        "comment": order.comment,
        "magic": order.magic,
    }
    if order.price is not None:
        payload["price"] = order.price
    if order.sl is not None:
        payload["sl"] = order.sl
    if order.tp is not None:
        payload["tp"] = order.tp

    try:
        data = await connector_client.place_order(payload)
    except Exception as e:
        raise _connector_error("Order", e)
    if not data.get("success"):
        raise TradeError(f"Order failed: {data.get('error') or 'unknown error'}")

    ticket = data.get("ticket")
    volume = data.get("volume") or order.volume
    sl = data.get("sl", order.sl)
    tp = data.get("tp", order.tp)
    price = data.get("price") or order.price
    if price is None and symbol_info:
        price = symbol_info.get("ask") if order.action == "BUY" else symbol_info.get("bid")
    if price is None:
        raise TradeError("Order placed but no fill price returned", 502)

    chat_id = await _resolve_chat_link(user_id, order.symbol, order.chat_memory_id)
    await _save_trade_record(
        user_id, order,
        price=price, volume=volume, sl=sl, tp=tp,
        ticket=ticket, resolved_chat_id=chat_id,
    )

    return {
        "success": True,
        "ticket": ticket,
        "symbol": order.symbol,
        "action": order.action,
        "volume": volume,
        "price": price,
        "sl": sl,
        "tp": tp,
        "comment": data.get("comment") or order.comment,
    }


async def close_position(ticket: int, close_volume: Optional[float], user_id: Optional[int]) -> dict:
    if _use_connector():
        return await _close_position_connector(ticket, close_volume, user_id)
    await init_mt5()
    mt5 = _get_mt5()
    loop = asyncio.get_running_loop()

    positions = await loop.run_in_executor(None, lambda: mt5.positions_get(ticket=ticket))
    if positions is None or len(positions) == 0:
        raise TradeError(f"Position {ticket} not found", 404)

    position = positions[0]
    volume = close_volume if close_volume else position.volume

    if volume > position.volume:
        raise TradeError("Close volume exceeds position volume")

    if position.type == mt5.POSITION_TYPE_BUY:
        tick = await loop.run_in_executor(None, mt5.symbol_info_tick, position.symbol)
        price = tick.bid
        order_type = mt5.ORDER_TYPE_SELL
    else:
        tick = await loop.run_in_executor(None, mt5.symbol_info_tick, position.symbol)
        price = tick.ask
        order_type = mt5.ORDER_TYPE_BUY

    symbol_info = await loop.run_in_executor(None, mt5.symbol_info, position.symbol)
    filling_mode = symbol_info.filling_mode if symbol_info else 2

    if filling_mode & 1:
        type_filling = mt5.ORDER_FILLING_FOK
    elif filling_mode & 2:
        type_filling = mt5.ORDER_FILLING_IOC
    else:
        type_filling = mt5.ORDER_FILLING_RETURN

    request = {
        "action": TRADE_ACTION_DEAL,
        "symbol": position.symbol,
        "volume": volume,
        "type": order_type,
        "position": ticket,
        "price": price,
        "deviation": 20,
        "magic": 0,
        "comment": "[IMPULSE_V2]",
        "type_time": getattr(mt5, 'ORDER_TIME_GTC', 0),
        "type_filling": type_filling,
    }

    result = await loop.run_in_executor(None, mt5.order_send, request)
    if result is None or result.retcode != getattr(mt5, 'TRADE_RETCODE_DONE', 10009):
        detail = result.comment if result else "Unknown error"
        raise TradeError(f"Close failed: {detail}")

    close_profit = None
    close_deal_price = None
    try:
        hist_from = datetime.now() - timedelta(seconds=10)
        hist_to = datetime.now() + timedelta(seconds=1)
        deals = await loop.run_in_executor(
            None, lambda: mt5.history_deals_get(hist_from, hist_to)
        )
        if deals:
            for d in deals:
                if d.position_id == ticket:
                    close_profit = d.profit
                    close_deal_price = d.price
                    break
    except Exception:
        pass

    try:
        async with AsyncSessionLocal() as db:
            rec = await db.execute(
                select(TradeRecord).where(TradeRecord.mt5_ticket == ticket)
            )
            trade_rec = rec.scalar_one_or_none()
            if trade_rec:
                trade_rec.status = "closed"
                trade_rec.closed_at = datetime.now(timezone.utc)
                trade_rec.exit_price = close_deal_price if close_deal_price else price
                trade_rec.profit_loss = close_profit

            db.add(PositionAudit(
                user_id=user_id,
                mt5_ticket=ticket,
                action="close",
                symbol=position.symbol,
                original_sl=position.sl,
                original_tp=position.tp,
                close_volume=volume,
                close_price=price,
            ))
            await db.commit()
    except Exception:
        logger.warning(f"Close audit failed for ticket {ticket}:\n{traceback.format_exc()}")

    return {
        "success": True,
        "ticket": ticket,
        "closed_volume": volume,
        "close_price": price,
        "comment": result.comment,
    }


async def _connector_position(ticket: int, action: str) -> dict:
    try:
        res = await connector_client.get_positions()
    except Exception as e:
        raise _connector_error(action, e)
    for p in res.get("positions") or []:
        if p.get("ticket") == ticket:
            return p
    raise TradeError(f"Position {ticket} not found", 404)


async def _close_position_connector(
    ticket: int, close_volume: Optional[float], user_id: Optional[int]
) -> dict:
    position = await _connector_position(ticket, "Close")
    volume = float(close_volume) if close_volume else float(position.get("volume") or 0)
    if volume > float(position.get("volume") or volume):
        raise TradeError("Close volume exceeds position volume")

    try:
        data = await connector_client.close_position(ticket, volume)
    except Exception as e:
        raise _connector_error("Close", e)
    if data.get("success") is False:
        raise TradeError(f"Close failed: {data.get('error') or 'unknown error'}")

    # The connector returns close_price but not profit — pull it from history.
    close_price = data.get("close_price")
    close_profit = None
    try:
        deal = await fetch_position_close(ticket)
        if deal:
            if deal.get("price") is not None:
                close_price = deal["price"]
            close_profit = deal.get("profit")
    except Exception:
        pass

    try:
        async with AsyncSessionLocal() as db:
            rec = await db.execute(
                select(TradeRecord).where(TradeRecord.mt5_ticket == ticket)
            )
            trade_rec = rec.scalar_one_or_none()
            if trade_rec:
                trade_rec.status = "closed"
                trade_rec.closed_at = datetime.now(timezone.utc)
                if close_price is not None:
                    trade_rec.exit_price = close_price
                if close_profit is not None:
                    trade_rec.profit_loss = close_profit

            db.add(PositionAudit(
                user_id=user_id,
                mt5_ticket=ticket,
                action="close",
                symbol=position.get("symbol", ""),
                original_sl=position.get("sl"),
                original_tp=position.get("tp"),
                close_volume=volume,
                close_price=close_price,
            ))
            await db.commit()
    except Exception:
        logger.warning(f"Close audit failed for ticket {ticket}:\n{traceback.format_exc()}")

    return {
        "success": True,
        "ticket": ticket,
        "closed_volume": volume,
        "close_price": close_price,
        "comment": data.get("comment", ""),
    }


async def modify_position(ticket: int, new_sl: Optional[float], new_tp: Optional[float], user_id: Optional[int]) -> dict:
    if _use_connector():
        return await _modify_position_connector(ticket, new_sl, new_tp, user_id)
    await init_mt5()
    mt5 = _get_mt5()
    loop = asyncio.get_running_loop()

    positions = await loop.run_in_executor(None, lambda: mt5.positions_get(ticket=ticket))
    if positions is None or len(positions) == 0:
        raise TradeError(f"Position {ticket} not found", 404)

    position = positions[0]
    symbol_info = await loop.run_in_executor(None, mt5.symbol_info, position.symbol)
    if symbol_info is None:
        raise TradeError("Cannot get symbol info")

    digits = symbol_info.digits
    resolved_sl = round(new_sl, digits) if new_sl is not None else position.sl
    resolved_tp = round(new_tp, digits) if new_tp is not None else position.tp

    request = {
        "action": TRADE_ACTION_SLTP,
        "symbol": position.symbol,
        "position": ticket,
        "sl": resolved_sl,
        "tp": resolved_tp,
    }

    result = await loop.run_in_executor(None, mt5.order_send, request)
    if result is None or result.retcode != getattr(mt5, 'TRADE_RETCODE_DONE', 10009):
        detail = result.comment if result else "Unknown error"
        raise TradeError(f"Modify failed: {detail}")

    try:
        async with AsyncSessionLocal() as db:
            db.add(PositionAudit(
                user_id=user_id,
                mt5_ticket=ticket,
                action="modify",
                symbol=position.symbol,
                original_sl=position.sl,
                original_tp=position.tp,
                new_sl=resolved_sl,
                new_tp=resolved_tp,
            ))
            await db.commit()
    except Exception:
        logger.warning(f"PositionAudit modify failed for ticket {ticket}:\n{traceback.format_exc()}")

    return {
        "success": True,
        "ticket": ticket,
        "sl": resolved_sl,
        "tp": resolved_tp,
        "comment": result.comment,
    }


async def _modify_position_connector(
    ticket: int, new_sl: Optional[float], new_tp: Optional[float], user_id: Optional[int]
) -> dict:
    position = await _connector_position(ticket, "Modify")
    try:
        data = await connector_client.modify_position(ticket, new_sl, new_tp)
    except Exception as e:
        raise _connector_error("Modify", e)
    if data.get("success") is False:
        raise TradeError(f"Modify failed: {data.get('error') or 'unknown error'}")

    resolved_sl = data.get("sl", new_sl)
    resolved_tp = data.get("tp", new_tp)

    try:
        async with AsyncSessionLocal() as db:
            db.add(PositionAudit(
                user_id=user_id,
                mt5_ticket=ticket,
                action="modify",
                symbol=position.get("symbol", ""),
                original_sl=position.get("sl"),
                original_tp=position.get("tp"),
                new_sl=resolved_sl,
                new_tp=resolved_tp,
            ))
            await db.commit()
    except Exception:
        logger.warning(f"PositionAudit modify failed for ticket {ticket}:\n{traceback.format_exc()}")

    return {
        "success": True,
        "ticket": ticket,
        "sl": resolved_sl,
        "tp": resolved_tp,
        "comment": data.get("comment", ""),
    }
