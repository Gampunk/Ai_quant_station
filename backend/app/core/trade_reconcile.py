"""Reconcile trade_records with MT5 history deals.

Trades closed outside the app (SL/TP hits, manual closes in the MT5
terminal, partial closes) never reach trade_service.close_position, so
their profit_loss stayed NULL forever and the RAG score's 0.3 profit
weight never fired. This job matches open trade_records against MT5
history deals by position ticket and writes the outcome back.
"""
import asyncio
import logging
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from ..core.config import settings
from ..core.database import AsyncSessionLocal
from ..models.ai_memory import TradeRecord

logger = logging.getLogger(__name__)

LOOKBACK_CAP_HOURS = 720  # never ask MT5 for more than 30 days of history


def _use_connector() -> bool:
    """Mirror trade_service: explicit flag wins, otherwise auto-detect."""
    if settings.MT5_USE_EXTERNAL_CONNECTOR and settings.MT5_CONNECTOR_URL:
        return True
    if settings.MT5_CONNECTOR_URL:
        try:
            import MetaTrader5  # noqa: F401
            return False
        except ImportError:
            return True
    return False


async def fetch_deals(hours: int) -> Optional[list[dict]]:
    """Fetch MT5 history deals as plain dicts; None when MT5 unreachable."""
    if _use_connector():
        from ..core.mt5_connector import connector_client
        try:
            res = await connector_client.get_history(hours=hours)
            if res.get("success"):
                return res.get("deals") or []
            logger.warning("[Reconcile] connector history failed: %s", res.get("error"))
            return None
        except Exception as e:
            logger.warning("[Reconcile] connector history error: %s", e)
            return None

    try:
        import MetaTrader5 as mt5
    except ImportError:
        return None

    loop = asyncio.get_running_loop()
    from_time = datetime.now() - timedelta(hours=hours)
    to_time = datetime.now() + timedelta(days=5)
    deals = await loop.run_in_executor(
        None, lambda: mt5.history_deals_get(from_time, to_time)
    )
    if deals is None:
        return []

    out = []
    for d in deals:
        if d.entry not in (0, 1):
            continue
        out.append({
            "ticket": d.order,
            "position_id": d.position_id,
            "entry": "OPEN" if d.entry == 0 else "CLOSE",
            "symbol": d.symbol,
            "direction": "BUY" if d.type == mt5.DEAL_TYPE_BUY else "SELL",
            "volume": d.volume,
            "price": d.price,
            "profit": d.profit,
            "swap": d.swap,
            "commission": d.commission,
            "comment": d.comment or "",
            "time": datetime.utcfromtimestamp(d.time).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return out


def _parse_deal_time(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def pair_close_deals(deals: list[dict]) -> dict[int, dict]:
    """position_id -> aggregated close outcome.

    Partial closes are summed for profit while the latest close deal
    donates price/time/comment.
    """
    closes: dict[int, dict] = {}
    for deal in deals:
        if deal.get("entry") == "OPEN":
            continue
        pid = deal.get("position_id")
        if not pid:
            continue
        pid = int(pid)
        deal_time = str(deal.get("time") or "")
        if pid not in closes:
            closes[pid] = {
                "profit": 0.0,
                "price": deal.get("price"),
                "time": deal.get("time"),
                "comment": deal.get("comment") or "",
            }
        slot = closes[pid]
        slot["profit"] += float(deal.get("profit") or 0.0)
        if deal_time >= str(slot.get("time") or ""):
            slot["price"] = deal.get("price")
            slot["time"] = deal.get("time")
            slot["comment"] = deal.get("comment") or ""
    return closes


async def fetch_position_close(ticket: int, hours: int = 2) -> Optional[dict]:
    """Close outcome for one position from recent history, if any."""
    deals = await fetch_deals(hours=hours)
    if not deals:
        return None
    return pair_close_deals(deals).get(int(ticket))


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def reconcile_trade_records() -> int:
    """Close open trade_records whose MT5 positions are already closed.

    Returns the number of records updated. Never raises — the scheduler
    calls this unattended.
    """
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(TradeRecord).where(
                    TradeRecord.status == "open",
                    TradeRecord.mt5_ticket.is_not(None),
                )
            )
            open_trades = list(result.scalars().all())
            if not open_trades:
                return 0

            now = datetime.now(timezone.utc)
            oldest = now
            for trade in open_trades:
                ex = _aware(trade.executed_at)
                if ex and ex < oldest:
                    oldest = ex
            hours = min(
                LOOKBACK_CAP_HOURS,
                max(24, int((now - oldest).total_seconds() // 3600) + 24),
            )

            deals = await fetch_deals(hours=hours)
            if deals is None:
                return 0

            closes = pair_close_deals(deals)
            updated = 0
            for trade in open_trades:
                close = closes.get(int(trade.mt5_ticket))
                if not close:
                    continue
                trade.status = "closed"
                trade.closed_at = _parse_deal_time(close.get("time")) or now
                if close.get("price") is not None:
                    trade.exit_price = close["price"]
                trade.profit_loss = float(close["profit"])
                updated += 1

            if updated:
                await db.commit()
                logger.info("[Reconcile] closed %d trade_record(s)", updated)
            return updated
    except Exception:
        logger.warning("[Reconcile] run failed:\n%s", traceback.format_exc())
        return 0


scheduler = AsyncIOScheduler()


def start_trade_reconciler():
    """Register the 5-minute profit reconciler with APScheduler."""
    if not scheduler.running:
        scheduler.add_job(
            reconcile_trade_records,
            "interval",
            minutes=5,
            id="trade-reconcile",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
    logger.info("[Reconcile] Trade reconciler started (every 5 min)")


def shutdown_reconciler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
