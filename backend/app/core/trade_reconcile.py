"""Reconcile trade_records with MT5 history deals.

Trades closed outside the app (SL/TP hits, manual closes in the MT5
terminal, partial closes) never reach trade_service.close_position, so
their profit_loss stayed NULL forever and the RAG score's 0.3 profit
weight never fired. This job matches open trade_records against MT5
history deals by position ticket and writes the outcome back.
"""
import logging
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from ..core.database import AsyncSessionLocal
from ..models.ai_memory import TradeRecord, AutopilotSettings

logger = logging.getLogger(__name__)

LOOKBACK_CAP_HOURS = 720  # never ask MT5 for more than 30 days of history


async def fetch_deals(hours: int) -> Optional[list[dict]]:
    """MT5 history deals, times in UTC; None when the connector is unreachable.

    Through the one connector client, like every other broker call. An earlier
    version also talked to MetaTrader5 directly, a second route the backend forbids.
    """
    from ..core.mt5_connector import ConnectorError, connector_client
    if not connector_client.configured:
        return None
    try:
        res = await connector_client.get_history(hours=hours)
    except ConnectorError as e:
        logger.warning("[Reconcile] connector history error: %s", e.detail)
        return None
    return res.get("deals") or []


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
        # Autopilot trades have their own lifecycle table and may close while
        # Autopilot is stopped, so reconcile them on the same server schedule.
        scheduler.add_job(
            reconcile_autopilot_trades,
            "interval",
            minutes=5,
            id="autopilot-trade-reconcile",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
    logger.info("[Reconcile] Trade reconciler started (every 5 min)")


async def reconcile_autopilot_trades() -> int:
    """Sync Autopilot closes periodically even when its decision loop is stopped."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AutopilotSettings.user_id))
            user_ids = result.scalars().all()
        if not user_ids:
            return 0
        from ..api.autopilot import sync_trade_results
        total = 0
        for user_id in user_ids:
            total += await sync_trade_results(int(user_id))
        return total
    except Exception:
        logger.warning("[Reconcile] Autopilot trade sync failed", exc_info=True)
        return 0


def shutdown_reconciler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
