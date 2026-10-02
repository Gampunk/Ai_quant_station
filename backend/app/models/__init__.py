"""
Central Models Index
Import all models to ensure they are registered with SQLAlchemy Base
"""

# Import all models here so they are registered with Base.metadata
from .user import User
from .market_data import MarketData
from .ai_memory import (
    ChatMemory,
    GlobalInsights,
    ModelUsage,
    TradeRecord,
    UserFeedback,
    CalculationHistory,
    IndicatorRequest,
    UserPreferences,
    AutopilotTrade,
    UserPrompt,
    DefaultPromptStrategy,
    AutopilotSettings,
    AiCallLog,
)
from .historical_lab import HistoricalBacktest
from .strategy_score import StrategyScore
from .chat_embedding import ChatEmbedding
from .risk import Alert, RiskDay, RiskDecision, RiskSettings, TradingHalt

__all__ = [
    "User",
    "MarketData",
    "ChatMemory",
    "GlobalInsights",
    "ModelUsage",
    "TradeRecord",
    "UserFeedback",
    "CalculationHistory",
    "IndicatorRequest",
    "UserPreferences",
    "AutopilotTrade",
    "UserPrompt",
    "DefaultPromptStrategy",
    "AutopilotSettings",
    "HistoricalBacktest",
    "StrategyScore",
    "ChatEmbedding",
    "AiCallLog",
    "RiskSettings",
    "RiskDay",
    "TradingHalt",
    "Alert",
    "RiskDecision",
]