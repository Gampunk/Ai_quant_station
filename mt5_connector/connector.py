# MT5 Connector Service
# This runs on Windows server with MT5 terminal installed
# Works like your existing mt5_data_server.py

import sys
import os
import secrets
import socket
from datetime import datetime, timedelta, timezone
from typing import Optional
import MetaTrader5 as mt5

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

# Fix Windows console encoding for emoji support
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# The docs page is an interactive order form. It is off unless explicitly enabled.
ENABLE_DOCS = os.getenv("MT5_ENABLE_DOCS", "").strip().lower() in ("1", "true", "yes")

app = FastAPI(
    title="MT5 Connector Service",
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url="/redoc" if ENABLE_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
)

CONNECTOR_API_TOKEN = os.getenv("MT5_API_TOKEN", "")

# Listen on this machine only unless told otherwise. A remote deployment must set
# MT5_CONNECTOR_HOST explicitly, for example to a private tunnel address.
BIND_HOST = os.getenv("MT5_CONNECTOR_HOST", "127.0.0.1")

# Without a token the connector refuses every request. Only set this for an
# instance nothing else can reach, such as the fake terminal used in tests.
ALLOW_NO_TOKEN = os.getenv("MT5_ALLOW_NO_TOKEN", "").strip().lower() in ("1", "true", "yes")

# Refuse order, close and modify unless the terminal is logged into a demo account.
# Anything other than an explicit false keeps the guard on.
REQUIRE_DEMO = os.getenv("MT5_REQUIRE_DEMO", "true").strip().lower() not in ("0", "false", "no")


def _max_volume() -> float:
    try:
        value = float(os.getenv("MT5_MAX_VOLUME", "1.0"))
    except ValueError:
        value = 1.0
    return value if value > 0 else 1.0


# The largest order this connector sends, in lots, whatever the backend asks for.
# A last lock behind the backend's risk checks. Raise it here, on this machine, only.
MAX_VOLUME = _max_volume()


def min_stop_distance(symbol_info) -> float:
    """The closest a stop or target may sit to the price: the broker's stops level, at least 10 points."""
    return max(symbol_info.trade_stops_level, 10) * symbol_info.point


def check_stops(action: str, price: float, sl, tp, min_dist: float, digits: int) -> None:
    """Refuse a stop or target on the wrong side of the price, or closer than the broker allows.

    An earlier version moved such a stop to the nearest allowed level without saying so,
    which changed the trade's risk behind the caller's back.
    """
    buy = action.startswith("BUY")
    gap = round(min_dist, digits)
    if sl is not None:
        if buy and sl > price - min_dist:
            raise HTTPException(status_code=400, detail=f"Stop loss {sl} must be at least {gap} below the price {price}")
        if not buy and sl < price + min_dist:
            raise HTTPException(status_code=400, detail=f"Stop loss {sl} must be at least {gap} above the price {price}")
    if tp is not None:
        if buy and tp < price + min_dist:
            raise HTTPException(status_code=400, detail=f"Take profit {tp} must be at least {gap} above the price {price}")
        if not buy and tp > price - min_dist:
            raise HTTPException(status_code=400, detail=f"Take profit {tp} must be at least {gap} below the price {price}")


def require_demo_account():
    """Raise unless trading is allowed on the connected account."""
    if not REQUIRE_DEMO:
        return
    acc = mt5.account_info()
    if acc is None:
        raise HTTPException(status_code=503, detail="Cannot read account info, refusing to trade")
    if acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        raise HTTPException(
            status_code=403,
            detail=f"Account {acc.login} is not a demo account. "
                   "Set MT5_REQUIRE_DEMO=false only for a deliberate live deployment.",
        )


def trade_mode_label(acc) -> str:
    return {mt5.ACCOUNT_TRADE_MODE_DEMO: "demo", mt5.ACCOUNT_TRADE_MODE_CONTEST: "contest",
            mt5.ACCOUNT_TRADE_MODE_REAL: "real"}.get(acc.trade_mode, "unknown")

def verify_auth(authorization: str | None = Header(default=None)):
    """Check the Authorization header. Used as a dependency on every endpoint.

    The header is the only accepted place for the token. An earlier version
    declared `authorization` as a plain argument, which FastAPI reads from the
    query string, so the header every client sends was ignored.
    """
    if not CONNECTOR_API_TOKEN:
        if ALLOW_NO_TOKEN:
            return True
        raise HTTPException(
            status_code=503,
            detail="Connector has no MT5_API_TOKEN set. Set one, or set "
                   "MT5_ALLOW_NO_TOKEN=true for an isolated local instance.",
        )
    supplied = (authorization or "")
    if supplied.lower().startswith("bearer "):
        supplied = supplied[7:]
    supplied = supplied.strip()
    if not supplied or not secrets.compare_digest(supplied, CONNECTOR_API_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid API token")
    return True

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:3000").split(","),
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# MT5 Connection State
mt5_initialized = False
last_error = None
terminal_path = None


def server_time(ts: int) -> str:
    """Format an MT5 timestamp.

    MT5 stamps positions, deals and candles in the broker's server time, encoded
    as if it were UTC. Formatting it as UTC shows that server time unchanged on
    any machine, whatever its own time zone. The backend converts to real UTC
    with MT5_BROKER_UTC_OFFSET where it needs to.
    """
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def as_utc(value: datetime) -> datetime:
    """MT5 reads a naive datetime in the machine's local time zone. Pin it to UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def get_network_ip():
    """Detect the primary network IP of this machine."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP


def get_startup_config():
    """Get configuration from env vars or interactive input."""
    port_env = os.getenv("MT5_CONNECTOR_PORT")
    terminal_env = os.getenv("MT5_TERMINAL_PATH")
    
    if port_env:
        return int(port_env), terminal_env
    
    print("\n" + "=" * 60)
    print("      MT5 Connector Service - Configuration Startup")
    print("=" * 60 + "\n")
    
    default_port = os.getenv("MT5_CONNECTOR_PORT", "5001")
    port_input = input(f"Enter Port [Default {default_port}]: ").strip() or default_port
    port = int(port_input)
    
    print("\nMultiple MT5 Instances Detected?")
    print("   (Leave empty to use your default/active MT5)")
    terminal_path = input("Enter MT5 Terminal Path (e.g. C:\\...\\terminal64.exe): ").strip() or None
    
    return port, terminal_path


PORT, STARTUP_PATH = get_startup_config()
SERVER_IP = get_network_ip()


class OrderRequest(BaseModel):
    symbol: str
    action: str
    volume: float
    price: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    comment: str = "[IMPULSE_CONNECTOR]"
    magic: int = 0


class CloseRequest(BaseModel):
    ticket: int
    volume: Optional[float] = None


class ModifyRequest(BaseModel):
    ticket: int
    sl: Optional[float] = None
    tp: Optional[float] = None


@app.get("/")
async def root(_auth: bool = Depends(verify_auth)):
    return {
        "service": "MT5 Connector",
        "version": "2.0.0",
        "mt5_initialized": mt5_initialized,
        "last_error": last_error,
        "terminal_path": terminal_path,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


@app.get("/health")
async def health(_auth: bool = Depends(verify_auth)):
    return {
        "status": "healthy" if mt5_initialized else "not_initialized",
        "mt5_connected": mt5_initialized,
        "terminal_path": terminal_path
    }


@app.post("/initialize")
async def initialize_mt5(_auth: bool = Depends(verify_auth)):
    """Initialize MT5 connection.

    The terminal is the one set by MT5_TERMINAL_PATH on this machine. Callers
    cannot choose it: MT5 starts whatever program sits at the path it is given.
    """
    global mt5_initialized, last_error, terminal_path
    
    try:
        path = STARTUP_PATH
        
        if path:
            if not mt5.initialize(path=path):
                last_error = mt5.last_error()
                raise HTTPException(status_code=500, detail=f"MT5 init failed: {last_error}")
        else:
            if not mt5.initialize():
                last_error = mt5.last_error()
                raise HTTPException(status_code=500, detail=f"MT5 init failed: {last_error}")
        
        mt5_initialized = True
        terminal_path = path
        account = mt5.account_info()
        
        return {
            "success": True,
            "message": "MT5 initialized successfully",
            "account": {
                "login": account.login,
                "server": account.server,
                "balance": account.balance,
                "equity": account.equity,
                "trade_mode": trade_mode_label(account),
            },
            "require_demo": REQUIRE_DEMO,
        }
    except Exception as e:
        last_error = str(e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/shutdown")
async def shutdown_mt5(_auth: bool = Depends(verify_auth)):
    """Shutdown MT5 connection."""
    global mt5_initialized
    mt5.shutdown()
    mt5_initialized = False
    return {"success": True, "message": "MT5 shutdown"}


@app.get("/account")
async def get_account(_auth: bool = Depends(verify_auth)):
    """Get account info."""
    if not mt5_initialized:
        raise HTTPException(status_code=400, detail="MT5 not initialized")
    
    acc = mt5.account_info()
    if acc is None:
        raise HTTPException(status_code=500, detail="Cannot get account info")
    
    margin_level = (acc.equity / acc.margin * 100) if acc.margin > 0 else 0
    
    return {
        "login": acc.login,
        "server": acc.server,
        "name": acc.name,
        "balance": acc.balance,
        "equity": acc.equity,
        "margin": acc.margin,
        "free_margin": acc.margin_free,
        "margin_level": round(margin_level, 2),
        "profit": acc.profit,
        "currency": acc.currency,
        "leverage": acc.leverage
    }


@app.get("/symbols")
async def get_symbols(_auth: bool = Depends(verify_auth)):
    """Get all available symbols."""
    if not mt5_initialized:
        raise HTTPException(status_code=400, detail="MT5 not initialized")
    
    symbols = mt5.symbols_get()
    if not symbols:
        return {"count": 0, "symbols": []}
    
    result = []
    for s in symbols:
        tick = mt5.symbol_info_tick(s.name)
        result.append({
            "name": s.name,
            "description": s.description,
            "visible": s.visible,
            "ask": tick.ask if tick else None,
            "bid": tick.bid if tick else None,
            "point": s.point,
            "digits": s.digits,
            "volume_min": s.volume_min,
            "volume_max": s.volume_max
        })
    
    return {"count": len(result), "symbols": result}


@app.get("/symbol/{symbol}")
async def get_symbol(symbol: str, _auth: bool = Depends(verify_auth)):
    """Get specific symbol info."""
    if not mt5_initialized:
        raise HTTPException(status_code=400, detail="MT5 not initialized")
    
    info = mt5.symbol_info(symbol)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Symbol {symbol} not found")
    
    tick = mt5.symbol_info_tick(symbol)
    
    return {
        "name": info.name,
        "description": info.description,
        "visible": info.visible,
        "ask": tick.ask if tick else None,
        "bid": tick.bid if tick else None,
        "point": info.point,
        "digits": info.digits,
        "volume_min": info.volume_min,
        "volume_max": info.volume_max,
        "volume_step": info.volume_step,
        "trade_stops_level": info.trade_stops_level,
        "min_stop_distance": min_stop_distance(info),
        "trade_contract_size": info.trade_contract_size,
        # Money per tick per lot, in the account currency. Position sizing needs both.
        "trade_tick_size": info.trade_tick_size,
        "trade_tick_value": info.trade_tick_value,
        "max_volume": MAX_VOLUME,
    }


@app.post("/order")
async def place_order(order: OrderRequest, _auth: bool = Depends(verify_auth)):
    """Place an order."""
    if not mt5_initialized:
        raise HTTPException(status_code=400, detail="MT5 not initialized")
    require_demo_account()
    
    if not mt5.symbol_select(order.symbol, True):
        raise HTTPException(status_code=404, detail=f"Symbol {order.symbol} not found")
    
    symbol_info = mt5.symbol_info(order.symbol)
    tick = mt5.symbol_info_tick(order.symbol)
    
    if symbol_info is None or tick is None:
        raise HTTPException(status_code=400, detail="Broker data unavailable")
    
    if order.volume < symbol_info.volume_min:
        raise HTTPException(status_code=400, detail=f"Volume below minimum {symbol_info.volume_min}")
    
    volume = round(order.volume / symbol_info.volume_step) * symbol_info.volume_step
    volume = round(volume, 2)
    if volume > symbol_info.volume_max:
        raise HTTPException(status_code=400, detail=f"Volume {volume} above the broker's maximum {symbol_info.volume_max}")
    if volume > MAX_VOLUME:
        raise HTTPException(status_code=403, detail=f"Volume {volume} above this connector's cap of {MAX_VOLUME} lots (MT5_MAX_VOLUME)")
    
    digits = symbol_info.digits
    point = symbol_info.point
    
    action_map = {
        "BUY": mt5.ORDER_TYPE_BUY,
        "SELL": mt5.ORDER_TYPE_SELL,
        "BUY_LIMIT": mt5.ORDER_TYPE_BUY_LIMIT,
        "SELL_LIMIT": mt5.ORDER_TYPE_SELL_LIMIT,
        "BUY_STOP": mt5.ORDER_TYPE_BUY_STOP,
        "SELL_STOP": mt5.ORDER_TYPE_SELL_STOP,
    }
    
    if order.action not in action_map:
        raise HTTPException(status_code=400, detail=f"Invalid action: {order.action}")
    
    order_type = action_map[order.action]
    is_pending = order.action in ("BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP")
    
    if is_pending and order.price is None:
        raise HTTPException(status_code=400, detail="Price required for pending orders")
    
    if is_pending:
        price = order.price
    elif order.action == "BUY":
        price = tick.ask
    else:
        price = tick.bid
    
    if price is None:
        raise HTTPException(status_code=500, detail="Cannot get price")
    
    price = round(price, digits)
    
    sl = round(order.sl, digits) if order.sl is not None else None
    tp = round(order.tp, digits) if order.tp is not None else None
    check_stops(order.action, price, sl, tp, min_stop_distance(symbol_info), digits)
    
    filling_mode = symbol_info.filling_mode
    if filling_mode & 1:
        type_filling = mt5.ORDER_FILLING_FOK
    elif filling_mode & 2:
        type_filling = mt5.ORDER_FILLING_IOC
    else:
        type_filling = mt5.ORDER_FILLING_RETURN
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL if not is_pending else mt5.TRADE_ACTION_PENDING,
        "symbol": order.symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "deviation": 20,
        "magic": order.magic,
        "comment": order.comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": type_filling,
    }
    
    if sl is not None:
        request["sl"] = sl
    if tp is not None:
        request["tp"] = tp
    
    result = mt5.order_send(request)
    
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        raise HTTPException(status_code=400, detail=f"Order failed: {result.comment if result else 'Unknown'}")
    
    # result.price is what the broker filled at. `price` above was only the quote
    # we saw beforehand, and the two differ by the slippage on the fill.
    filled_price = result.price if getattr(result, "price", 0) else price
    return {
        "success": True,
        "ticket": result.order,
        "symbol": order.symbol,
        "volume": result.volume if getattr(result, "volume", 0) else volume,
        "price": filled_price,
        "requested_price": price,
        "sl": sl,
        "tp": tp,
        "comment": result.comment,
        "position": result.order
    }


@app.post("/close")
async def close_position(close_req: CloseRequest, _auth: bool = Depends(verify_auth)):
    """Close a position."""
    if not mt5_initialized:
        raise HTTPException(status_code=400, detail="MT5 not initialized")
    require_demo_account()
    
    positions = mt5.positions_get(ticket=close_req.ticket)
    if positions is None or len(positions) == 0:
        raise HTTPException(status_code=404, detail=f"Position {close_req.ticket} not found")
    
    position = positions[0]
    close_volume = close_req.volume if close_req.volume else position.volume
    
    if close_volume > position.volume:
        raise HTTPException(status_code=400, detail="Close volume exceeds position")
    
    if position.type == mt5.POSITION_TYPE_BUY:
        price = mt5.symbol_info_tick(position.symbol).bid
        order_type = mt5.ORDER_TYPE_SELL
    else:
        price = mt5.symbol_info_tick(position.symbol).ask
        order_type = mt5.ORDER_TYPE_BUY
    
    symbol_info = mt5.symbol_info(position.symbol)
    filling_mode = symbol_info.filling_mode if symbol_info else 2
    
    if filling_mode & 1:
        type_filling = mt5.ORDER_FILLING_FOK
    elif filling_mode & 2:
        type_filling = mt5.ORDER_FILLING_IOC
    else:
        type_filling = mt5.ORDER_FILLING_RETURN
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": position.symbol,
        "volume": close_volume,
        "type": order_type,
        "position": close_req.ticket,
        "price": price,
        "deviation": 20,
        "magic": 0,
        "comment": "[IMPULSE_CONNECTOR]",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": type_filling,
    }
    
    result = mt5.order_send(request)
    
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        raise HTTPException(status_code=400, detail=f"Close failed: {result.comment if result else 'Unknown'}")
    
    filled_price = result.price if getattr(result, "price", 0) else price
    return {
        "success": True,
        "ticket": close_req.ticket,
        "closed_volume": close_volume,
        "close_price": filled_price,
        "requested_price": price,
        "comment": result.comment
    }


@app.post("/modify")
async def modify_position(mod_req: ModifyRequest, _auth: bool = Depends(verify_auth)):
    """Modify SL/TP of a position."""
    if not mt5_initialized:
        raise HTTPException(status_code=400, detail="MT5 not initialized")
    require_demo_account()
    
    positions = mt5.positions_get(ticket=mod_req.ticket)
    if positions is None or len(positions) == 0:
        raise HTTPException(status_code=404, detail=f"Position {mod_req.ticket} not found")
    
    position = positions[0]
    symbol_info = mt5.symbol_info(position.symbol)
    
    if symbol_info is None:
        raise HTTPException(status_code=400, detail="Cannot get symbol info")
    
    digits = symbol_info.digits
    new_sl = round(mod_req.sl, digits) if mod_req.sl is not None else position.sl
    new_tp = round(mod_req.tp, digits) if mod_req.tp is not None else position.tp
    
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": position.symbol,
        "position": mod_req.ticket,
        "sl": new_sl,
        "tp": new_tp,
    }
    
    result = mt5.order_send(request)
    
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        raise HTTPException(status_code=400, detail=f"Modify failed: {result.comment if result else 'Unknown'}")
    
    return {
        "success": True,
        "ticket": mod_req.ticket,
        "sl": new_sl,
        "tp": new_tp
    }


@app.get("/positions")
async def get_positions(_auth: bool = Depends(verify_auth)):
    """Get all open positions."""
    if not mt5_initialized:
        raise HTTPException(status_code=400, detail="MT5 not initialized")
    
    acc = mt5.account_info()
    positions = mt5.positions_get()
    
    position_list = []
    total_profit = 0.0
    
    if positions:
        for pos in positions:
            position_list.append({
                "ticket": pos.ticket,
                "symbol": pos.symbol,
                "direction": "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL",
                "volume": pos.volume,
                "entry_price": pos.price_open,
                "current_price": pos.price_current,
                "sl": pos.sl if pos.sl != 0 else None,
                "tp": pos.tp if pos.tp != 0 else None,
                "position_id": pos.ticket,
                "profit": pos.profit,
                "open_time": server_time(pos.time)
            })
            total_profit += pos.profit
    
    margin_level = (acc.equity / acc.margin * 100) if acc.margin > 0 else 0
    
    return {
        "success": True,
        "balance": acc.balance,
        "equity": acc.equity,
        "margin": acc.margin,
        "free_margin": acc.margin_free,
        "margin_level": round(margin_level, 2),
        "open_count": len(position_list),
        "total_profit": round(total_profit, 2),
        "positions": position_list
    }


@app.get("/history")
async def get_history(hours: int = 0, _auth: bool = Depends(verify_auth)):
    """Get trade history."""
    if not mt5_initialized:
        raise HTTPException(status_code=400, detail="MT5 not initialized")
    
    # Deal times are server time, so the start of the window is off by the broker's
    # UTC offset. A few hours extra is harmless; the far end absorbs the rest.
    now = datetime.now(timezone.utc)
    if hours > 0:
        from_time = now - timedelta(hours=hours)
    else:
        from_time = datetime(2000, 1, 1, tzinfo=timezone.utc)
    
    to_time = now + timedelta(days=5)
    
    deals = mt5.history_deals_get(from_time, to_time)
    if deals is None:
        return {"success": True, "count": 0, "deals": []}
    
    deal_list = []
    for deal in deals:
        entry_label = {0: "OPEN", 1: "CLOSE", 2: "ROLLOVER", 3: "SPLIT"}.get(deal.entry, "UNKNOWN")
        if deal.entry not in (0, 1, 2, 3):
            continue
        
        deal_list.append({
            "ticket": deal.order,
            "symbol": deal.symbol,
            "direction": "BUY" if deal.type == mt5.DEAL_TYPE_BUY else "SELL",
            "volume": deal.volume,
            "price": deal.price,
            "profit": deal.profit,
            "swap": deal.swap,
            "commission": deal.commission,
            "comment": deal.comment or "",
            "position_id": deal.position_id,
            "time": server_time(deal.time),
            "entry": entry_label
        })
    
    return {"success": True, "count": len(deal_list), "deals": deal_list}


@app.get("/data/range/{symbol}")
async def get_data_range(symbol: str, timeframe: str = "1h", start: str = "", end: str = "", _auth: bool = Depends(verify_auth)):
    """Get OHLC data for a date range."""
    if not mt5_initialized:
        raise HTTPException(status_code=400, detail="MT5 not initialized")

    timeframe_map = {
        '1m': mt5.TIMEFRAME_M1, '5m': mt5.TIMEFRAME_M5, '15m': mt5.TIMEFRAME_M15,
        '30m': mt5.TIMEFRAME_M30, '1h': mt5.TIMEFRAME_H1, '4h': mt5.TIMEFRAME_H4,
        '1d': mt5.TIMEFRAME_D1, '1w': mt5.TIMEFRAME_W1, '1M': mt5.TIMEFRAME_MN1
    }
    tf = timeframe_map.get(timeframe, mt5.TIMEFRAME_H1)

    if not mt5.symbol_select(symbol, True):
        raise HTTPException(status_code=404, detail=f"Symbol {symbol} not found")

    try:
        start_dt = as_utc(datetime.fromisoformat(start)) if start else datetime(2000, 1, 1, tzinfo=timezone.utc)
        end_dt = as_utc(datetime.fromisoformat(end)) if end else datetime.now(timezone.utc)
    except ValueError:
        raise HTTPException(status_code=400, detail="start and end must be ISO dates")

    rates = mt5.copy_rates_range(symbol, tf, start_dt, end_dt)
    if rates is None or len(rates) == 0:
        return {"success": True, "symbol": symbol, "timeframe": timeframe, "count": 0, "data": []}

    import pandas as pd
    df = pd.DataFrame(rates)

    data = []
    for _, row in df.iterrows():
        data.append({
            "time": int(row['time']),
            "open": float(row['open']),
            "high": float(row['high']),
            "low": float(row['low']),
            "close": float(row['close']),
            "tick_volume": int(row['tick_volume'])
        })

    return {
        "success": True,
        "symbol": symbol,
        "timeframe": timeframe,
        "count": len(data),
        "data": data
    }


@app.get("/data/latest/{symbol}")
async def get_latest_data(symbol: str, timeframe: str = "1h", count: int = 500, _auth: bool = Depends(verify_auth)):
    """Get latest OHLC data."""
    if not mt5_initialized:
        raise HTTPException(status_code=400, detail="MT5 not initialized")
    
    timeframe_map = {
        '1m': mt5.TIMEFRAME_M1,
        '5m': mt5.TIMEFRAME_M5,
        '15m': mt5.TIMEFRAME_M15,
        '30m': mt5.TIMEFRAME_M30,
        '1h': mt5.TIMEFRAME_H1,
        '4h': mt5.TIMEFRAME_H4,
        '1d': mt5.TIMEFRAME_D1,
        '1w': mt5.TIMEFRAME_W1,
        '1M': mt5.TIMEFRAME_MN1
    }
    
    tf = timeframe_map.get(timeframe, mt5.TIMEFRAME_H1)
    
    if not mt5.symbol_select(symbol, True):
        raise HTTPException(status_code=404, detail=f"Symbol {symbol} not found")
    
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None or len(rates) == 0:
        raise HTTPException(status_code=500, detail="No data available")
    
    import pandas as pd
    df = pd.DataFrame(rates)

    data = []
    for _, row in df.iterrows():
        data.append({
            "time": int(row['time']),
            "open": float(row['open']),
            "high": float(row['high']),
            "low": float(row['low']),
            "close": float(row['close']),
            "tick_volume": int(row['tick_volume'])
        })
    
    return {
        "success": True,
        "symbol": symbol,
        "timeframe": timeframe,
        "count": len(data),
        "data": data
    }


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  MT5 Connector Service - Interactive Mode")
    print("=" * 60)
    
    # Try to auto-connect at startup
    try:
        if STARTUP_PATH:
            if mt5.initialize(path=STARTUP_PATH):
                mt5_initialized = True
                terminal_path = STARTUP_PATH
                acc = mt5.account_info()
                print(f"Auto-Connected to MT5 Terminal")
                print(f"   Account: {acc.login} | Server: {acc.server}")
            else:
                print(f"Manual MT5 initialization required (Call /initialize via API)")
        else:
            if mt5.initialize():
                mt5_initialized = True
                acc = mt5.account_info()
                print(f"Auto-Connected to MT5 Terminal (Default)")
                print(f"   Account: {acc.login} | Server: {acc.server}")
            else:
                print(f"Manual MT5 initialization required (Call /initialize via API)")
    except Exception as e:
        print(f"Manual MT5 initialization required (Call /initialize via API)")
        print(f"Error: {e}")
    
    shown = SERVER_IP if BIND_HOST == "0.0.0.0" else BIND_HOST
    print(f"\nAPI Server starting on http://{shown}:{PORT}")
    print(f"Docs (Swagger UI): http://{shown}:{PORT}/docs")
    print(f"Demo-only trading guard: {'ON' if REQUIRE_DEMO else 'OFF'}")
    print(f"Largest order accepted: {MAX_VOLUME} lots (MT5_MAX_VOLUME)")
    if CONNECTOR_API_TOKEN:
        print("API token: required")
    elif ALLOW_NO_TOKEN:
        print("API token: NONE, explicitly allowed. Local use only.")
    else:
        print("API token: MISSING. Every request will be refused with 503.")
    print(f"Docs page: {'/docs enabled' if ENABLE_DOCS else 'disabled'}")
    print("=" * 60 + "\n")
    
    uvicorn.run(app, host=BIND_HOST, port=PORT)