"""
Risk limits, the daily equity baseline, and every order decision.

Settings are never edited in place. Each change adds a row, and the newest row is
the one in force, so research can tell which limits applied to any decision.
"""
from datetime import datetime, timezone

from sqlalchemy import (JSON, BigInteger, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer,
                        String, Text, UniqueConstraint)

from ..core.database import Base


def _now():
    return datetime.now(timezone.utc)


class RiskSettings(Base):
    __tablename__ = "risk_settings"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    created_at = Column(DateTime(timezone=True), default=_now, index=True)
    changed_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    changed_by_name = Column(String, nullable=True)
    reason = Column(Text, nullable=True)

    # Percent of equity the autopilot risks on one trade; it sizes every order from this.
    autopilot_risk_pct = Column(Float, nullable=False)
    # Most any single order may risk, from any page, in percent of equity.
    max_trade_risk_pct = Column(Float, nullable=False)
    # 0 means no limit.
    max_open_positions = Column(Integer, nullable=False)
    # Loss since the start of the UTC day, in percent of that day's starting equity. 0 turns it off.
    daily_loss_pct = Column(Float, nullable=False)
    # No new orders while the margin level is below this percent. 0 turns it off.
    min_margin_level = Column(Float, nullable=False)
    # How far a pending order's price may be from the market, in percent. 0 turns it off.
    max_pending_distance_pct = Column(Float, nullable=False)
    require_stop_loss = Column(Boolean, nullable=False)
    # A missing stop is set this many ATR(14, 15-minute) from the price. 0 is off.
    default_stop_atr_mult = Column(Float, nullable=False, default=0.0, server_default="0")


class RiskDay(Base):
    """The equity at the start of each UTC day, per trading account, the daily loss baseline."""
    __tablename__ = "risk_days"

    id = Column(Integer, primary_key=True, autoincrement=True)
    day = Column(String, nullable=False)  # YYYY-MM-DD, UTC
    account_login = Column(BigInteger, nullable=False)
    start_equity = Column(Float, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now)
    # "midnight": recorded by the 00:00 UTC job. "first_check": the job missed the day
    # (connector down at midnight, server off), so the first order check recorded it.
    source = Column(String, nullable=False, default="first_check", server_default="first_check")

    __table_args__ = (UniqueConstraint("day", "account_login", name="uq_risk_days_day_account"),)


class RiskDecision(Base):
    __tablename__ = "risk_decisions"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    created_at = Column(DateTime(timezone=True), default=_now, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    source = Column(String, nullable=False)       # terminal, ai_analyst, autopilot
    action = Column(String, nullable=False)       # BUY, SELL, BUY_LIMIT, ..., MODIFY
    symbol = Column(String, nullable=False)
    # refused: a risk rule stopped it. sent: the broker accepted it. failed: the broker refused it.
    outcome = Column(String, nullable=False, index=True)
    reason_code = Column(String, nullable=True, index=True)
    message = Column(Text, nullable=True)

    requested_volume = Column(Float, nullable=True)
    volume = Column(Float, nullable=True)
    price = Column(Float, nullable=True)          # the market price or pending price it was judged against
    sl = Column(Float, nullable=True)
    tp = Column(Float, nullable=True)
    stop_distance = Column(Float, nullable=True)
    risk_amount = Column(Float, nullable=True)    # account currency lost if the stop is hit
    risk_pct = Column(Float, nullable=True)
    equity = Column(Float, nullable=True)
    settings_id = Column(Integer, ForeignKey("risk_settings.id", ondelete="SET NULL"), nullable=True)
    mt5_ticket = Column(BigInteger, nullable=True, index=True)
    context = Column(JSON, nullable=True)         # prompt, market regime and anything else the caller knows

    __table_args__ = (Index("ix_risk_decisions_source_created", "source", "created_at"),)


class TradingHalt(Base):
    """The kill switch. Never edited: each switch on or off adds a row, the newest is in force."""
    __tablename__ = "trading_halts"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    created_at = Column(DateTime(timezone=True), default=_now, index=True)
    halted = Column(Boolean, nullable=False)
    changed_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    changed_by_name = Column(String, nullable=True)
    reason = Column(Text, nullable=True)


class Alert(Base):
    """A heartbeat alert: a check started failing ("down") or recovered ("up")."""
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    created_at = Column(DateTime(timezone=True), default=_now, index=True)
    check = Column(String, nullable=False, index=True)   # connector, prices, autopilot:<user id>
    state = Column(String, nullable=False)               # down, up
    message = Column(Text, nullable=False)
    delivered = Column(Boolean, nullable=False, default=False)  # sent to Telegram
