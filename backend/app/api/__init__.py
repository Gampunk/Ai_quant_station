# API package - imports all route modules
from . import auth, mt5, trade, ai, yahoo, execute, analytics, historical_lab, autopilot, backtest, rag_health

__all__ = ["auth", "mt5", "trade", "ai", "yahoo", "execute", "analytics", "historical_lab", "autopilot", "backtest", "rag_health"]
