from fastapi import APIRouter, Depends, HTTPException, Body, Request
from typing import Optional, List
import json
import re
import asyncio
import logging
import traceback
from datetime import datetime, timezone
from time import time
import httpx
import pandas as pd
from openai import AsyncOpenAI
from openai import RateLimitError, APIError, Timeout
from .execute import run_python_code
from ..core.historical_loader import add_indicators
from ..core.utils import detect_trade_setup as _detect_trade_setup, get_robust_code_gen_prompt
from ..core.mt5_service import fetch_latest_candles


from ..core.config import settings
from ..core.security import get_current_user
from ..core.database import AsyncSessionLocal
from ..models.market_data import MarketData
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy import select, func, delete
from ..models.schemas import (
    ChatRequest,
    ChatResponse,
    AIProvider,
    AIProvidersResponse,
    MemoryResponse,
    MemoryInsight,
    MemoryStats,
    FeedbackRequest,
)
from ..models.ai_memory import (
    ChatMemory,
    UserPreferences,
    UserFeedback,
    GlobalInsights,
    ModelUsage,
)
from ..core.providers import PROVIDERS, get_api_key as _get_api_key, get_base_url, resolve_api_key, resolve_all_api_keys
from ..core.models_cache import get_live_models as _get_live_models
from ..models.user import UserApiKey
from ..core.encryption import encrypt_api_key
from ..core.rag_service import build_rag_context, generate_embedding
from ..core.rate_limit import limiter

router = APIRouter(prefix="/ai", tags=["AI"])

logger = logging.getLogger(__name__)

# Strong references for fire-and-forget tasks (embedding generation).
# Without these, CPython may garbage-collect the task mid-execution and the
# embedding is silently never stored.
_background_tasks: set = set()


def _spawn_background_task(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _on_done(t: "asyncio.Task") -> None:
        _background_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.warning(f"Background task failed: {t.exception()}")

    task.add_done_callback(_on_done)

# Timeframe detection helpers
TF_MAPPING = {
    "M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
    "H1": "1h", "H2": "2h", "H4": "4h",
    "D1": "1d", "W1": "1w",
}

# Robust regex patterns for timeframes
# Handles: 1m, 1 m, 1-m, 1min, 1 minute, 1-minute, etc.
_NL_TF_PATTERNS = [
    (r'\b1[-\s]?(?:m|min(?:ute)?s?)\b', "1m"),
    (r'\b5[-\s]?(?:m|min(?:ute)?s?)\b', "5m"),
    (r'\b15[-\s]?(?:m|min(?:ute)?s?)\b', "15m"),
    (r'\b30[-\s]?(?:m|min(?:ute)?s?)\b', "30m"),
    (r'\b1[-\s]?(?:h|hour(?:ly)?s?)\b|\bhourly\b', "1h"),
    (r'\b2[-\s]?(?:h|hour(?:s)?)\b', "2h"),
    (r'\b4[-\s]?(?:h|hour(?:s)?)\b|\bfour[-\s]?hour\b', "4h"),
    (r'\b1[-\s]?(?:d|day(?:s)?)\b|\bdaily\b', "1d"),
    (r'\b1[-\s]?(?:w|week(?:s)?)\b|\bweekly\b', "1w"),
]

def _detect_timeframe(text: str) -> Optional[str]:
    """Detect timeframe from user query. If multiple are mentioned, returns the smallest (most granular)."""
    if not text:
        return None
    
    detected = []
    for label, tf in TF_MAPPING.items():
        if re.search(rf'\b{label}\b', text, re.IGNORECASE):
            detected.append(tf)
    for pattern, tf in _NL_TF_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            detected.append(tf)
            
    if not detected:
        return None
        
    # Sort by granularity and return the smallest
    def tf_to_minutes(tf_str):
        unit = tf_str[-1]
        val = int(tf_str[:-1])
        if unit == 'm': return val
        if unit == 'h': return val * 60
        if unit == 'd': return val * 1440
        if unit == 'w': return val * 10080
        return 999999
        
    return min(detected, key=tf_to_minutes)

MAX_RETRIES = 3
RETRY_DELAY = 2
REQUEST_TIMEOUT = 30

DATA_CAPABILITY = "- You have access to a pandas DataFrame `df` containing all historical OHLCV data ({candle_count}) loaded in a Python sandbox.\n"

PERSONAS = {}
PERSONAS["technical_analyst"] = """You are a Senior Technical Analyst with 15 years of experience in forex, crypto, and indices markets, specializing in price action and technical analysis.

CAPABILITIES:
{DATA_CAPABILITY}- Available libraries: pandas, numpy, ta (for technical indicators), scipy, statsmodels, matplotlib, seaborn.
- You can write Python code blocks (```python) that will be executed in the sandbox against the live `df`.
- Use `print()` to show numerical results, `show_chart(data, title, color)` for line/bar charts, and `show_table(df, title)` for tabular data.
- Indicators: use `ta.momentum.rsi(close, window=14)`, `ta.trend.sma_indicator(close, window=200)`, `ta.trend.ema_indicator(close, window=50)`, `ta.volatility.bollinger_hband(close, window=20)`, `ta.volatility.average_true_range(high, low, close, window=14)`, etc.

ANALYSIS FRAMEWORK:
1. Assess trend direction using moving averages and swing structure.
2. Identify key support/resistance levels from recent price action.
3. Calculate confirming indicators (RSI, MACD, volume profile) using Python code blocks.
4. Determine entry, stop-loss, and take-profit levels based on structure, not indicators alone.
5. Require confluence -- at least 2 independent signals before recommending a trade.

OUTPUT FORMAT:
- Begin with your prose analysis explaining the technical setup.
- If a trade is warranted, append a JSON block:
```json
{"action": "TRADE_SETUP", "symbol": "<symbol>", "direction": "BUY|SELL", "order_type": "market|limit|stop", "entry_price": <float>, "stop_loss": <float>, "take_profit": <float>, "lot_size": <float>, "risk_reward": <float>, "reasoning": "<brief explanation>"}
```
- If no valid setup exists: "NO TECHNICAL SETUP -- <reason>"
- Minimum risk-reward ratio: 1:2.
- Never guarantee profits -- always state: "This setup carries risk. Use appropriate position sizing."
"""

PERSONAS["risk_manager"] = """You are a Risk Management Specialist with expertise in portfolio risk, position sizing, and capital preservation across institutional and retail trading environments.

CAPABILITIES:
{DATA_CAPABILITY}- Available libraries: pandas, numpy, ta (for technical indicators), scipy, statsmodels.
- Use `print()` for calculated risk metrics, `show_chart()` for volatility/risk visualizations.
- Calculate ATR using: `ta.volatility.average_true_range(high, low, close, window=14)`.
- Access the last candle with `df.iloc[-1]` for current levels.

RISK ANALYSIS FRAMEWORK:
1. First, calculate current volatility (ATR) and compare to the 50-candle rolling average. If ATR > 1.5x average, flag as high volatility.
2. Determine optimal position size based on the account risk per trade (0.5--1% maximum).
3. Ensure stop-loss is placed beyond a genuine invalidation point, not an arbitrary distance.
4. Calculate exact dollar risk before evaluating potential reward.
5. Prefer limit entries -- better price execution improves risk parameters.

OUTPUT FORMAT:
- Begin with a risk assessment summary: volatility state, current ATR, suggested max lot size.
- If a trade passes all risk filters, output:
```json
{"action": "TRADE_SETUP", "symbol": "<symbol>", "direction": "BUY|SELL", "order_type": "limit|market", "entry_price": <float>, "stop_loss": <float>, "take_profit": <float>, "lot_size": <float>, "risk_amount_usd": <float>, "risk_reward": <float>, "reasoning": "<risk-focused explanation>"}
```
- Rejection reasons: NO SETUP -- <reason> where reason is one of:
  - Volatility excessive (ATR {value} vs avg {avg})
  - Risk-reward below 2.0 threshold
  - Insufficient structure for logical stop placement
  - Position size below minimum tradable lot
"""

PERSONAS["quant"] = """You are a Quantitative Strategy Developer specializing in algorithmic trading, statistical arbitrage, and data-driven market analysis.

CAPABILITIES:
{DATA_CAPABILITY}- Full Python execution environment with: pandas, numpy, ta, scipy, statsmodels (ADF test, cointegration), sklearn (regression, RandomForest), matplotlib, seaborn.
- Use `ta.momentum.rsi()`, `ta.trend.sma_indicator()`, `ta.trend.ema_indicator()`, `ta.volatility.bollinger_hband()`, `ta.volatility.average_true_range()`, `ta.momentum.stoch()`.
- Output results via `print()`, `show_chart()`, or `show_table()`.
- You MUST write a Python code block with every response to demonstrate the quantitative basis for your analysis.

WORKFLOW:
1. Accept the provided `df` and the user's query.
2. Write Python code to compute relevant statistics: rolling mean/std, correlation matrix, ADF stationarity test, volatility clustering, signal simulation.
3. If the user asks for a strategy, implement a simple backtest on the `df` using vectorized signal generation, compute Sharpe ratio, win rate, max drawdown, and profit factor.
4. Output the numerical results using `print()`, then interpret them.
5. Only recommend a trade if the simulated edge is positive and statistically meaningful.

OUTPUT FORMAT:
```json
{"action": "TRADE_SETUP", "symbol": "<symbol>", "direction": "BUY|SELL", "order_type": "market|limit|stop", "entry_price": <float>, "stop_loss": <float>, "take_profit": <float>, "lot_size": <float>, "expected_value": <float>, "sharpe_ratio": <float>, "reasoning": "<data-driven explanation>"}
```
- If data shows no edge: NO STATISTICAL EDGE -- <metric> does not support a directional bias.
"""

PERSONAS["swing_trader"] = """You are a Swing Trader specializing in multi-day to multi-week positions on higher timeframes (4H, daily, weekly). You follow trends, capture medium-term moves, and ignore intraday noise.

CAPABILITIES:
{DATA_CAPABILITY}- Available: pandas, numpy, ta (all indicators), scipy, statsmodels, matplotlib, seaborn.
- Calculate SMA/EMA crossovers, ATR for volatility-adjusted targets, and volume profile using the sandbox.
- Use `ta.trend.ema_indicator(close, window=200)` for macro trend, `ta.volatility.average_true_range(high, low, close, window=14)` for ATR.

SWING TRADING FRAMEWORK:
1. Determine the macro trend first -- look at the 100+ candle view. Is price above the 200 EMA? Are highs/lows expanding?
2. Trade only in the direction of the macro trend. Counter-trend trades require a confirmed reversal pattern (double bottom, divergence, engulfing candle at key S/R).
3. Entry: pullback to moving average (20 EMA or 50 EMA) in a trending market.
4. Stop-loss: below the most recent swing low (buys) or above the most recent swing high (sells). Minimum 1.5x ATR distance.
5. Take-profit: at next major S/R level or 3x ATR from entry. Trail stop after 1.5x ATR profit.
6. Position size: 0.5x standard (wider stop = smaller size to maintain consistent dollar risk).

OUTPUT FORMAT:
- Begin with macro trend assessment, key levels, and expected hold duration.
- If a swing setup is identified:
```json
{"action": "TRADE_SETUP", "symbol": "<symbol>", "direction": "BUY|SELL", "order_type": "market|limit", "entry_price": <float>, "stop_loss": <float>, "take_profit": <float>, "lot_size": <float>, "risk_reward": <float>, "hold_days": <int>, "reasoning": "<swing rationale>"}
```
- If no valid swing setup: NO SWING SETUP -- <reason> where reason includes the market structure assessment (ranging, no pullback, unclear trend).
"""

PERSONAS["scalper"] = """You are a Professional Scalper operating on M1 and M5 timeframes. Your edge comes from speed, precision, and discipline. You target small consistent gains with high probability setups.

CAPABILITIES:
- You have a pandas DataFrame `df` in a Python sandbox with the most recent 500+ candles.
- Available: pandas, numpy, ta (indicators), scipy, matplotlib.
- Use `ta.momentum.rsi(close, window=7)` for fast RSI, Bollinger Bands via `ta.volatility.bollinger_hband()` / `bollinger_lband()`, ATR for volatility assessment.
- You can write Python to measure recent range, momentum, and volatility.

SCALPING FRAMEWORK:
1. Assess the immediate market state: Is price ranging tightly (< 2x spread range)? Is there a momentum burst? Is price at a Bollinger Band extreme?
2. Entry triggers: breakout of a 5-bar range with volume, RSI(7) crossing 30/70 with momentum, or price rejecting a Bollinger Band.
3. Stop-loss: 0.5x ATR or 5--10 pips -- whichever is tighter. Cut immediately if wrong.
4. Take-profit: 1x to 1.5x ATR or the opposite Bollinger Band -- quick mechanical targets.
5. Market orders only. No limit or stop orders -- speed matters.
6. Maximum trade duration: 15 minutes. If price hasn't reached TP or SL within 15 candles (M1) or 5 candles (M5), consider market exit.

OUTPUT FORMAT:
- Keep analysis brief -- 2-3 sentences max. Scalpers don't read essays.
- If a scalp setup is active:
```json
{"action": "TRADE_SETUP", "symbol": "<symbol>", "direction": "BUY|SELL", "order_type": "market", "entry_price": <float>, "stop_loss": <float>, "take_profit": <float>, "lot_size": <float>, "risk_reward": <float>, "max_hold_minutes": <int>, "reasoning": "<one-line rationale>"}
```
- No scalp setup: NO SCALP SETUP -- <reason> where reason is one of:
  - Market too slow (range compact)
  - No momentum -- waiting for breakout
  - Spread too wide relative to ATR
  - All indicators neutral -- no edge in current conditions
"""

PERSONA_NAMES = {
    "technical_analyst": "Technical Analyst",
    "risk_manager": "Risk Manager",
    "quant": "Quant / Systematic",
    "swing_trader": "Swing Trader",
    "scalper": "Scalper",
}

async def _cache_market_data_internal(symbol: str, timeframe: str, data: List[dict], source: str):
    """Internal helper to cache market data in ai.py."""
    try:
        async with AsyncSessionLocal() as db:
            records = []
            for d in data:
                dt_time = d.get("time")
                if isinstance(dt_time, str):
                    try:
                        dt_time = datetime.strptime(dt_time, '%Y-%m-%d %H:%M:%S')
                    except Exception:
                        dt_time = datetime.fromisoformat(dt_time.replace('Z', '+00:00'))
                elif isinstance(dt_time, (int, float)):
                    # Handle Unix timestamp
                    dt_time = datetime.fromtimestamp(dt_time)
                
                records.append({
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "time": dt_time,
                    "open": d.get("open") or d.get("Open"),
                    "high": d.get("high") or d.get("High"),
                    "low": d.get("low") or d.get("Low"),
                    "close": d.get("close") or d.get("Close"),
                    "tick_volume": d.get("tick_volume") or d.get("Volume"),
                    "source": source
                })

            if not records:
                return

            if db.bind.dialect.name == "postgresql":
                stmt = pg_insert(MarketData).values(records).on_conflict_do_nothing()
            else:
                stmt = sqlite_insert(MarketData).values(records).on_conflict_do_nothing()
            
            await db.execute(stmt)
            await db.commit()
    except Exception as e:
                logger.warning(f"AI Cache Error: {e}")


PROMPT_REFINER_SYSTEM = """You are a query refiner for a trading AI assistant. Your ONLY job is to rewrite the user's raw trading question into a clear, specific, well-structured analysis request. 

RULES:
1. Keep the original intent intact — do NOT answer the question, just refine it.
2. Add relevant context: mention the symbol, timeframe, and specific technical elements the user hints at.
3. Be precise — convert vague language ("is it good?") into specific analysis requests ("Evaluate the risk-reward for a long entry").
4. Output ONLY the refined query text — no explanations, no JSON, no formatting.
5. Maximum 3 sentences.
6. If the query is already specific and well-structured, return it as-is with minimal changes.

EXAMPLES:
User: "is it good to buy?"
Refined: "Analyze EURUSD for a potential buy entry. Assess the current trend direction, identify key support/resistance levels, and evaluate whether the risk-reward profile supports a long position."

User: "whats the market doing"
Refined: "Provide a comprehensive market analysis of EURUSD. Describe the current price action, trend structure, volatility conditions, and any notable technical patterns."

User: "check rsi on bitcoin"
Refined: "Calculate and analyze the RSI indicator on BTCUSD. Interpret the current RSI value, identify any divergences, and assess whether the reading suggests overbought or oversold conditions."

User: "should I sell my gold position"
Refined: "Evaluate whether to close or hold a long XAUUSD position. Analyze the current trend, key support levels, and any reversal signals that would warrant selling."

User: "how's the volatility on nasdaq"
Refined: "Analyze the current volatility conditions on NASDAQ. Calculate ATR, compare it to recent averages, and assess whether the volatility environment favors range-trading or breakout strategies."
"""

REFINER_MODEL = "mistralai/mistral-7b-instruct-v0.3"  # Fast/cheap model for refinement


async def _refine_query(
    user_query: str,
    symbol: Optional[str],
    persona_name: str,
    market_context: str,
    api_key: str,
    base_url: str,
    fallback_model: str,
) -> str:
    """Refine a user's raw query into a structured analysis request using a cheap AI call.

    Tries the fast REFINER_MODEL first, falls back to the user's model if unavailable.
    Returns the original query if both attempts fail.
    """
    if not user_query or len(user_query.strip()) < 3:
        return user_query

    persona_desc = PERSONA_NAMES.get(persona_name, persona_name)
    context_summary = market_context[:300] if market_context else 'No market data loaded'

    refiner_input = f"""Symbol: {symbol or 'Unknown'}
Persona: {persona_desc}
User query: {user_query}
Market context: {context_summary}
"""

    models_to_try = [REFINER_MODEL, fallback_model]

    for model in models_to_try:
        try:
            client = AsyncOpenAI(base_url=base_url, api_key=api_key)
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": PROMPT_REFINER_SYSTEM},
                    {"role": "user", "content": refiner_input},
                ],
                temperature=0.1,
                max_tokens=300,
                timeout=10,
            )
            refined = response.choices[0].message.content
            if refined and len(refined.strip()) > 5:
                logger.info(f"[Refiner] {model}: '{user_query[:50]}...' → '{refined[:60]}...'")
                return refined.strip()
        except Exception as e:
            logger.warning(f"[Refiner] Model {model} failed: {e}")

    return user_query


@router.get("/providers", response_model=AIProvidersResponse)
async def get_providers(current_user: dict = Depends(get_current_user)):
    """Get available AI providers with live model lists (cached 24h)."""
    providers_list = []
    for key, value in PROVIDERS.items():
        api_key = _get_api_key(key, settings)
        live_models = []
        if api_key:
            live_models = await _get_live_models(key, api_key, value["base_url"], value.get("needs_nvapi_prefix", False))
        providers_list.append(
            AIProvider(
                id=key,
                name=value["name"],
                models=live_models if live_models else value["models"],
                has_key=bool(api_key),
            )
        )

    return AIProvidersResponse(providers=providers_list)


@router.post("/user-keys")
async def save_user_keys(
    keys: dict = Body(...), current_user: dict = Depends(get_current_user)
):
    user_id = current_user["user_id"]
    async with AsyncSessionLocal() as db:
        await db.execute(delete(UserApiKey).where(UserApiKey.user_id == user_id))
        for provider, api_key in keys.items():
            if provider not in PROVIDERS:
                continue
            if not api_key:
                continue
            encrypted = encrypt_api_key(api_key, settings.SECRET_KEY or settings.effective_secret_key)
            db.add(UserApiKey(user_id=user_id, provider=provider, encrypted_key=encrypted))
        await db.commit()
    return {"status": "ok"}


@router.get("/user-keys")
async def get_user_keys(current_user: dict = Depends(get_current_user)):
    user_id = current_user["user_id"]
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(UserApiKey).where(UserApiKey.user_id == user_id))
        keys = result.scalars().all()
    return {"providers": {k.provider: True for k in keys}}


@router.post("/test")
async def test_connection(
    provider: str = Body(...), model: str = Body(...), current_user: dict = Depends(get_current_user)
):
    if provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail="Invalid provider")
    api_key = _get_api_key(provider, settings)
    if not api_key:
        raise HTTPException(status_code=400, detail=f"No API key configured for {provider}")
    try:
        client = AsyncOpenAI(base_url=get_base_url(provider), api_key=api_key)
        await client.models.list()
        return {"success": True, "message": "Connection successful"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Connection failed: {str(e)}")



@router.post("/chat", response_model=ChatResponse)
@limiter.limit("10/minute")
async def chat(request: Request, chat_req: ChatRequest, current_user: dict = Depends(get_current_user)):
    if chat_req.provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail="Invalid provider")
    all_keys = await resolve_all_api_keys(chat_req.provider, settings, current_user["id"], AsyncSessionLocal)
    if not all_keys:
        raise HTTPException(status_code=400, detail=f"No API key for {chat_req.provider}")
    api_key = all_keys[0]  # Primary key for initial calls (refiner, etc.)
    provider_config = PROVIDERS[chat_req.provider]

    # ── TIMEFRAME DETECTION & DATA FETCHING ────────────────────────────────
    # Detect if user is asking for a specific timeframe (e.g., "on 5m", "for 1h")
    requested_tf = None
    if chat_req.messages:
        last_msg = chat_req.messages[-1].content
        requested_tf = _detect_timeframe(last_msg)
        if requested_tf and requested_tf != chat_req.timeframe:
            logger.info(f"[AI] Detected requested timeframe change: {chat_req.timeframe} -> {requested_tf}")
            chat_req.timeframe = requested_tf
            # Force refresh from MT5 if timeframe changed
            if chat_req.symbol:
                if not chat_req.load_market_data:
                    chat_req.load_market_data = "mt5"
                chat_req.candle_data = [] # Clear frontend data as it's likely wrong timeframe

    try:
        logger.info(f"[AI] Received - symbol: {chat_req.symbol}, timeframe: {chat_req.timeframe}, candle_data: {len(chat_req.candle_data) if chat_req.candle_data else 0}")
        
        # Fetch market data if requested
        market_context = ""
        candle_data_for_ai = []  # Store for prompt
        # Use candle data if provided directly, or if load_market_data is set with symbol
        if chat_req.candle_data or (chat_req.load_market_data and chat_req.symbol):
            # Cap at 30000 to prevent OOM (matches frontend's max fetch)
            if chat_req.candle_data and len(chat_req.candle_data) > 30000:
                chat_req.candle_data = chat_req.candle_data[-30000:]
            period = chat_req.data_period or "1mo"
            
            # USE INCOMING CANDLE DATA FROM FRONTEND (Priority)
            data_loaded = False
            candle_data_for_ai = chat_req.candle_data or []
            
            if candle_data_for_ai:
                print(f"[AI] Building market context with {len(candle_data_for_ai)} candles")
                # Use the candle data directly from frontend
                latest = candle_data_for_ai[-1]
                samples = []
                for c in candle_data_for_ai[-10:]:
                    samples.append(f"O:{c['open']:.2f} H:{c['high']:.2f} L:{c['low']:.2f} C:{c['close']:.2f}")
                
                market_context = f"""
Current market data for {chat_req.symbol or 'Unknown'} (Source: MT5):
- Latest: Open={latest['open']:.2f} High={latest['high']:.2f} Low={latest['low']:.2f} Close={latest['close']:.2f}
- Latest Time: {latest['time']}

SAMPLES (Last 10 candles): {', '.join(samples)}
"""
                data_loaded = True
                logger.info(f"[AI] Market context built - using {len(candle_data_for_ai)} candles")

            # FALLBACK TO MT5 if requested or if candle_data is missing/insufficient
            if not data_loaded and chat_req.load_market_data == "mt5" and chat_req.symbol:
                try:
                    logger.info(f"[AI] Fetching {chat_req.timeframe} data for {chat_req.symbol} from MT5...")
                    count = chat_req.candle_count or 1000
                    
                    # Increase count for smaller timeframes to support higher-TF resampling
                    if chat_req.timeframe in ['1m', '5m', '15m']:
                        count = max(count, 2000)
                    elif count < 300: 
                        count = 300
                    
                    mt5_data = await fetch_latest_candles(chat_req.symbol, count=count, timeframe=chat_req.timeframe or "1h")
                    
                    if mt5_data:
                        candle_data_for_ai = mt5_data
                        latest = mt5_data[-1]
                        samples = []
                        for c in mt5_data[-10:]:
                            samples.append(f"O:{c['open']:.2f} H:{c['high']:.2f} L:{c['low']:.2f} C:{c['close']:.2f}")
                        
                        market_context = f"""
Current market data for {chat_req.symbol} (Source: MT5 Backend):
- Timeframe: {chat_req.timeframe}
- Latest: Open={latest['open']:.2f} High={latest['high']:.2f} Low={latest['low']:.2f} Close={latest['close']:.2f}
- Latest Time: {latest['time']}

SAMPLES (Last 10 candles): {', '.join(samples)}
"""
                        data_loaded = True
                        logger.info(f"[AI] Market context built from MT5 - {len(candle_data_for_ai)} candles")
                except Exception as e:
                    logger.error(f"[AI] MT5 fallback error: {e}")
                    market_context = f"\n[Note: Could not fetch MT5 data for {chat_req.symbol}: {str(e)}]\n"

            # FALLBACK TO YAHOO if no candle data and yahoo requested
            elif not data_loaded and chat_req.load_market_data == "yahoo":
                import yfinance as yf
                try:
                    ticker = yf.Ticker(chat_req.symbol)
                    hist = ticker.history(period=period)
                    if not hist.empty:
                        latest = hist.iloc[-1]
                        hist_tail = hist.tail(10)
                        market_context = f"""
Current market data for {chat_req.symbol} (Source: Yahoo Finance):
- Latest: Open={latest["Open"]:.2}, High={latest["High"]:.2}, Low={latest["Low"]:.2}, Close={latest["Close"]:.2}
- Volume: {latest["Volume"]}

Last 10 candles:
{hist_tail.to_string()}
"""
                        # Cache Yahoo data too
                        yahoo_records = []
                        for idx, row in hist.iterrows():
                            yahoo_records.append({
                                "time": idx.strftime('%Y-%m-%d %H:%M:%S'),
                                "open": float(row['Open']),
                                "high": float(row['High']),
                                "low": float(row['Low']),
                                "close": float(row['Close']),
                                "tick_volume": int(row['Volume'])
                            })
                        await _cache_market_data_internal(chat_req.symbol, "1d", yahoo_records, "yahoo")
                except Exception as e:
                    market_context = f"\n[Note: Could not fetch data for {chat_req.symbol}: {str(e)}]\n"

        # Fetch user memory (previous conversations about same symbol)
        user_memory_context = ""
        global_memory_context = ""
        rag_context = ""

        async with AsyncSessionLocal() as db:
            try:
                # User's previous conversations
                # PRIORITY: Filter by chat_session_id if provided, else fall back to symbol-based history
                query = select(ChatMemory).where(ChatMemory.user_id == current_user["id"])
                
                if chat_req.chat_session_id:
                    query = query.where(ChatMemory.chat_session_id == chat_req.chat_session_id)
                elif chat_req.symbol:
                    query = query.where(ChatMemory.symbol == chat_req.symbol)
                else:
                    pass
                    
                result = await db.execute(
                    query.order_by(ChatMemory.created_at.desc()).limit(10)
                )
                prev_chats = result.scalars().all()

                if prev_chats:
                    recent_context = []
                    for chat in reversed(prev_chats[:5]):
                        role_label = "User" if chat.role == "user" else "AI"
                        content_preview = (
                            chat.content[:100] + "..."
                            if len(chat.content) > 100
                            else chat.content
                        )
                        recent_context.append(f"{role_label}: {content_preview}")

                    if recent_context:
                        user_memory_context = (
                            f"\n[Your recent chats about {chat_req.symbol}:\n"
                            + "\n".join(recent_context)
                            + "\n]"
                        )

                # Global memory (anonymized aggregated insights)
                global_result = await db.execute(
                    select(ChatMemory)
                    .where(
                        ChatMemory.symbol == chat_req.symbol,
                        ChatMemory.detected_setup.isnot(None),
                    )
                    .order_by(ChatMemory.created_at.desc())
                    .limit(20)
                )
                global_chats = global_result.scalars().all()

                if global_chats:
                    setups = []
                    for chat in global_chats:
                        if chat.detected_setup:
                            setups.append(
                                chat.detected_setup.get("direction", "UNKNOWN")
                            )

                    if setups:
                        buy_count = setups.count("BUY")
                        sell_count = setups.count("SELL")
                        global_memory_context = f"\n[Community insights for {chat_req.symbol}: {buy_count} BUY signals, {sell_count} SELL signals suggested recently]"
            except Exception as e:
                logger.warning(f"Memory fetch error: {e}")

        # RAG context: semantically similar past analyses with performance data
        if chat_req.symbol and any(m.role == "user" for m in chat_req.messages):
            try:
                last_user_msg = next(
                    (m.content for m in reversed(chat_req.messages) if m.role == "user"),
                    None
                )
                if last_user_msg:
                    rag_context = await build_rag_context(chat_req.symbol, last_user_msg)
            except Exception as e:
                logger.warning(f"RAG context build error: {e}")

        # Determine actual candle count for dynamic persona prompt
        if candle_data_for_ai:
            actual_count = len(candle_data_for_ai)
            data_capability_text = DATA_CAPABILITY.format(candle_count=f"{actual_count} candles")
            if actual_count < 100:
                data_capability_text += f"\nWARNING: Limited historical data ({actual_count} candles). Indicators with large windows (like SMA 200) will fail or return NaNs. However, indicators like ATR(14), RSI(14), or Bollinger Bands(20) are safe to use as long as at least 30+ candles are present.\n"
        else:
            data_capability_text = "- No real-time market data available. Do NOT write Python code blocks. Provide only text-based general analysis.\n"

        # Build messages with current conversation + memory context
        messages = []

        # Build system prompt from selected persona
        persona_key = chat_req.persona or "technical_analyst"
        persona_prompt = PERSONAS.get(persona_key, PERSONAS["technical_analyst"])

        system_parts = [persona_prompt.replace("{DATA_CAPABILITY}", data_capability_text)]

        # Common rules appended to all personas
        system_parts.append("")
        system_parts.append("""CRITICAL CODE EXECUTION RULES:
1. Write DIRECT executable Python statements — NOT a function definition. The sandbox runs your code via exec() and does NOT call any function.
2. DO NOT write "def calculate_signals(df):" — write statements that use the existing 'df' variable directly.
3. Correct: ```python
import ta
atr = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=14)
print(f"ATR(14): {atr.iloc[-1]:.2f}")
show_chart(atr.dropna().values, "ATR(14)")
```
4. Incorrect (will produce NO output): ```python
def calculate_signals(df): ...
```

- Analyze only based on the provided market data. Never use local system time.
- Never guarantee profits — always mention risk.
- NEVER use hardcoded integer indices like df.iloc[13]. Use df.iloc[-1] or df.tail(N) instead.
- After resample() or dropna(), check len(df) before accessing elements.
- Use 'ta' library: ta.volatility.average_true_range(), ta.momentum.rsi(), ta.trend.sma_indicator(), ta.trend.ema_indicator().
- Always include a ```python block that executes real calculations and prints real numbers. Show result with print() and optionally show_chart().
- If the data timeframe is 1h candles, you CANNOT resample it to 1m — that would create fake data. Only work with the timeframe actually provided.
""")

        # Add market data if available (like Streamlit - include full data samples)
        if market_context:
            system_parts.append(f"\nCurrent market data:\n{market_context}")
        
        # Add raw candle data if available (Streamlit style)
        if candle_data_for_ai:
            latest_time = candle_data_for_ai[-1].get('time', 'N/A')
            total_candles = len(candle_data_for_ai)
            samples = []
            for c in candle_data_for_ai[-5:]:  # Last 5 for quick text reference
                samples.append(f"O:{c.get('open')} H:{c.get('high')} L:{c.get('low')} C:{c.get('close')}")
            system_parts.append(f"\nLATEST_CANDLE_TIME: {latest_time}")
            system_parts.append(f"TOTAL_CANDLES_IN_DF: {total_candles}")
            system_parts.append(f"LATEST_SAMPLES: {', '.join(samples)}")
            system_parts.append("\nNote: The 'df' variable in the Python environment contains ALL these candles. Use it for your calculations.")
            system_parts.append(f"\nNote on timeframes: 'df' contains raw {chat_req.timeframe or '1m'} data. To analyze higher timeframes, resample in your Python code using pandas: df.resample('1h').agg({{'open':'first','high':'max','low':'min','close':'last'}}).dropna(). Available pandas aliases: '1min', '5min', '15min', '30min', '1h', '4h', '1D' (do NOT use '1T'/'4H'/'m' — they are removed in pandas 3). You can also compute multi-TF indicators by resampling to each TF and merging. WARNING: After resample().dropna(), check len(df) before accessing elements — resampling reduces row count significantly.")

        # Add current session context (recent conversation from database)
        if user_memory_context:
            system_parts.append(f"\n{user_memory_context}")

        # Add global insights (community data)
        if global_memory_context:
            system_parts.append(f"\n{global_memory_context}")

        # Add RAG context (semantically similar past analyses with performance data)
        if rag_context:
            system_parts.append(f"\n[RELEVANT PAST PERFORMANCE:\n{rag_context}\n]")
            system_parts.append("\nNote: The above shows past analyses similar to the current query, weighted by profit outcome and user feedback. Use this track record to inform your analysis — repeat what worked, avoid what didn't.")

        system_prompt = "\n".join(system_parts)
        messages.append({"role": "system", "content": system_prompt})

        # Add the current conversation from frontend
        # This is the MAIN conversation - previous messages in this chat
        for m in chat_req.messages:
            messages.append({"role": m.role, "content": m.content})

        # PROMPT REFINEMENT — rewrite the last user query before sending to main AI
        if chat_req.refine_prompt and messages and messages[-1].get("role") == "user":
            last_user_msg = messages[-1]["content"]
            refined = await _refine_query(
                user_query=last_user_msg,
                symbol=chat_req.symbol,
                persona_name=chat_req.persona or "technical_analyst",
                market_context=market_context,
                api_key=api_key,
                base_url=provider_config["base_url"],
                fallback_model=chat_req.model,
            )
            if refined != last_user_msg:
                messages[-1]["content"] = refined
                logger.info(f"[AI Chat] Query refined: '{last_user_msg[:50]}...' → '{refined[:60]}...'")

        logger.info(f"[AI Chat] === Starting AI Request ===")
        logger.info(f"[AI Chat] Provider: {chat_req.provider}")
        logger.info(f"[AI Chat] Model: {chat_req.model}")
        logger.info(f"[AI Chat] Base URL: {provider_config['base_url']}")
        logger.info(f"[AI Chat] API Keys configured: {len(all_keys)}")
        
        # Professional retry logic with multi-key fallback
        assistant_message = None
        reasoning_text = None
        token_usage = None
        req_elapsed_ms = None
        last_error = "Unknown error - check server logs"
        full_raw_response = None
        
        for attempt in range(MAX_RETRIES):
            # Try each key for this attempt
            for key_idx, current_key in enumerate(all_keys):
                try:
                    logger.info(f"[AI Chat] Attempt {attempt + 1}/{MAX_RETRIES}, key {key_idx + 1}/{len(all_keys)} - Making API call...")
                    
                    client = AsyncOpenAI(base_url=provider_config["base_url"], api_key=current_key)
                    import time as time_module
                    req_start = time_module.time()
                    response = await client.chat.completions.create(
                        model=chat_req.model,
                        messages=messages,
                        temperature=0.2,
                        max_tokens=32768,
                        timeout=REQUEST_TIMEOUT
                    )
                    req_elapsed_ms = int((time_module.time() - req_start) * 1000)
                    
                    # Handle both content and reasoning_content (Qwen model uses reasoning_content)
                    msg = response.choices[0].message
                    assistant_message = msg.content or msg.reasoning_content or ""
                    reasoning_text = msg.reasoning_content if hasattr(msg, 'reasoning_content') and msg.reasoning_content else None
                    
                    # Capture token usage if available
                    token_usage = None
                    token_detail = {}
                    if hasattr(response, 'usage') and response.usage:
                        token_usage = response.usage.total_tokens
                        token_detail = {
                            "prompt_tokens": response.usage.prompt_tokens or 0,
                            "completion_tokens": response.usage.completion_tokens or 0,
                            "total_tokens": token_usage,
                        }
                    
                    # Capture full raw API response
                    try:
                        full_raw_response = response.model_dump(mode='json')
                    except Exception:
                        try:
                            full_raw_response = response.dict()
                        except Exception:
                            full_raw_response = None
                    
                    logger.info(f"[AI Chat] SUCCESS - Response length: {len(assistant_message)} chars, Tokens: {token_usage}, Latency: {req_elapsed_ms}ms")
                    break
                    
                except RateLimitError as e:
                    last_error = f"Rate limit exceeded (key {key_idx + 1}): {str(e)}"
                    logger.warning(f"[AI Chat] Rate limit on key {key_idx + 1}: {str(e)}")
                    # Try next key
                    continue
                    
                except APIError as e:
                    last_error = f"API error (key {key_idx + 1}): {str(e)}"
                    logger.warning(f"[AI Chat] API error on key {key_idx + 1}: {str(e)}")
                    # Try next key
                    continue
                    
                except Exception as e:
                    last_error = f"Error (key {key_idx + 1}): {type(e).__name__}: {str(e)}"
                    logger.error(f"[AI Chat] Exception on key {key_idx + 1}: {type(e).__name__}: {str(e)}")
                    # Try next key
                    continue

            if assistant_message is not None:
                break
                
            # All keys for this attempt exhausted — wait before next attempt
            if attempt < MAX_RETRIES - 1:
                logger.info(f"[AI Chat] All {len(all_keys)} keys failed. Waiting {RETRY_DELAY}s before retry {attempt + 2}...")
                await asyncio.sleep(RETRY_DELAY)
        
        if assistant_message is None:
            logger.error(f"[AI Chat] All {MAX_RETRIES} attempts failed. Last error: {last_error}")
            raise HTTPException(
                status_code=503,
                detail="AI service temporarily unavailable. Please check your API key configuration and try again."
            )

        # Parse for trade setup or action
        detected_setup = _detect_trade_setup(assistant_message)
        detected_action = _detect_trade_action(assistant_message)
        
        # AUTOMATIC CODE EXECUTION with Multi-Attempt Self-Correction
        execution_output = None
        exec_data_preview = None
        exec_charts = None
        exec_tables = None

        # Security: skip execution if user message contains embedded code indicators
        _last_user_msg = chat_req.messages[-1].content if chat_req.messages else ""
        if re.search(r'```(?:python)?|__class__|__subclasses__|__import__|__builtins__|__bases__|__mro__|chr\s*\(\s*(?:95|0[xX]5[fF])|\\x5[fF]|getattr\s*\(', _last_user_msg, re.I):
            execution_output = "[Security: Code execution skipped — user message contains embedded code patterns. AI analysis provided as text only.]"
            python_match = None
        else:
            python_match = re.search(r"```python\s*(.*?)(?:```|$)", assistant_message, re.S)

        MAX_EXEC_RETRIES = 2
        exec_attempt = 0
        
        while python_match and exec_attempt <= MAX_EXEC_RETRIES:
            code = python_match.group(1)
            logger.info(f"[AI Chat] Executing Python code (attempt {exec_attempt + 1})...")
            
            exec_res = await run_python_code(code, candle_data_for_ai, chat_req.symbol, user_id=current_user["id"])
            
            if exec_res.get("success"):
                execution_output = exec_res.get("output")
                exec_data_preview = exec_res.get("data_preview")
                exec_charts = exec_res.get("charts")
                exec_tables = exec_res.get("tables")
                logger.info(f"[AI Chat] Code execution successful.")
                break
            else:
                error_msg = exec_res.get("error")
                exec_attempt += 1
                
                if exec_attempt > MAX_EXEC_RETRIES:
                    logger.error(f"[AI Chat] Code execution failed permanently: {error_msg}")
                    break
                
                logger.warning(f"[AI Chat] Code attempt {exec_attempt} failed: {error_msg}. Retrying self-correction...")
                
                try:
                    # Provide specific guidance for common errors like IndexError
                    hint = ""
                    if "IndexError" in error_msg:
                        hint = " (HINT: You likely sliced the DataFrame too small before calculating an indicator like ATR, RSI, or SMA. Ensure the DataFrame has enough rows—at least 50 to 200—before passing it to 'ta' functions.)"
                    elif "NaN" in error_msg or "NoneType" in error_msg:
                        hint = " (HINT: Check for NaNs produced by indicators and use .dropna() or handle them before further calculations.)"
                    
                    # Ask the AI to fix its own code
                    correction_messages = messages + [
                        {"role": "assistant", "content": assistant_message},
                        {"role": "user", "content": f"The Python code you provided failed with this error: {error_msg}{hint}. Please provide a FIXED version of the code block wrapped in ```python ... ```."}
                    ]
                    
                    correction_client = AsyncOpenAI(base_url=provider_config["base_url"], api_key=all_keys[0])
                    response = await correction_client.chat.completions.create(
                        model=chat_req.model,
                        messages=correction_messages,
                        temperature=0.1,
                        max_tokens=8192
                    )
                    
                    msg = response.choices[0].message
                    assistant_message = msg.content or msg.reasoning_content or ""
                    # Look for the new code block in the updated message
                    python_match = re.search(r"```python\s*(.*?)(?:```|$)", assistant_message, re.S)
                except Exception as e:
                    logger.error(f"[AI Chat] Self-correction request failed: {str(e)}")
                    break

        # Combine data previews
        final_data_preview = exec_data_preview or _detect_data_preview(assistant_message)


        # Save to database for memory
        saved_chat_memory_id = None
        async with AsyncSessionLocal() as db:
            try:
                user_msg = ChatMemory(
                    user_id=current_user["id"], 
                    symbol=chat_req.symbol,
                    chat_session_id=chat_req.chat_session_id, # LINK TO SESSION
                    role="user", 
                    content=chat_req.messages[-1].content if chat_req.messages else "",
                    provider=chat_req.provider, 
                    model=chat_req.model,
                )
                db.add(user_msg)
                await db.commit()
                await db.refresh(user_msg)

                assistant_msg = ChatMemory(
                    user_id=current_user["id"], 
                    symbol=chat_req.symbol,
                    chat_session_id=chat_req.chat_session_id, # LINK TO SESSION
                    role="assistant", 
                    content=assistant_message,
                    reasoning=reasoning_text,
                    raw_thinking=full_raw_response,
                    provider=chat_req.provider, 
                    model=chat_req.model,
                    tokens_used=token_usage, 
                    latency_ms=req_elapsed_ms,
                    detected_setup=detected_setup, 
                    detected_action=detected_action,
                )
                db.add(assistant_msg)
                await db.commit()
                await db.refresh(assistant_msg)
                saved_chat_memory_id = assistant_msg.id

                # Fire-and-forget: generate and store embedding for RAG
                try:
                    _spawn_background_task(
                        generate_embedding(saved_chat_memory_id, assistant_message)
                    )
                except Exception:
                    pass

                if chat_req.symbol and detected_setup:
                    result = await db.execute(select(GlobalInsights).where(GlobalInsights.symbol == chat_req.symbol))
                    insight = result.scalar_one_or_none()
                    if insight:
                        insight.total_analyzed += 1
                        if detected_setup.get("direction") == "BUY": insight.buy_signals += 1
                        else: insight.sell_signals += 1
                        insight.last_updated = datetime.now(timezone.utc)
                    else:
                        insight = GlobalInsights(symbol=chat_req.symbol, total_analyzed=1,
                            buy_signals=1 if detected_setup.get("direction") == "BUY" else 0,
                            sell_signals=1 if detected_setup.get("direction") == "SELL" else 0)
                        db.add(insight)
                    await db.commit()

                usage_result = await db.execute(select(ModelUsage).where(
                    ModelUsage.provider == chat_req.provider, ModelUsage.model == chat_req.model,
                    ModelUsage.user_id == current_user["id"]))
                usage = usage_result.scalar_one_or_none()
                cost = 0.0
                if token_detail:
                    from ..core.providers import estimate_cost
                    cost = estimate_cost(
                        token_detail["prompt_tokens"], token_detail["completion_tokens"],
                        chat_req.provider, chat_req.model,
                    )
                if usage:
                    usage.total_requests += 1
                    usage.total_tokens += token_detail.get("total_tokens", 0)
                    usage.total_cost += cost
                    usage.last_used = datetime.now(timezone.utc)
                else:
                    usage = ModelUsage(provider=chat_req.provider, model=chat_req.model,
                        user_id=current_user["id"], total_requests=1,
                        total_tokens=token_detail.get("total_tokens", 0),
                        total_cost=cost)
                    db.add(usage)
                await db.commit()
            except Exception as e:
                logger.warning(f"Failed to save chat memory: {e}")

        return ChatResponse(
            message=assistant_message,
            detected_setup=detected_setup,
            detected_action=detected_action,
            data_preview=final_data_preview,
            detected_chart=_detect_chart(assistant_message),
            execution_output=execution_output,
            execution_charts=exec_charts,
            execution_tables=exec_tables,
            chat_memory_id=saved_chat_memory_id,
            chat_session_id=chat_req.chat_session_id, # ECHO BACK
            rag_context=rag_context if chat_req.debug_rag else None,
        )




    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except Exception as e:
        logger.error(f"[AI Chat] Unhandled error: {str(e)}\n{traceback.format_exc()}")
        raise HTTPException(
            status_code=500,
            detail=f"AI service error: {str(e)}"
        )


def _detect_trade_action(text: str) -> Optional[dict]:
    """Detect TRADE_ACTION JSON from AI response."""
    json_pattern = r"```json\s*(.*?)\s*```"
    blocks = re.findall(json_pattern, text, re.S | re.I)

    for block in blocks:
        try:
            data = json.loads(block)
            if isinstance(data, dict) and data.get("action") in (
                "CLOSE_POSITION",
                "MODIFY_SL",
                "MODIFY_TP",
                "MODIFY_SLTP",
                "ADD_TO_POSITION",
            ):
                return data
        except json.JSONDecodeError:
            pass

    return None


def _detect_data_preview(text: str) -> Optional[str]:
    """Detect data preview (DataFrame tail) in AI response."""
    if "DataFrame shape:" in text and "Last" in text and "rows:" in text:
        # Extract from "DataFrame shape:" to the end of the table
        match = re.search(r"(DataFrame shape:.*?\n.*?(?:\n\s*\d+.*)+)", text, re.S)
        if match:
            return match.group(1).strip()
    
    # Also look for any table-like structure if it's explicitly marked
    if "DATA_PREVIEW:" in text:
        parts = text.split("DATA_PREVIEW:")
        if len(parts) > 1:
            return parts[1].strip().split("\n\n")[0]
            
    return None


def _detect_chart(text: str) -> Optional[dict]:
    """Detect SHOW_CHART JSON from AI response."""
    json_pattern = r"```json\s*(.*?)\s*```"
    blocks = re.findall(json_pattern, text, re.S | re.I)

    for block in blocks:
        try:
            data = json.loads(block)
            if isinstance(data, dict) and data.get("action") == "SHOW_CHART":
                return data
        except json.JSONDecodeError:
            pass

    return None




# Memory endpoints
@router.get("/memory", response_model=MemoryResponse)
async def get_memory(skip: int = 0, limit: int = 20, current_user: dict = Depends(get_current_user)):
    """Get memory/insights."""
    limit = min(limit, 100)
    async with AsyncSessionLocal() as db:
        try:
            # Get recent conversations
            result = await db.execute(
                select(ChatMemory)
                .where(ChatMemory.user_id == current_user["id"])
                .order_by(ChatMemory.created_at.desc())
                .offset(skip)
                .limit(limit)
            )
            conversations = result.scalars().all()

            conv_list = []
            for c in conversations:
                conv_list.append(
                    {
                        "id": c.id,
                        "symbol": c.symbol,
                        "role": c.role,
                        "content": c.content[:200] + "..."
                        if len(c.content) > 200
                        else c.content,
                        "detected_setup": c.detected_setup,
                        "created_at": c.created_at.isoformat()
                        if c.created_at
                        else None,
                    }
                )

            # Get stats
            total_result = await db.execute(
                select(func.count(ChatMemory.id)).where(
                    ChatMemory.user_id == current_user["id"]
                )
            )
            total_conversations = total_result.scalar() or 0

            # Get preferences
            pref_result = await db.execute(
                select(UserPreferences).where(
                    UserPreferences.user_id == current_user["id"]
                )
            )
            pref = pref_result.scalar_one_or_none()

            prefs_dict = {}
            if pref:
                prefs_dict = {
                    "favorite_symbols": json.loads(pref.favorite_symbols)
                    if pref.favorite_symbols
                    else [],
                    "default_provider": pref.default_provider,
                    "default_model": pref.default_model,
                    "default_data_source": pref.default_data_source,
                    "default_period": pref.default_period,
                }

            return MemoryResponse(
                conversations=conv_list,
                insights=[],
                preferences=prefs_dict,
                stats=MemoryStats(
                    total_conversations=total_conversations,
                    total_trades_suggested=0,
                    successful_trades=0,
                    failed_trades=0,
                    win_rate=0.0,
                ),
            )
        except Exception as e:
            logger.warning(f"Memory error: {e}")
        return MemoryResponse(
            conversations=[],
            insights=[],
            preferences={},
            stats=MemoryStats(
                total_conversations=0,
                total_trades_suggested=0,
                successful_trades=0,
                failed_trades=0,
                win_rate=0.0,
            ),
        )


@router.post("/feedback")
async def save_feedback(
    feedback: FeedbackRequest, current_user: dict = Depends(get_current_user)
):
    """Save user feedback."""
    async with AsyncSessionLocal() as db:
        try:
            fb = UserFeedback(
                user_id=current_user["id"],
                is_helpful=feedback.rating >= 3,
                notes=feedback.feedback_text,
            )
            db.add(fb)
            await db.commit()
            return {"success": True, "message": "Feedback saved"}
        except Exception as e:
            return {"success": False, "message": str(e)}
