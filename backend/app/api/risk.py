"""
Risk limits: read them, change them, see their history, today's position against
them, and every order decision. Anyone logged in can read; only an admin changes.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from ..core.database import AsyncSessionLocal
from ..core.mt5_connector import ConnectorError, connector_client
from ..core.risk import DEFAULTS, RANGES, current_settings, save_settings, settings_dict, start_of_day_equity
from ..core.security import get_current_user, require_role
from ..models.risk import RiskDecision, RiskSettings

router = APIRouter(prefix="/risk", tags=["Risk"])
require_admin = require_role("admin")


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
    equity = account.get("equity")
    loss_pct = round((start - equity) / start * 100, 4) if start and equity is not None else None
    return {
        "settings_id": settings["id"],
        "equity": equity,
        "start_of_day_equity": start,
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
