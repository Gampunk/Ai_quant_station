"""
User Feedback and Analytics Endpoints
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text, case

from ..core.database import get_db, AsyncSessionLocal
from ..core.security import get_current_user
from ..core.config import settings
from ..core.mt5_connector import connector_client
from ..models.strategy_score import StrategyScore
from ..models.ai_memory import AutopilotTrade
from ..core.trade_outcome import close_result

import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics", tags=["User Analytics"])


class FeedbackRequest(BaseModel):
    chat_memory_id: Optional[int] = None
    is_helpful: bool
    notes: Optional[str] = None


class CalculationRecord(BaseModel):
    symbol: str
    indicator: str
    period: int
    value: float
    candle_count: int


# ── Reports Pydantic models ────────────────────────────────────────────────

class TodaySummary(BaseModel):
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    pnl: float = 0.0
    best_prompt: str = ""


class DailySummary(BaseModel):
    date: str
    trades: int
    wins: int
    losses: int
    win_rate: float
    pnl: float


class ReportsResponse(BaseModel):
    today: TodaySummary
    daily_history: List[DailySummary]
    prompts: List[dict]
    trades: List[dict]
    # Set when the broker's trade history could not be read: the figures are then incomplete.
    mt5_error: Optional[str] = None


@router.get("/reports")
async def get_reports(current_user: dict = Depends(get_current_user)):
    """Consolidated reports — reads [AUTOPILOT] trades from MT5 connector.
    No database queries — all data comes from the MT5 connector."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    thirty_days_ago = today_start - timedelta(days=30)

    trades: list[dict] = []
    mt5_error: str | None = None

    if connector_client.configured:
        try:
            mt5_deals = (await connector_client.get_history(hours=720)).get("deals", [])

            autopilot_pids = {
                d.get("position_id")
                for d in mt5_deals
                if d.get("position_id") and (d.get("comment") or "").strip().startswith("[AUTOPILOT]")
            }
            auto_deals = [d for d in mt5_deals if d.get("position_id") in autopilot_pids]

            pos_map: dict = {}
            for deal in auto_deals:
                pid = deal.get("position_id")
                if not pid:
                    continue
                if pid not in pos_map:
                    pos_map[pid] = {"open": None, "close": None}
                if deal.get("entry") == "OPEN":
                    pos_map[pid]["open"] = deal
                else:
                    pos_map[pid]["close"] = deal

            for pid, pair in pos_map.items():
                open_deal = pair["open"]
                close_deal = pair["close"]
                if not open_deal:
                    continue

                deal_time = open_deal.get("time", "")
                try:
                    deal_dt = datetime.strptime(deal_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    continue
                if deal_dt < thirty_days_ago:
                    continue

                direction = open_deal.get("direction", "")
                profit = close_deal.get("profit", 0) if close_deal else 0
                comment = open_deal.get("comment", "") or ""
                prompt_match = __import__("re").search(r"P(\d+)", comment)
                prompt_number = int(prompt_match.group(1)) if prompt_match else None

                res_type = close_result(close_deal)

                trades.append({
                    "id": -pid,
                    "prompt_number": prompt_number,
                    "prompt_text": f"Prompt #{prompt_number}" if prompt_number else "Autopilot",
                    "symbol": open_deal.get("symbol", ""),
                    "direction": direction,
                    "entry_price": open_deal.get("price"),
                    "stop_loss": None,
                    "take_profit": None,
                    "lot_size": open_deal.get("volume", 0),
                    "mt5_ticket": pid,
                    "executed_at": open_deal.get("time", "").replace(" ", "T"),
                    "result": res_type,
                    "profit": float(profit) if profit else 0.0,
                    "closed_at": close_deal.get("time", "").replace(" ", "T") if close_deal else None,
                    "reasoning": comment,
                    "confidence": None,
                })
        except Exception as exc:
            logger.exception("[Reports] Could not read trade history from the connector")
            mt5_error = f"Trade history unavailable: {exc}"
            trades = []

    # ── Today's summary ──
    today_str = today_start.strftime("%Y-%m-%d")
    today_trades = [t for t in trades if t["executed_at"][:10] == today_str]
    closed_today = [t for t in today_trades if t["result"] != "OPEN"]
    day_wins = sum(1 for t in closed_today if (t["profit"] or 0) > 0)
    day_losses = sum(1 for t in closed_today if (t["profit"] or 0) <= 0)
    day_pnl = sum(t["profit"] or 0 for t in closed_today)

    best_prompt = ""
    if closed_today:
        prompt_pnl: dict[int, float] = {}
        for t in closed_today:
            pn = t["prompt_number"]
            if pn:
                prompt_pnl[pn] = prompt_pnl.get(pn, 0) + (t["profit"] or 0)
        if prompt_pnl:
            best_pn = max(prompt_pnl, key=prompt_pnl.get)
            best_prompt = f"#{best_pn}" if best_pn > 0 else f"Custom-{abs(best_pn)}"

    today_summary = TodaySummary(
        trades=len(closed_today),
        wins=day_wins,
        losses=day_losses,
        win_rate=round(day_wins / len(closed_today) * 100, 1) if closed_today else 0.0,
        pnl=round(day_pnl, 2),
        best_prompt=best_prompt,
    )

    # ── Daily history (last 30 days) ──
    daily_map: dict[str, dict] = {}
    for t in trades:
        date_str = t["executed_at"][:10]
        if date_str not in daily_map:
            daily_map[date_str] = {"trades": 0, "wins": 0, "pnl": 0.0}
        daily_map[date_str]["trades"] += 1
        if t["result"] != "OPEN":
            daily_map[date_str]["pnl"] += t["profit"] or 0
            if (t["profit"] or 0) > 0:
                daily_map[date_str]["wins"] += 1

    daily_history = []
    for i in range(29, -1, -1):
        day = (today_start - timedelta(days=i)).strftime("%Y-%m-%d")
        if day in daily_map:
            d = daily_map[day]
            w = d["wins"]
            l = d["trades"] - w
            daily_history.append(DailySummary(
                date=day,
                trades=d["trades"],
                wins=w,
                losses=l,
                win_rate=round(w / d["trades"] * 100, 1) if d["trades"] > 0 else 0.0,
                pnl=d["pnl"],
            ))
        else:
            daily_history.append(DailySummary(date=day, trades=0, wins=0, losses=0, win_rate=0.0, pnl=0.0))

    # ── Prompt stats ──
    groups: dict[int, dict] = {}
    for t in trades:
        if t["result"] == "OPEN":
            continue
        pn = t["prompt_number"]
        if pn is None:
            continue
        if pn not in groups:
            groups[pn] = {"prompt_number": pn, "prompt_text": t["prompt_text"], "total_trades": 0, "wins": 0, "losses": 0, "total_profit": 0.0}
        groups[pn]["total_trades"] += 1
        groups[pn]["total_profit"] += t["profit"] or 0
        if (t["profit"] or 0) > 0:
            groups[pn]["wins"] += 1
        else:
            groups[pn]["losses"] += 1

    prompt_stats = []
    for g in groups.values():
        g["win_rate"] = round(g["wins"] / g["total_trades"] * 100, 1) if g["total_trades"] > 0 else 0.0
        g["avg_profit"] = round(g["total_profit"] / g["total_trades"], 2) if g["total_trades"] > 0 else 0.0
        g["total_profit"] = round(g["total_profit"], 2)
        pn = g["prompt_number"]
        g["display_name"] = f"Custom-{abs(pn)}" if pn < 0 else f"#{pn}"
        prompt_stats.append(g)
    prompt_stats.sort(key=lambda x: x["total_profit"], reverse=True)

    # ── Recent trades (last 50) ──
    trades.sort(key=lambda x: x["executed_at"], reverse=True)
    trades_list = trades[:50]

    return ReportsResponse(
        today=today_summary,
        daily_history=daily_history,
        prompts=prompt_stats,
        trades=trades_list,
        mt5_error=mt5_error,
    )


@router.get("/reports/export")
async def export_reports(current_user: dict = Depends(get_current_user)):
    """Export all trades (no limit — returns full trade list)."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    thirty_days_ago = today_start - timedelta(days=30)

    trades: list[dict] = []
    mt5_error: str | None = None

    if connector_client.configured:
        try:
            mt5_deals = (await connector_client.get_history(hours=720)).get("deals", [])

            autopilot_pids = {
                d.get("position_id")
                for d in mt5_deals
                if d.get("position_id") and (d.get("comment") or "").strip().startswith("[AUTOPILOT]")
            }
            auto_deals = [d for d in mt5_deals if d.get("position_id") in autopilot_pids]

            pos_map: dict = {}
            for deal in auto_deals:
                pid = deal.get("position_id")
                if not pid:
                    continue
                if pid not in pos_map:
                    pos_map[pid] = {"open": None, "close": None}
                if deal.get("entry") == "OPEN":
                    pos_map[pid]["open"] = deal
                else:
                    pos_map[pid]["close"] = deal

            for pid, pair in pos_map.items():
                open_deal = pair["open"]
                close_deal = pair["close"]
                if not open_deal:
                    continue

                deal_time = open_deal.get("time", "")
                try:
                    deal_dt = datetime.strptime(deal_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    continue
                if deal_dt < thirty_days_ago:
                    continue

                direction = open_deal.get("direction", "")
                profit = close_deal.get("profit", 0) if close_deal else 0
                comment = open_deal.get("comment", "") or ""
                prompt_match = __import__("re").search(r"P(\d+)", comment)
                prompt_number = int(prompt_match.group(1)) if prompt_match else None

                res_type = close_result(close_deal)

                trades.append({
                    "id": -pid,
                    "prompt_number": prompt_number,
                    "prompt_text": f"Prompt #{prompt_number}" if prompt_number else "Autopilot",
                    "symbol": open_deal.get("symbol", ""),
                    "direction": direction,
                    "entry_price": open_deal.get("price"),
                    "stop_loss": None,
                    "take_profit": None,
                    "lot_size": open_deal.get("volume", 0),
                    "mt5_ticket": pid,
                    "executed_at": open_deal.get("time", "").replace(" ", "T"),
                    "result": res_type,
                    "profit": float(profit) if profit else 0.0,
                    "closed_at": close_deal.get("time", "").replace(" ", "T") if close_deal else None,
                    "reasoning": comment,
                    "confidence": None,
                })
        except Exception as exc:
            logger.exception("[Reports] Could not read trade history from the connector")
            mt5_error = f"Trade history unavailable: {exc}"
            trades = []

    trades.sort(key=lambda x: x["executed_at"], reverse=True)
    return {"trades": trades, "total_count": len(trades), "mt5_error": mt5_error}


# ── Journal Pydantic models ─────────────────────────────────────────────────

class JournalSummary(BaseModel):
    total_trades: int = 0
    autopilot_trades: int = 0
    manual_trades: int = 0
    mt5_trades: int = 0
    mt5_available: bool = False
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0
    best_trade: Optional[dict] = None
    worst_trade: Optional[dict] = None


class JournalResponse(BaseModel):
    from_date: str
    to_date: str
    summary: JournalSummary
    trades: List[dict]
    page: int
    per_page: int
    has_next: bool
    has_prev: bool
    total_count: int


# ── Shared helper: fetch autopilot trades from MT5 ─────────────────────

async def _fetch_mt5_trades(from_date: str, to_date: Optional[str] = None) -> tuple[list[dict], bool]:
    """Fetch autopilot trades from MT5 connector within a date range.
    Returns (trades_list, mt5_available)."""
    try:
        day_start = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid from_date format. Use YYYY-MM-DD")
    if to_date:
        try:
            day_end = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid to_date format. Use YYYY-MM-DD")
    else:
        day_end = day_start + timedelta(days=1)

    trades: list[dict] = []
    mt5_available = False

    if connector_client.configured:
        hours = int((day_end - day_start).total_seconds() / 3600) + 1
        try:
            mt5_deals = (await connector_client.get_history(hours=max(hours, 24))).get("deals", [])
            mt5_available = True

            autopilot_pids = {
                d.get("position_id")
                for d in mt5_deals
                if d.get("position_id") and (d.get("comment") or "").strip().startswith("[AUTOPILOT]")
            }
            auto_deals = [d for d in mt5_deals if d.get("position_id") in autopilot_pids]

            pos_map: dict = {}
            for deal in auto_deals:
                pid = deal.get("position_id")
                if not pid:
                    continue
                if pid not in pos_map:
                    pos_map[pid] = {"open": None, "close": None}
                if deal.get("entry") == "OPEN":
                    pos_map[pid]["open"] = deal
                else:
                    pos_map[pid]["close"] = deal

            for pid, pair in pos_map.items():
                open_deal = pair["open"]
                close_deal = pair["close"]
                if not open_deal:
                    continue

                deal_time = open_deal.get("time", "")
                try:
                    deal_dt = datetime.strptime(deal_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    continue
                if deal_dt < day_start or deal_dt >= day_end:
                    continue

                direction = open_deal.get("direction", "")
                profit = close_deal.get("profit", 0) if close_deal else 0
                comment = open_deal.get("comment", "") or ""
                prompt_match = __import__("re").search(r"P(\d+)", comment)
                prompt_number = int(prompt_match.group(1)) if prompt_match else None

                res_type = close_result(close_deal)

                trades.append({
                    "id": -pid,
                    "source": "mt5_connector",
                    "symbol": open_deal.get("symbol", ""),
                    "direction": direction,
                    "entry_price": open_deal.get("price"),
                    "exit_price": close_deal.get("price") if close_deal else None,
                    "stop_loss": None,
                    "take_profit": None,
                    "lot_size": open_deal.get("volume", 0),
                    "profit": float(profit) if profit else 0.0,
                    "result": res_type,
                    "executed_at": open_deal.get("time", "").replace(" ", "T"),
                    "closed_at": close_deal.get("time", "").replace(" ", "T") if close_deal else None,
                    "prompt_number": prompt_number,
                    "prompt_text": f"Prompt #{prompt_number}" if prompt_number else "Autopilot",
                    "confidence": None,
                    "reasoning": comment,
                    "mt5_ticket": pid,
                })
        except Exception:
            logger.exception("[Journal] Could not read trade history from the connector")
            mt5_available = False
            trades = []

    trades.sort(key=lambda x: x.get("executed_at", ""), reverse=True)
    return trades, mt5_available


@router.get("/journal/export")
async def export_journal(
    from_date: str,
    to_date: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    """Export journal trades as JSON (no pagination — all trades in date range)."""
    user_id = current_user["id"]
    trades, mt5_available = await _fetch_mt5_trades(from_date, to_date)
    if not mt5_available or not trades:
        trades = await _fetch_local_trades(user_id, from_date, to_date)
    return {"trades": trades, "total_count": len(trades)}


def _db_trade_to_dict(t: AutopilotTrade) -> dict:
    """Convert an AutopilotTrade DB row to the journal trade dict format."""
    exit_price = t.exit_price or 0
    profit = t.profit or 0
    return {
        "id": t.id,
        "source": "autopilot",
        "symbol": t.symbol,
        "direction": t.direction,
        "entry_price": t.entry_price,
        "exit_price": t.exit_price,
        "stop_loss": t.stop_loss,
        "take_profit": t.take_profit,
        "lot_size": t.lot_size,
        "profit": profit,
        "result": t.result or "OPEN",
        "executed_at": t.executed_at.isoformat() if t.executed_at else "",
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
        "prompt_number": t.prompt_number,
        "prompt_text": t.prompt_text,
        "confidence": t.confidence,
        "reasoning": t.reasoning,
        "mt5_ticket": t.mt5_ticket,
    }


async def _fetch_local_trades(user_id: int, from_date: str, to_date: Optional[str] = None) -> list[dict]:
    """Fallback: fetch trades from local autopilot_trades table."""
    try:
        day_start = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid from_date format. Use YYYY-MM-DD")
    if to_date:
        try:
            day_end = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid to_date format. Use YYYY-MM-DD")
    else:
        day_end = day_start + timedelta(days=1)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AutopilotTrade).where(
                AutopilotTrade.user_id == user_id,
                AutopilotTrade.executed_at >= day_start,
                AutopilotTrade.executed_at < day_end,
            ).order_by(AutopilotTrade.executed_at.desc())
        )
        trades = result.scalars().all()

    return [_db_trade_to_dict(t) for t in trades]


@router.get("/journal")
async def get_journal(
    from_date: str,
    to_date: Optional[str] = None,
    page: int = 1,
    per_page: int = 20,
    current_user: dict = Depends(get_current_user),
):
    """Trade journal — displays autopilot trades from MT5 connector (with DB fallback)."""
    per_page = min(per_page, 100)
    user_id = current_user["id"]

    try:
        day_start = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid from_date format. Use YYYY-MM-DD")
    if to_date:
        try:
            day_end = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid to_date format. Use YYYY-MM-DD")
    else:
        day_end = day_start + timedelta(days=1)

    trades, mt5_available = await _fetch_mt5_trades(from_date, to_date)

    if not mt5_available or not trades:
        trades = await _fetch_local_trades(user_id, from_date, to_date)

    total = len(trades)

    closed_trades = [t for t in trades if t.get("result") != "OPEN"]
    wins = sum(1 for t in closed_trades if (t.get("profit") or 0) > 0)
    losses = sum(1 for t in closed_trades if (t.get("profit") or 0) <= 0)
    pnl = sum(t.get("profit") or 0 for t in trades)

    best = max(trades, key=lambda t: t.get("profit") or 0) if trades else None
    worst = min(trades, key=lambda t: t.get("profit") or 0) if trades else None

    summary = JournalSummary(
        total_trades=total,
        autopilot_trades=total,
        manual_trades=0,
        mt5_trades=total,
        wins=wins,
        losses=losses,
        pnl=round(pnl, 2),
        best_trade={
            "symbol": best["symbol"],
            "direction": best["direction"],
            "profit": round(best["profit"], 2),
            "source": best["source"],
        } if best else None,
        worst_trade={
            "symbol": worst["symbol"],
            "direction": worst["direction"],
            "profit": round(worst["profit"], 2),
            "source": worst["source"],
        } if worst else None,
        mt5_available=mt5_available,
    )

    offset = (page - 1) * per_page
    page_trades = trades[offset:offset + per_page]

    return JournalResponse(
        from_date=day_start.strftime("%Y-%m-%d"),
        to_date=(day_end - timedelta(days=1)).strftime("%Y-%m-%d"),
        summary=summary,
        trades=page_trades,
        page=page,
        per_page=per_page,
        has_next=offset + per_page < total,
        has_prev=page > 1,
        total_count=total,
    )


@router.get("/test")
async def test_endpoint(current_user: dict = Depends(get_current_user)):
    """Simple test endpoint"""
    return {"status": "ok", "message": "Analytics is working"}


@router.post("/feedback")
async def submit_feedback(
    feedback: FeedbackRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Submit feedback on AI response"""
    try:
        from app.models.ai_memory import UserFeedback
        fb = UserFeedback(
            user_id=current_user["id"],
            chat_memory_id=feedback.chat_memory_id,
            is_helpful=feedback.is_helpful,
            notes=feedback.notes
        )
        db.add(fb)
        await db.commit()
        return {"success": True, "message": "Feedback recorded"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/calculation")
async def record_calculation(
    calc: CalculationRecord,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Record a calculation performed by AI (ATR, RSI, etc.)"""
    try:
        from app.models.ai_memory import CalculationHistory, IndicatorRequest
        from sqlalchemy import select

        calc_record = CalculationHistory(
            user_id=current_user["id"],
            symbol=calc.symbol,
            indicator=calc.indicator,
            period=calc.period,
            value=calc.value,
            candle_count=calc.candle_count
        )
        db.add(calc_record)

        result = await db.execute(
            select(IndicatorRequest).where(
                IndicatorRequest.user_id == current_user["id"],
                IndicatorRequest.symbol == calc.symbol,
                IndicatorRequest.indicator == calc.indicator
            )
        )
        indicator_req = result.scalar_one_or_none()

        if indicator_req:
            indicator_req.request_count += 1
        else:
            new_req = IndicatorRequest(
                user_id=current_user["id"],
                symbol=calc.symbol,
                indicator=calc.indicator,
                period=calc.period
            )
            db.add(new_req)

        await db.commit()
        return {"success": True, "message": "Calculation recorded"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/calculations")
async def get_calculation_history(
    symbol: Optional[str] = None,
    indicator: Optional[str] = None,
    skip: int = 0,
    limit: int = 20,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get user's calculation history"""
    limit = min(limit, 500)
    try:
        from app.models.ai_memory import CalculationHistory
        from sqlalchemy import select, desc

        query = select(CalculationHistory).where(
            CalculationHistory.user_id == current_user["id"]
        )

        if symbol:
            query = query.where(CalculationHistory.symbol == symbol)
        if indicator:
            query = query.where(CalculationHistory.indicator == indicator)

        query = query.order_by(desc(CalculationHistory.created_at)).offset(skip).limit(limit)

        result = await db.execute(query)
        records = result.scalars().all()

        return [
            {
                "id": r.id,
                "symbol": r.symbol,
                "indicator": r.indicator,
                "period": r.period,
                "value": float(r.value),
                "candle_count": r.candle_count,
                "created_at": r.created_at.isoformat() if r.created_at else None
            }
            for r in records
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/indicator-stats")
async def get_indicator_stats(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get aggregated indicator usage stats"""
    try:
        from app.models.ai_memory import IndicatorRequest
        from sqlalchemy import select, func

        result = await db.execute(
            select(
                IndicatorRequest.indicator,
                func.sum(IndicatorRequest.request_count).label("total_requests"),
                func.avg(IndicatorRequest.period).label("avg_period")
            )
            .where(IndicatorRequest.user_id == current_user["id"])
            .group_by(IndicatorRequest.indicator)
        )

        stats = []
        for row in result:
            symbol_result = await db.execute(
                select(IndicatorRequest.symbol)
                .where(
                    IndicatorRequest.user_id == current_user["id"],
                    IndicatorRequest.indicator == row[0]
                )
                .order_by(IndicatorRequest.request_count.desc())
                .limit(1)
            )
            symbol_row = symbol_result.fetchone()
            most_used = symbol_row[0] if symbol_row else None

            stats.append({
                "indicator": row[0],
                "total_requests": row[1],
                "most_used_symbol": most_used,
                "avg_period": float(row[2]) if row[2] else None
            })

        return stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/feedback-stats")
async def get_feedback_stats(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get user's feedback statistics"""
    try:
        from app.models.ai_memory import UserFeedback
        from sqlalchemy import select, func

        total_result = await db.execute(
            select(func.count(UserFeedback.id))
            .where(UserFeedback.user_id == current_user["id"])
        )
        total = total_result.scalar() or 0

        helpful_result = await db.execute(
            select(func.count(UserFeedback.id))
            .where(
                UserFeedback.user_id == current_user["id"],
                UserFeedback.is_helpful == True
            )
        )
        helpful = helpful_result.scalar() or 0

        return {
            "total_feedback": total,
            "helpful_count": helpful,
            "not_helpful_count": total - helpful,
            "helpful_percentage": round((helpful / total * 100), 1) if total > 0 else 0
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/strategy-scores")
async def get_strategy_scores(
    symbol: Optional[str] = None,
    sort: str = "win_rate",
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get strategy scoreboard with win rate and P&L per prompt.
    Refreshes from latest trade data on every call."""
    try:
        from ..core.strategy_scorer import update_strategy_scores
        await update_strategy_scores()

        from sqlalchemy import select, desc

        query = select(StrategyScore)
        if symbol:
            query = query.where(StrategyScore.symbol == symbol)

        sort_col = getattr(StrategyScore, sort, StrategyScore.win_rate)
        query = query.order_by(desc(sort_col)).limit(50)

        result = await db.execute(query)
        scores = result.scalars().all()

        return [
            {
                "prompt_text": s.prompt_text,
                "symbol": s.symbol,
                "direction": s.direction,
                "source": s.source,
                "total_trades": s.total_trades,
                "winning_trades": s.winning_trades,
                "total_pnl": float(s.total_pnl) if s.total_pnl else 0,
                "win_rate": float(s.win_rate) if s.win_rate else 0,
                "avg_confidence": float(s.avg_confidence) if s.avg_confidence else None,
                "avg_profit": float(s.avg_profit) if s.avg_profit else None,
                "avg_loss": float(s.avg_loss) if s.avg_loss else None,
                "profit_factor": float(s.profit_factor) if s.profit_factor else None,
                "last_used": s.last_used.isoformat() if s.last_used else None,
            }
            for s in scores
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
