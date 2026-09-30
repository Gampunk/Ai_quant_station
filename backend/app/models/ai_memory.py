from sqlalchemy import Column, Integer, String, DateTime, Float, Text, ForeignKey, JSON, Boolean, BigInteger, Numeric, UniqueConstraint, Index, true
from datetime import datetime, timezone
from ..core.database import Base


class ChatMemory(Base):
    __tablename__ = "chat_memories"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    chat_session_id = Column(String, nullable=True, index=True) # New column for session-based history
    symbol = Column(String, nullable=True)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    reasoning = Column(Text, nullable=True)
    raw_thinking = Column(JSON, nullable=True)
    provider = Column(String, nullable=True)
    model = Column(String, nullable=True)
    tokens_used = Column(Integer, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    detected_setup = Column(JSON, nullable=True)
    detected_action = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)


    __table_args__ = (
        Index("ix_chat_memories_user_symbol", "user_id", "symbol"),
    )


class GlobalInsights(Base):
    __tablename__ = "global_insights"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, nullable=False, unique=True, index=True)
    
    total_analyzed = Column(Integer, default=0)
    buy_signals = Column(Integer, default=0)
    sell_signals = Column(Integer, default=0)
    
    avg_entry_price = Column(Float, nullable=True)
    avg_stop_loss = Column(Float, nullable=True)
    avg_take_profit = Column(Float, nullable=True)
    
    total_conversations = Column(Integer, default=0)
    last_updated = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class ModelUsage(Base):
    __tablename__ = "model_usage"

    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String, nullable=False)
    model = Column(String, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    
    total_requests = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    total_cost = Column(Float, default=0.0)
    
    last_used = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("provider", "model", "user_id", name="uq_model_usage"),
    )


class TradeRecord(Base):
    __tablename__ = "trade_records"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    symbol = Column(String, nullable=False)
    direction = Column(String, nullable=False)
    entry_price = Column(Float, nullable=False)
    # The quote when a market order was sent. entry_price is the broker's fill;
    # the difference is the slippage. Empty for pending orders.
    requested_price = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    take_profit = Column(Float, nullable=True)
    volume = Column(Float, nullable=False)
    order_type = Column(String, default="market")
    status = Column(String, default="open")
    
    executed_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    closed_at = Column(DateTime(timezone=True), nullable=True)
    exit_price = Column(Float, nullable=True)
    # The quote when a close was sent from the Terminal page. Empty for stops and targets.
    requested_exit_price = Column(Float, nullable=True)
    profit_loss = Column(Float, nullable=True)
    
    magic_number = Column(Integer, nullable=True, index=True)
    mt5_ticket = Column(BigInteger, nullable=True, unique=True, index=True)
    comment = Column(String, nullable=True)
    
    ai_message = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_trade_records_user_symbol", "user_id", "symbol"),
    )


class UserFeedback(Base):
    __tablename__ = "user_feedback"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    chat_memory_id = Column(Integer, ForeignKey("chat_memories.id", ondelete="SET NULL"), nullable=True)
    is_helpful = Column(Boolean, nullable=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class CalculationHistory(Base):
    __tablename__ = "calculation_history"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    symbol = Column(String, nullable=False)
    indicator = Column(String, nullable=False)
    period = Column(Integer, nullable=False)
    value = Column(Float, nullable=False)
    candle_count = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_calc_history_user_symbol_indicator", "user_id", "symbol", "indicator"),
        Index("ix_calc_history_user_created", "user_id", "created_at"),
    )


class IndicatorRequest(Base):
    __tablename__ = "indicator_requests"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    symbol = Column(String, nullable=False)
    indicator = Column(String, nullable=False)
    period = Column(Integer, nullable=True)
    request_count = Column(Integer, default=1)
    last_requested = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("user_id", "symbol", "indicator", "period", name="uq_indicator_request"),
    )


class UserPreferences(Base):
    __tablename__ = "user_preferences"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    
    favorite_symbols = Column(JSON, nullable=True)
    default_provider = Column(String, default="nvidia")
    default_model = Column(String, default="qwen/qwen3.5-122b-a10b")
    default_data_source = Column(String, default="yahoo")
    default_period = Column(String, default="1mo")
    
    theme = Column(String, default="dark")

    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class AutopilotTrade(Base):
    __tablename__ = "autopilot_trades"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    prompt_number = Column(Integer, nullable=False)
    prompt_text = Column(String, nullable=False)

    symbol = Column(String, nullable=False)
    direction = Column(String, nullable=False)
    entry_price = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    take_profit = Column(Float, nullable=True)
    lot_size = Column(Float, nullable=False)
    order_type = Column(String, default="market")

    mt5_ticket = Column(BigInteger, nullable=True)
    executed_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    execution_price = Column(Float, nullable=True)
    # The quote when a market order was sent; execution_price is the fill.
    # entry_price is what the AI proposed, which is neither.
    requested_price = Column(Float, nullable=True)
    execution_status = Column(String, default="pending")

    result = Column(String, nullable=True)
    profit = Column(Float, nullable=True)
    exit_price = Column(Float, nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    duration_minutes = Column(Integer, nullable=True)

    reasoning = Column(Text, nullable=True)
    confidence = Column(Float, nullable=True)
    ai_response = Column(Text, nullable=True)
    raw_thinking = Column(JSON, nullable=True)
    market_regime = Column(String, nullable=True, index=True)
    regime_details = Column(JSON, nullable=True)
    prompt_tags = Column(JSON, nullable=True)
    decision_score = Column(Float, nullable=True)
    decision_context = Column(JSON, nullable=True)

    provider = Column(String, nullable=True)
    model = Column(String, nullable=True)
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)

    source = Column(String, nullable=True)
    call_count = Column(Integer, nullable=True)
    call_tokens = Column(Integer, nullable=True)

    cycle_number = Column(Integer, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_autopilot_trades_user_executed", "user_id", "executed_at"),
        Index("ix_autopilot_trades_user_result", "user_id", "result"),
        Index("ix_autopilot_trades_user_prompt", "user_id", "prompt_number"),
        Index("ix_autopilot_trades_user_cycle", "user_id", "cycle_number"),
    )


class UserPrompt(Base):
    __tablename__ = "user_prompts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    content = Column(Text, nullable=False)
    strategy_code = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class DefaultPromptStrategy(Base):
    __tablename__ = "default_prompt_strategies"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    prompt_number = Column(Integer, unique=True, nullable=False)
    strategy_code = Column(Text, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class AutopilotSettings(Base):
    __tablename__ = "autopilot_settings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)

    enabled = Column(Boolean, default=False)

    interval_seconds = Column(Integer, default=300)
    default_lot = Column(Float, default=0.10)
    max_trades_per_day = Column(Integer, default=10)
    cooldown_minutes = Column(Integer, default=5)

    max_daily_loss = Column(Float, default=-50.0)
    # The autopilot's own daily loss brake can be switched off; the account-wide limit still applies.
    daily_loss_limit_enabled = Column(Boolean, nullable=False, default=True, server_default=true())

    mt5_terminal_path = Column(String, nullable=True)
    mt5_connector_url = Column(String, nullable=True)
    mt5_connected = Column(Boolean, default=False)

    symbol = Column(String, default="XAUUSD")

    provider = Column(String, default="nvidia")
    model = Column(String, default="qwen/qwen3.5-122b-a10b")

    selected_prompts = Column(JSON, default=list)

    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class RevokedToken(Base):
    __tablename__ = "revoked_tokens"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    token_jti = Column(String, nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)


class PositionAudit(Base):
    """Audit trail for position close/modify actions via the API."""
    __tablename__ = "position_audits"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    mt5_ticket = Column(BigInteger, nullable=False, index=True)
    action = Column(String, nullable=False)  # "close" or "modify"
    symbol = Column(String, nullable=False)
    original_sl = Column(Float, nullable=True)
    original_tp = Column(Float, nullable=True)
    new_sl = Column(Float, nullable=True)
    new_tp = Column(Float, nullable=True)
    close_volume = Column(Float, nullable=True)
    close_price = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)

    __table_args__ = (
        Index("ix_position_audits_user_created", "user_id", "created_at"),
    )


class AutopilotLog(Base):
    """Persistent autopilot log entries for analysis and debugging."""
    __tablename__ = "autopilot_logs"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    level = Column(String(20), nullable=False, index=True)
    message = Column(Text, nullable=False)
    cycle_number = Column(Integer, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_autopilot_logs_user_timestamp", "user_id", "timestamp"),
        Index("ix_autopilot_logs_user_level", "user_id", "level"),
        Index("ix_autopilot_logs_user_cycle", "user_id", "cycle_number"),
    )


class AiCallLog(Base):
    """Log every AI API call made by the autopilot — including failed/retry calls."""
    __tablename__ = "ai_call_logs"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    prompt_number = Column(Integer, nullable=True, index=True)
    cycle_number = Column(Integer, nullable=True, index=True)
    provider = Column(String, nullable=True)
    model = Column(String, nullable=True)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    stage = Column(String, nullable=True)
    outcome = Column(String, nullable=True)
    error_message = Column(String, nullable=True)
    cost = Column(Float, default=0.0)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)

    __table_args__ = (
        Index("ix_ai_call_logs_user_cycle", "user_id", "cycle_number"),
    )
