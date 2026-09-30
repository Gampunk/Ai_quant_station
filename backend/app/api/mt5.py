"""
Market data, account and history routes. Everything goes through the MT5 connector.

Reading needs any logged-in user. Initializing the terminal needs a trading role.
An earlier version accepted the shared connector token here, which identifies no
one, and fell back to the Windows-only MetaTrader5 package in eleven places.
"""
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import get_db
from ..core.mt5_connector import ConnectorError, connector_client
from ..core.broker_clock import broker_clock
from ..core.security import get_current_user, require_trader
from ..models.market_data import MarketData
from ..models.schemas import (
    AccountInfo, DataResponse, HistoryResponse, MT5Symbol, MT5SymbolsResponse,
    OHLCData, Position, PositionsResponse, Trade,
)

router = APIRouter(prefix="/mt5", tags=["MT5"])


def _http_error(exc: ConnectorError) -> HTTPException:
    # Refusals from the connector keep their status; anything else is a bad gateway.
    status = exc.status_code if exc.status_code < 500 else 502
    return HTTPException(status_code=status, detail=f"MT5 connector: {exc.detail}")


async def _cache_market_data(db: AsyncSession, symbol: str, timeframe: str, data: List[OHLCData], source: str = "mt5"):
    """Helper to cache market data in SQLite using INSERT OR IGNORE."""
    try:
        # Prepare records
        records = []
        for d in data:
            # Convert string time or datetime to datetime object for DB
            if isinstance(d.time, str):
                try:
                    dt_time = datetime.strptime(d.time, '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    # Try ISO format if standard format fails
                    dt_time = datetime.fromisoformat(d.time.replace('Z', '+00:00'))
            elif isinstance(d.time, (int, float)):
                # Handle Unix timestamp
                dt_time = datetime.fromtimestamp(d.time)
            else:
                dt_time = d.time
            
            records.append({
                "symbol": symbol,
                "timeframe": timeframe,
                "time": dt_time,
                "open": d.open,
                "high": d.high,
                "low": d.low,
                "close": d.close,
                "tick_volume": d.tick_volume,
                "source": source
            })

        if not records:
            return

        # Check dialect for ON CONFLICT support
        if db.bind.dialect.name == "postgresql":
            stmt = pg_insert(MarketData).values(records).on_conflict_do_nothing()
        else:
            stmt = sqlite_insert(MarketData).values(records).on_conflict_do_nothing()
        
        await db.execute(stmt)
        await db.commit()
    except Exception as e:
        import traceback
        print(f"Error caching market data: {e}\n{traceback.format_exc()}")


@router.get("/health")
async def health_check(current_user: dict = Depends(get_current_user)):
    try:
        result = await connector_client.health()
    except ConnectorError as exc:
        raise HTTPException(status_code=503, detail=f"Connector unavailable: {exc.detail}")
    # The connector reports "mt5_connected". This route used to read a key the
    # connector never sends, so it always said the terminal was not initialized.
    await connector_client.refresh_clock()
    return {"status": "running", "source": "connector", "mt5_initialized": bool(result.get("mt5_connected")),
            "broker_clock": broker_clock.status()}


@router.post("/initialize")
async def initialize_mt5(current_user: dict = Depends(require_trader)):
    """Initialize the terminal. Which terminal is chosen by MT5_TERMINAL_PATH on the connector."""
    try:
        result = await connector_client.initialize()
    except ConnectorError as exc:
        raise _http_error(exc)
    account = result.get("account") or {}
    return {"success": True, "message": "MT5 initialized successfully",
            "account": account.get("login"), "server": account.get("server")}


@router.get("/symbols/all")
async def get_all_symbols(current_user: dict = Depends(get_current_user)):
    """Get all available symbols from broker."""
    try:
        res = await connector_client.get_symbols()
    except ConnectorError as exc:
        raise _http_error(exc)
    symbols = [MT5Symbol(**s) for s in res.get("symbols", [])]
    return MT5SymbolsResponse(count=len(symbols), symbols=symbols)


@router.get("/symbols")
async def get_symbols_jwt(current_user: dict = Depends(get_current_user)):
    """Symbols visible in the terminal, in the shape the symbol pickers use."""
    try:
        res = await connector_client.get_symbols()
    except ConnectorError as exc:
        return {"success": False, "symbols": [], "error": exc.detail}
    symbols, seen = [], set()
    for s in res.get("symbols", []):
        name = s.get("name")
        if name and name not in seen and s.get("visible", True):
            seen.add(name)
            symbols.append({"symbol": name, "name": name, "type": "forex"})
    return {"success": True, "symbols": symbols}


@router.get("/symbol/{symbol}")
async def get_symbol_info(symbol: str, current_user: dict = Depends(get_current_user)):
    """Get detailed symbol information."""
    try:
        return MT5Symbol(**await connector_client.get_symbol(symbol))
    except ConnectorError as exc:
        raise _http_error(exc)


@router.post("/data/fetch")
async def fetch_data(
    symbol: str,
    timeframe: str,
    start_date: str,
    end_date: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Fetch OHLC data for a date range."""
    try:
        res = await connector_client.get_range(symbol, timeframe, start_date, end_date)
    except ConnectorError as exc:
        raise _http_error(exc)
    ohlc_data = [OHLCData(**item) for item in res.get("data", [])]
    await _cache_market_data(db, symbol, timeframe, ohlc_data, source="mt5_connector")
    return DataResponse(success=True, symbol=symbol, timeframe=timeframe, rows=len(ohlc_data), data=ohlc_data)


class DataFetchRequest(BaseModel):
    symbol: str
    timeframe: str = "1h"
    count: int = 1000


@router.post("/data/latest")
async def fetch_latest(
    request: DataFetchRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Fetch the latest N candles."""
    try:
        res = await connector_client.get_latest_data(request.symbol, request.timeframe, request.count)
    except ConnectorError as exc:
        raise _http_error(exc)
    ohlc_data = [OHLCData(**item) for item in res.get("data", [])]
    await _cache_market_data(db, request.symbol, request.timeframe, ohlc_data, source="mt5_connector")
    return DataResponse(success=True, symbol=request.symbol, timeframe=request.timeframe,
                        rows=len(ohlc_data), data=ohlc_data)


@router.get("/account")
async def get_account_info(current_user: dict = Depends(get_current_user)):
    """Get account information."""
    try:
        return AccountInfo(**await connector_client.get_account())
    except ConnectorError as exc:
        raise _http_error(exc)


@router.get("/positions")
async def get_positions(current_user: dict = Depends(get_current_user)):
    """Get open positions."""
    try:
        res = await connector_client.get_positions()
    except ConnectorError as exc:
        raise _http_error(exc)
    positions = [Position(**p) for p in res.get("positions", [])]
    return PositionsResponse(
        success=True,
        balance=res.get("balance", 0.0), equity=res.get("equity", 0.0), margin=res.get("margin", 0.0),
        free_margin=res.get("free_margin", 0.0), margin_level=res.get("margin_level", 0.0),
        open_count=len(positions), total_profit=res.get("total_profit", 0.0), positions=positions,
    )


@router.get("/history")
async def get_history(hours: int = 0, current_user: dict = Depends(get_current_user)):
    """Get trade history."""
    try:
        res = await connector_client.get_history(hours=hours)
    except ConnectorError as exc:
        raise _http_error(exc)
    deals = [Trade(**t) for t in res.get("deals", [])]
    return HistoryResponse(success=True, count=len(deals), deals=deals)
