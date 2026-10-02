"""
Risk limits: read them, change them, see their history, today's position against
them, and every order decision. Anyone logged in can read; only an admin changes.

Also the kill switch (/halt), closing every position (/close-all), and the
heartbeat's alerts (/alerts).
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from ..core.database import AsyncSessionLocal
from ..core.mt5_connector import ConnectorError, connector_client
from ..core.risk import (DEFAULTS, RANGES, current_halt, current_settings, day_start_source, halt_dict,
                         save_settings, set_halt, settings_dict, start_of_day_equity)
from ..core.security import get_current_user, require_role, require_trader
from ..models.risk import Alert, RiskDecision, RiskSettings

router = APIRouter(prefix="/risk", tags=["Risk"])
require_admin = require_role("admin")
log = logging.getLogger("risk")


class RiskSettingsUpdate(BaseModel):
    reason: str
    autopilot_risk_pct: Optional[float] = None
    max_trade_risk_pct: Optional[float] = None
    max_open_positions: Optional[int] = None
    daily_loss_pct: Optional[float] = None
    min_margin_level: Optional[float] = None
    max_pending_distance_pct: Optional[float] = None
    require_stop_loss: Optional[bool] = None


@router.get("/settings")
async def get_settings(current_user: dict = Depends(get_current_user)):
    async with AsyncSessionLocal() as db:
        row = await current_settings(db)
    return {"settings": settings_dict(row), "defaults": DEFAULTS,
            "ranges": {k: {"min": lo, "max": hi} for k, (lo, hi) in RANGES.items()}}


@router.put("/settings")
async def update_settings(body: RiskSettingsUpdate, current_user: dict = Depends(require_admin)):
    changes = body.model_dump(exclude_none=True)
    reason = changes.pop("reason")
    if not changes:
        raise HTTPException(status_code=400, detail="Nothing to change")
    try:
        row = await save_settings(changes, current_user, reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"settings": settings_dict(row)}


@router.get("/settings/history")
async def settings_history(limit: int = Query(50, ge=1, le=500), current_user: dict = Depends(get_current_user)):
    async with AsyncSessionLocal() as db:
        await current_settings(db)
        rows = (await db.execute(select(RiskSettings).order_by(RiskSettings.id.desc()).limit(limit))).scalars().all()
    return {"versions": [settings_dict(r) for r in rows]}


@router.get("/status")
async def risk_status(current_user: dict = Depends(get_current_user)):
    """Where the account stands against each account-wide limit right now."""
    try:
        account = await connector_client.get_account()
        positions = (await connector_client.get_positions()).get("positions", [])
    except ConnectorError as exc:
        raise HTTPException(status_code=502 if exc.status_code >= 500 else exc.status_code, detail=exc.detail)
    async with AsyncSessionLocal() as db:
        settings = settings_dict(await current_settings(db))
        start = await start_of_day_equity(db, account)
        start_source = await day_start_source(db, account)
    equity = account.get("equity")
    loss_pct = round((start - equity) / start * 100, 4) if start and equity is not None else None
    return {
        "settings_id": settings["id"],
        "equity": equity,
        "start_of_day_equity": start,
        # midnight: recorded by the 00:00 UTC job. first_check: that run was missed.
        "start_of_day_source": start_source,
        "daily_loss_pct": loss_pct,
        "daily_loss_limit_pct": settings["daily_loss_pct"],
        "margin_level": account.get("margin_level"),
        "min_margin_level": settings["min_margin_level"],
        "open_positions": len(positions),
        "max_open_positions": settings["max_open_positions"],
    }


@router.get("/decisions")
async def decisions(limit: int = Query(100, ge=1, le=1000), outcome: Optional[str] = None,
                    source: Optional[str] = None, current_user: dict = Depends(get_current_user)):
    query = select(RiskDecision).order_by(RiskDecision.id.desc()).limit(limit)
    if outcome:
        query = query.where(RiskDecision.outcome == outcome)
    if source:
        query = query.where(RiskDecision.source == source)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(query)).scalars().all()
    columns = [c.name for c in RiskDecision.__table__.columns]
    out = []
    for r in rows:
        item = {c: getattr(r, c) for c in columns}
        item["created_at"] = r.created_at.isoformat() if r.created_at else None
        out.append(item)
    return {"decisions": out}


# ── Kill switch ─────────────────────────────────────────────────────────────
class HaltChange(BaseModel):
    halted: bool
    reason: Optional[str] = None


@router.get("/halt")
async def get_halt(current_user: dict = Depends(get_current_user)):
    """Whether trading is stopped, by whom and why. Every page polls this for its banner."""
    async with AsyncSessionLocal() as db:
        return halt_dict(await current_halt(db))


@router.post("/halt")
async def change_halt(body: HaltChange, current_user: dict = Depends(require_trader)):
    """Stop all trading, or resume it.

    Anyone who can trade may stop it: in an emergency nobody should wait for an
    admin. Only an admin may resume, and must say why.
    """
    if not body.halted:
        if current_user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Only an admin can resume trading")
        if not (body.reason or "").strip():
            raise HTTPException(status_code=400, detail="Say why trading is being resumed")
    row = await set_halt(body.halted, current_user, body.reason)
    stopped = []
    if body.halted:
        from .autopilot import stop_all_autopilots
        stopped = await stop_all_autopilots(body.reason or "no reason given")
    return {**halt_dict(row), "autopilots_stopped": stopped}


class CloseAll(BaseModel):
    confirm: str


CLOSE_ALL_PHRASE = "CLOSE ALL"


@router.post("/close-all")
async def close_all_positions(body: CloseAll, current_user: dict = Depends(require_trader)):
    """Close every open position. Needs the typed phrase, so it cannot happen by a stray click."""
    if body.confirm != CLOSE_ALL_PHRASE:
        raise HTTPException(status_code=400, detail=f'Type "{CLOSE_ALL_PHRASE}" to confirm')
    from ..services.trade_service import TradeError, close_position
    try:
        positions = (await connector_client.get_positions()).get("positions", [])
    except ConnectorError as exc:
        raise HTTPException(status_code=502, detail=f"Could not read open positions: {exc.detail}")
    closed, failed = [], []
    for p in positions:
        try:
            closed.append(await close_position(p["ticket"], None, current_user["id"]))
        except TradeError as exc:
            log.error("Close all: position %s not closed: %s", p.get("ticket"), exc.message)
            failed.append({"ticket": p.get("ticket"), "error": exc.message})
    log.warning("Close all by %s: %d closed, %d failed", current_user.get("username"), len(closed), len(failed))
    return {"closed": closed, "failed": failed}


@router.get("/alerts")
async def alerts(limit: int = Query(20, ge=1, le=200), current_user: dict = Depends(get_current_user)):
    """The heartbeat's recent alerts, newest first."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Alert).order_by(Alert.id.desc()).limit(limit))).scalars().all()
    return {"alerts": [{"id": a.id, "created_at": a.created_at.isoformat() if a.created_at else None,
                        "check": a.check, "state": a.state, "message": a.message, "delivered": a.delivered}
                       for a in rows]}
