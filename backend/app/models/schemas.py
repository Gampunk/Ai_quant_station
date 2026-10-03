from pydantic import BaseModel, Field
from typing import Optional, List, Union
from datetime import datetime


# Auth Schemas
class UserLogin(BaseModel):
    username: str
    password: str


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class TokenRefresh(BaseModel):
    refresh_token: str


class UserResponse(BaseModel):
    id: int
    username: str
    name: str
    role: str
    is_active: bool = True
    created_at: datetime
    last_login: Optional[datetime] = None

    class Config:
        from_attributes = True


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=6)


# MT5 Schemas
class MT5Symbol(BaseModel):
    name: str
    description: str
    visible: bool
    ask: Optional[float] = None
    bid: Optional[float] = None
    point: Optional[float] = None
    digits: Optional[int] = None
    volume_min: Optional[float] = None
    volume_max: Optional[float] = None


class MT5SymbolsResponse(BaseModel):
    count: int
    symbols: List[MT5Symbol]


class OHLCData(BaseModel):
    time: Union[str, int]  # str for yahoo, int (Unix timestamp) for MT5
    open: float
    high: float
    low: float
    close: float
    tick_volume: int
    spread: Optional[int] = None
    real_volume: Optional[int] = None


class FetchDataRequest(BaseModel):
    symbol: str
    timeframe: str = "1m"
    start_date: str
    end_date: str


class FetchLatestRequest(BaseModel):
    symbol: str
    timeframe: str = "1m"
    count: int = 1000


class DataResponse(BaseModel):
    success: bool
    symbol: str
    timeframe: str
    rows: int
    data: List[OHLCData]


class AccountInfo(BaseModel):
    login: int
    server: str
    name: str
    balance: float
    equity: float
    margin: float
    free_margin: float
    margin_level: float
    profit: float
    currency: str
    leverage: int


class Position(BaseModel):
    ticket: int
    symbol: str
    direction: str
    volume: float
    entry_price: float
    current_price: float
    sl: Optional[float] = None
    tp: Optional[float] = None
    profit: float
    open_time: str


class PositionsResponse(BaseModel):
    success: bool
    balance: float
    equity: float
    margin: float
    free_margin: float
    margin_level: float
    open_count: int
    total_profit: float
    positions: List[Position]


class Trade(BaseModel):
    ticket: int
    symbol: str
    direction: str
    volume: float
    price: float
    profit: float
    swap: float
    commission: float
    comment: str
    time: str  # UTC
    entry: str
    position_id: Optional[int] = None
    # Why the deal happened, from the broker: sl, tp, expert, client, stop_out...
    reason: Optional[str] = None


class HistoryResponse(BaseModel):
    success: bool
    count: int
    deals: List[Trade]


# Trade Schemas
class OrderRequest(BaseModel):
    symbol: str
    action: str
    volume: float
    price: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    comment: str = "[IMPULSE_V2]"
    magic: int = 0
    chat_memory_id: Optional[int] = None


class OrderResponse(BaseModel):
    success: bool
    ticket: int
    symbol: str
    action: str
    volume: float
    price: float
    sl: Optional[float]
    tp: Optional[float]
    comment: str
    # What the order risks if its stop is hit, as checked by the risk gate.
    risk_amount: Optional[float] = None
    risk_pct: Optional[float] = None


class CloseRequest(BaseModel):
    ticket: int
    volume: Optional[float] = None


class ModifyRequest(BaseModel):
    ticket: int
    sl: Optional[float] = None
    tp: Optional[float] = None


# AI Schemas
class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    provider: str
    model: str
    chat_session_id: Optional[str] = None  # NEW: used to isolate conversation history
    persona: Optional[str] = "technical_analyst"  # technical_analyst, risk_manager, quant, swing_trader, scalper
    symbol: Optional[str] = None
    load_market_data: Optional[str] = None  # "yahoo" or "mt5"
    data_period: Optional[str] = "1mo"  # 1d, 1w, 1mo, 3mo, 1y
    candle_count: Optional[int] = 1000
    timeframe: Optional[str] = "1h"  # Primary timeframe
    candle_data: Optional[List[dict]] = None  # Candle data from frontend
    refine_prompt: bool = True  # Automatically enrich/refine user query before sending to AI
    debug_rag: bool = False  # Return the injected RAG context in the response (visibility)


class ChatResponse(BaseModel):
    message: str
    detected_setup: Optional[dict] = None
    detected_action: Optional[dict] = None
    data_preview: Optional[str] = None
    detected_chart: Optional[dict] = None
    execution_output: Optional[str] = None
    execution_charts: Optional[List[dict]] = None
    execution_tables: Optional[List[dict]] = None
    chat_memory_id: Optional[int] = None
    chat_session_id: Optional[str] = None # NEW: echoes back the session ID
    rag_context: Optional[str] = None  # RAG context injected into the system prompt (debug_rag only)





class AIProvider(BaseModel):
    id: str
    name: str
    models: List[str]
    has_key: bool = False


class AIProvidersResponse(BaseModel):
    providers: List[AIProvider]


# Autopilot Schemas
class AutopilotStatus(BaseModel):
    enabled: bool
    lot_size: float
    interval: int
    success_count: int
    error_count: int
    last_run: Optional[str] = None
    next_run: Optional[str] = None


class AutopilotConfig(BaseModel):
    lot_size: float = 0.10
    interval: int = 300  # 5 minutes


class AutopilotLog(BaseModel):
    timestamp: str
    message: str
    type: str  # info, success, error


# Memory Schemas
class FeedbackRequest(BaseModel):
    rating: int
    feedback_text: str = ""
    message: str = ""


class MemoryInsight(BaseModel):
    id: int
    insight: str
    category: str
    occurrences: int
    last_seen: str


class MemoryStats(BaseModel):
    total_conversations: int
    total_trades_suggested: int
    successful_trades: int
    failed_trades: int
    win_rate: float


class MemoryResponse(BaseModel):
    conversations: List[dict]
    insights: List[MemoryInsight]
    preferences: dict
    stats: MemoryStats