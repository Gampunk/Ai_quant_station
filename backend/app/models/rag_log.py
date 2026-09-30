from sqlalchemy import Column, Integer, String, DateTime, Index, ForeignKey, JSON
from datetime import datetime, timezone
from ..core.database import Base


class RagLog(Base):
    """Telemetry row written on every build_rag_context() call.

    Powers GET /api/rag-health: the "[RAG] symbol: X similar, Y top,
    Z losers" log line is mirrored here so the scoreboard plateau check
    (no strategy reaching the min-trades bar over the last N calls) can
    be computed from SQL instead of parsing journalctl.
    """

    __tablename__ = "rag_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    cycle_id = Column(String(36), nullable=True, index=True)
    symbol = Column(String, index=True)
    source = Column(String, nullable=True)
    query_hash = Column(String(64), nullable=True)
    context_hash = Column(String(64), nullable=True)
    selected_memories = Column(JSON, nullable=True)
    similar_count = Column(Integer, default=0)
    top_count = Column(Integer, default=0)
    losers_count = Column(Integer, default=0)
    context_chars = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)

    __table_args__ = (
        Index("ix_rag_logs_symbol_id", "symbol", "id"),
    )
