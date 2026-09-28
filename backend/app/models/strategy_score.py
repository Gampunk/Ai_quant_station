from sqlalchemy import Column, Integer, String, DateTime, Float, Text, UniqueConstraint
from datetime import datetime, timezone
from ..core.database import Base


class StrategyScore(Base):
    __tablename__ = "strategy_scores"

    id = Column(Integer, primary_key=True, index=True)
    prompt_text = Column(Text, nullable=False)
    symbol = Column(String, nullable=False)
    direction = Column(String, nullable=True)
    source = Column(String, nullable=False)

    total_trades = Column(Integer, default=0)
    winning_trades = Column(Integer, default=0)
    total_pnl = Column(Float, default=0.0)
    win_rate = Column(Float, default=0.0)
    avg_confidence = Column(Float, nullable=True)
    avg_profit = Column(Float, nullable=True)
    avg_loss = Column(Float, nullable=True)
    profit_factor = Column(Float, nullable=True)

    total_tokens = Column(Integer, default=0)
    avg_tokens = Column(Integer, nullable=True)
    total_cost = Column(Float, default=0.0)
    avg_cost = Column(Float, nullable=True)
    cost_efficiency = Column(Float, nullable=True)
    roi_per_dollar = Column(Float, nullable=True)

    first_used = Column(DateTime(timezone=True), nullable=True)
    last_used = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("prompt_text", "symbol", "direction", "source",
                         name="uq_strategy_score"),
    )
