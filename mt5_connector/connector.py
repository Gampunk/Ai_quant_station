# MT5 Connector Service
# This runs on Windows server with MT5 terminal installed
# Works like your existing mt5_data_server.py

import sys
import os
import secrets
import socket
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
import MetaTrader5 as mt5

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import uvicorn

load_dotenv()

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


# Why a deal happened, from MT5's DEAL_REASON_* values. Stop and target hits used to
# be guessed by searching the comment for "sl" or "tp".
DEAL_REASONS = {0: "client", 1: "mobile", 2: "web", 3: "expert", 4: "sl", 5: "tp",
                6: "stop_out", 7: "rollover", 8: "variation_margin", 9: "split"}

# Symbols whose latest price reveals the broker's clock. Crypto often trades at weekends.
CLOCK_SYMBOLS = [s.strip() for s in os.getenv("MT5_CLOCK_SYMBOLS", "XAUUSD,EURUSD,BTCUSD").split(",") if s.strip()]
_clock_last = {"tick": None, "at": None}


def read_broker_clock() -> dict:
    """Estimate how far the broker's server clock runs ahead of UTC.

    MT5 has no call for the server's time zone. The latest price's time is server
    time, so while prices are live it sits within seconds of UTC plus the offset.
    When the market is closed the latest price is hours old and says nothing, so
    an offset is reported only when prices moved since the previous reading and the
    difference lands within 5 minutes of a whole or half hour.
    """
    ticks = [mt5.symbol_info_tick(name) for name in CLOCK_SYMBOLS if mt5.symbol_select(name, True)]
    times = [t.time for t in ticks if t is not None and t.time]
    now = time.time()
    if not times:
        return {"offset_hours": None, "live": False, "server_time": None, "utc_time": _utc_text(now)}
    tick = max(times)
    prev_tick, prev_at = _clock_last["tick"], _clock_last["at"]
    live = prev_tick is not None and tick > prev_tick and now - prev_at < 3600
    _clock_last.update(tick=tick, at=now)
    diff = tick - now
    offset = round(diff / 1800) / 2
    reliable = live and abs(diff - offset * 3600) <= 300 and -12 <= offset <= 14
    return {"offset_hours": offset if reliable else None, "live": live,
            "server_time": server_time(tick), "utc_time": _utc_text(now)}


def _utc_text(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


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
    """Port, terminal path and token, from the environment (or the .env file next to
    this script), asking at the console only for what is missing.

    The token is never asked for when MT5_ALLOW_NO_TOKEN is on, as for the fake
    terminal in tests. Without a token the connector refuses every request.
    """
    port_env = os.getenv("MT5_CONNECTOR_PORT")
    terminal_env = os.getenv("MT5_TERMINAL_PATH")
    token_env = os.getenv("MT5_API_TOKEN", "")

    if port_env and (token_env or ALLOW_NO_TOKEN):
        return int(port_env), terminal_env, token_env

    print("\n" + "=" * 60)
    print("      MT5 Connector Service - Configuration Startup")
    print("=" * 60 + "\n")

    if port_env:
        port, terminal_path = int(port_env), terminal_env
    else:
        port_input = input("Enter Port [Default 5001]: ").strip() or "5001"
        port = int(port_input)
        print("\nMultiple MT5 Instances Detected?")
        print("   (Leave empty to use your default/active MT5)")
        terminal_path = input("Enter MT5 Terminal Path (e.g. C:\\...\\terminal64.exe): ").strip() or None

    api_token = token_env
    if not api_token and not ALLOW_NO_TOKEN:
        print("\n  MT5_API_TOKEN is required for secure communication.")
        print("  Generate a strong token: python -c \"import secrets; print(secrets.token_hex(32))\"")
        api_token = input("  Enter MT5_API_TOKEN: ").strip()
        if not api_token:
            print("\n  ERROR: MT5_API_TOKEN cannot be empty. Restart with a valid token.\n")
            raise SystemExit(1)
    return port, terminal_path, api_token


PORT, STARTUP_PATH, CONNECTOR_API_TOKEN = get_startup_config()
SERVER_IP = get_network_ip()


class OrderRequest(BaseModel):
    symbol: str
    action: str
    volume: float
    price: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    max_sl_distance: Optional[float] = None
    min_reward_risk: Optional[float] = None
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


@app.get("/clock")
async def clock(_auth: bool = Depends(verify_auth)):
    """The broker's server clock against UTC. offset_hours is None until prices are live."""
    if not mt5_initialized:
        raise HTTPException(status_code=400, detail="MT5 not initialized")
    return read_broker_clock()


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

    # Reject malformed protection geometry instead of silently moving a stop or
    # target across the entry. Broker-distance adjustments below only handle
    # levels that are on the correct side but too close.
    is_buy = "BUY" in order.action
    if order.sl is not None and ((is_buy and order.sl >= price) or (not is_buy and order.sl <= price)):
        raise HTTPException(status_code=400, detail="Stop loss is on the wrong side of the order price")
    if order.tp is not None and ((is_buy and order.tp <= price) or (not is_buy and order.tp >= price)):
        raise HTTPException(status_code=400, detail="Take profit is on the wrong side of the order price")
    
    sl = round(order.sl, digits) if order.sl is not None else None
    tp = round(order.tp, digits) if order.tp is not None else None
    check_stops(order.action, price, sl, tp, min_stop_distance(symbol_info), digits)
    if sl is not None and order.max_sl_distance is not None:
        if abs(price - sl) > order.max_sl_distance + point:
            raise HTTPException(status_code=400, detail="Stop loss is further than the configured risk cap allows")
    if sl is not None and tp is not None and order.min_reward_risk is not None:
        risk_distance = abs(price - sl)
        reward_distance = abs(tp - price)
        if risk_distance <= 0 or reward_distance < risk_distance * order.min_reward_risk:
            raise HTTPException(status_code=400, detail="Take profit is below the minimum reward/risk ratio")
    
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
    
    accepted_retcodes = {
        mt5.TRADE_RETCODE_DONE,
        getattr(mt5, "TRADE_RETCODE_PLACED", -1),
        getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", -1),
    }
    if result is None or result.retcode not in accepted_retcodes:
        raise HTTPException(status_code=400, detail=f"Order failed: {result.comment if result else 'Unknown'}")

    retcode = result.retcode
    order_status = (
        "placed" if retcode == getattr(mt5, "TRADE_RETCODE_PLACED", -1)
        else "partially_filled" if retcode == getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", -1)
        else "filled"
    )
    
    # result.price is what the broker filled at. `price` above was only the quote
    # we saw beforehand, and the two differ by the slippage on the fill.
    filled_price = result.price if getattr(result, "price", 0) else price
    return {
        "success": True,
        "ticket": result.order,
        "order_ticket": result.order,
        "deal": result.deal,
        "deal_ticket": result.deal,
        "retcode": retcode,
        "order_status": order_status,
        "is_pending": is_pending,
        "symbol": order.symbol,
        "volume": result.volume if getattr(result, "volume", 0) else volume,
        "price": filled_price,
        "requested_price": price,
        "submitted_quote": None if is_pending else price,
        "sl": sl,
        "tp": tp,
        "comment": result.comment,
        "position": getattr(result, "position", 0) or None
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
        entry_labels = {
            getattr(mt5, "DEAL_ENTRY_IN", 0): "OPEN",
            getattr(mt5, "DEAL_ENTRY_OUT", 1): "CLOSE",
            getattr(mt5, "DEAL_ENTRY_INOUT", 2): "INOUT",
            getattr(mt5, "DEAL_ENTRY_OUT_BY", 3): "OUT_BY",
        }
        reason_names = {
            getattr(mt5, "DEAL_REASON_CLIENT", 0): "CLIENT",
            getattr(mt5, "DEAL_REASON_MOBILE", 1): "MOBILE",
            getattr(mt5, "DEAL_REASON_WEB", 2): "WEB",
            getattr(mt5, "DEAL_REASON_EXPERT", 3): "EXPERT",
            getattr(mt5, "DEAL_REASON_SL", 4): "SL",
            getattr(mt5, "DEAL_REASON_TP", 5): "TP",
            getattr(mt5, "DEAL_REASON_SO", 6): "STOP_OUT",
            getattr(mt5, "DEAL_REASON_ROLLOVER", -99): "ROLLOVER",
        }
        entry_label = entry_labels.get(deal.entry)
        if entry_label is None:
            continue
        reason_code = int(getattr(deal, "reason", -1))
        reason_label = reason_names.get(reason_code)
        if reason_label is None:
            for code_name in ("DEAL_REASON_VMARGIN", "DEAL_REASON_SPLIT", "DEAL_REASON_CORPORATE_ACTION"):
                code = getattr(mt5, code_name, None)
                if code is not None and reason_code == int(code):
                    reason_label = code_name.removeprefix("DEAL_REASON_")
                    break
        
        deal_list.append({
            "ticket": deal.order,
            "deal_ticket": deal.ticket,
            "order_ticket": deal.order,
            "reason_code": reason_code,
            "reason": reason_label or "UNKNOWN",
            "entry_code": int(deal.entry),
            "symbol": deal.symbol,
            "direction": "BUY" if deal.type == mt5.DEAL_TYPE_BUY else "SELL",
            "volume": deal.volume,
            "price": deal.price,
            "profit": deal.profit,
            "swap": deal.swap,
            "commission": deal.commission,
            "comment": deal.comment or "",
            "position_id": deal.position_id,
            "magic": int(getattr(deal, "magic", 0) or 0),
            "time": server_time(deal.time),
            "time_msc": int(getattr(deal, "time_msc", 0) or 0),
            "entry": entry_label,
            "reason": DEAL_REASONS.get(getattr(deal, "reason", None), "unknown"),
        })
    
    return {"success": True, "count": len(deal_list), "deals": deal_list}


def _serialize_order(order, is_active: bool = False):
    state_names = {
        getattr(mt5, "ORDER_STATE_STARTED", -101): "started",
        getattr(mt5, "ORDER_STATE_PLACED", -102): "placed",
        getattr(mt5, "ORDER_STATE_CANCELED", -103): "cancelled",
        getattr(mt5, "ORDER_STATE_PARTIAL", -104): "partially_filled",
        getattr(mt5, "ORDER_STATE_FILLED", -105): "filled",
        getattr(mt5, "ORDER_STATE_REJECTED", -106): "rejected",
        getattr(mt5, "ORDER_STATE_EXPIRED", -107): "expired",
        getattr(mt5, "ORDER_STATE_REQUEST_ADD", -108): "request_add",
        getattr(mt5, "ORDER_STATE_REQUEST_MODIFY", -109): "request_modify",
        getattr(mt5, "ORDER_STATE_REQUEST_CANCEL", -110): "request_cancel",
    }
    type_names = {
        getattr(mt5, "ORDER_TYPE_BUY", -201): "buy",
        getattr(mt5, "ORDER_TYPE_SELL", -202): "sell",
        getattr(mt5, "ORDER_TYPE_BUY_LIMIT", -203): "buy_limit",
        getattr(mt5, "ORDER_TYPE_SELL_LIMIT", -204): "sell_limit",
        getattr(mt5, "ORDER_TYPE_BUY_STOP", -205): "buy_stop",
        getattr(mt5, "ORDER_TYPE_SELL_STOP", -206): "sell_stop",
        getattr(mt5, "ORDER_TYPE_BUY_STOP_LIMIT", -207): "buy_stop_limit",
        getattr(mt5, "ORDER_TYPE_SELL_STOP_LIMIT", -208): "sell_stop_limit",
    }
    setup_time = getattr(order, "time_setup", None)
    done_time = getattr(order, "time_done", None)
    return {
        "ticket": int(order.ticket), "order_ticket": int(order.ticket),
        "symbol": order.symbol, "status": state_names.get(order.state, "unknown"),
        "is_active": is_active,
        "state_code": int(order.state), "type": type_names.get(order.type, "unknown"),
        "volume_initial": float(order.volume_initial), "volume_current": float(order.volume_current),
        "price_open": float(order.price_open), "price_current": float(order.price_current),
        "sl": float(order.sl) if order.sl else None, "tp": float(order.tp) if order.tp else None,
        "position_id": int(getattr(order, "position_id", 0) or 0) or None,
        "magic": int(getattr(order, "magic", 0) or 0), "comment": getattr(order, "comment", "") or "",
        # Broker server time, like every other endpoint here; the backend converts to UTC.
        "setup_time": server_time(setup_time) if setup_time else None,
        "done_time": server_time(done_time) if done_time else None,
    }


@app.get("/orders")
async def get_orders(_auth: bool = Depends(verify_auth)):
    """Get currently active MT5 orders, including unfilled pending orders."""
    if not mt5_initialized:
        raise HTTPException(status_code=400, detail="MT5 not initialized")
    orders = mt5.orders_get()
    if orders is None:
        error = mt5.last_error()
        raise HTTPException(status_code=502, detail=f"Could not read active orders: {error}")
    serialized = [_serialize_order(order, is_active=True) for order in orders]
    return {"success": True, "count": len(serialized), "orders": serialized}


@app.get("/history/orders")
async def get_order_history(hours: int = 0, _auth: bool = Depends(verify_auth)):
    """Get historical orders to observe fills, cancellation, rejection, and expiry."""
    if not mt5_initialized:
        raise HTTPException(status_code=400, detail="MT5 not initialized")
    now = datetime.now(timezone.utc)
    from_time = now - timedelta(hours=hours) if hours > 0 else datetime(2000, 1, 1, tzinfo=timezone.utc)
    to_time = now + timedelta(days=5)
    orders = mt5.history_orders_get(from_time, to_time)
    if orders is None:
        error = mt5.last_error()
        raise HTTPException(status_code=502, detail=f"Could not read order history: {error}")
    serialized = [_serialize_order(order, is_active=False) for order in orders]
    return {"success": True, "count": len(serialized), "orders": serialized}


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
