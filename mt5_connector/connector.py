# MT5 Connector Service
# This runs on Windows server with MT5 terminal installed
# Works like your existing mt5_data_server.py

import sys
import os
import socket
from datetime import datetime, timedelta
from typing import Optional
import MetaTrader5 as mt5

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import uvicorn

load_dotenv()

# Fix Windows console encoding for emoji support
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

app = FastAPI(title="MT5 Connector Service")


def verify_auth(authorization: str = ""):
    if not CONNECTOR_API_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="Server misconfigured: MT5_API_TOKEN not set. Restart with a valid token."
        )
    token = authorization.replace("Bearer ", "").strip()
    if token != CONNECTOR_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid API token")

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
    token_env = os.getenv("MT5_API_TOKEN")

    if port_env and token_env:
        return int(port_env), terminal_env, token_env

    print("\n" + "=" * 60)
    print("      MT5 Connector Service - Configuration Startup")
    print("=" * 60 + "\n")

    default_port = os.getenv("MT5_CONNECTOR_PORT", "5001")
    port_input = input(f"Enter Port [Default {default_port}]: ").strip() or default_port
    port = int(port_input)

    print("\nMultiple MT5 Instances Detected?")
    print("   (Leave empty to use your default/active MT5)")
    terminal_path = input("Enter MT5 Terminal Path (e.g. C:\\...\\terminal64.exe): ").strip() or None

    if token_env:
        api_token = token_env
        print(f"\n  MT5_API_TOKEN loaded from environment.")
    else:
        print("\n  MT5_API_TOKEN is required for secure communication.")
        print("  Generate a strong token: python -c \"import secrets; print(secrets.token_hex(32))\"")
        api_token = input("  Enter MT5_API_TOKEN: ").strip()
        if not api_token:
            print("\n  ERROR: MT5_API_TOKEN cannot be empty!")
            print("  The connector will REJECT all requests without a token.")
            print("  Restart with a valid token.\n")
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
async def root(authorization: str = Header("")):
    verify_auth(authorization)
    return {
        "service": "MT5 Connector",
        "version": "2.0.0",
        "mt5_initialized": mt5_initialized,
        "last_error": last_error,
        "terminal_path": terminal_path,
        "timestamp": datetime.now().isoformat()
    }


@app.get("/health")
async def health(authorization: str = Header("")):
    verify_auth(authorization)
    return {
        "status": "healthy" if mt5_initialized else "not_initialized",
        "mt5_connected": mt5_initialized,
        "terminal_path": terminal_path
    }


@app.post("/initialize")
async def initialize_mt5(terminal_path_input: Optional[str] = None, authorization: str = Header("")):
    verify_auth(authorization)
    """Initialize MT5 connection."""
    global mt5_initialized, last_error, terminal_path
    
    try:
        path = terminal_path_input or STARTUP_PATH
        
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
                "equity": account.equity
            }
        }
    except Exception as e:
        last_error = str(e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/shutdown")
async def shutdown_mt5(authorization: str = Header("")):
    verify_auth(authorization)
    """Shutdown MT5 connection."""
    global mt5_initialized
    mt5.shutdown()
    mt5_initialized = False
    return {"success": True, "message": "MT5 shutdown"}


@app.get("/account")
async def get_account(authorization: str = Header("")):
    verify_auth(authorization)
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
async def get_symbols(authorization: str = Header("")):
    verify_auth(authorization)
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
async def get_symbol(symbol: str, authorization: str = Header("")):
    verify_auth(authorization)
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
        "volume_max": info.volume_max
    }


@app.post("/order")
async def place_order(order: OrderRequest, authorization: str = Header("")):
    verify_auth(authorization)
    """Place an order."""
    if not mt5_initialized:
        raise HTTPException(status_code=400, detail="MT5 not initialized")
    
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
    
    min_dist = max(symbol_info.trade_stops_level, 10) * point
    
    sl = None
    if order.sl is not None:
        sl = round(order.sl, digits)
        if "BUY" in order.action:
            if sl >= price - min_dist:
                sl = round(price - min_dist, digits)
        else:
            if sl <= price + min_dist:
                sl = round(price + min_dist, digits)
    
    tp = None
    if order.tp is not None:
        tp = round(order.tp, digits)
        if "BUY" in order.action:
            if tp <= price + min_dist:
                tp = round(price + min_dist, digits)
        else:
            if tp >= price - min_dist:
                tp = round(price - min_dist, digits)

    if sl is not None and ((is_buy and sl >= price) or (not is_buy and sl <= price)):
        raise HTTPException(status_code=400, detail="Adjusted stop loss is on the wrong side of the order price")
    if tp is not None and ((is_buy and tp <= price) or (not is_buy and tp >= price)):
        raise HTTPException(status_code=400, detail="Adjusted take profit is on the wrong side of the order price")
    if sl is not None and order.max_sl_distance is not None:
        if abs(price - sl) > order.max_sl_distance + point:
            raise HTTPException(status_code=400, detail="Broker stop-distance requirement exceeds the configured risk cap")
    if sl is not None and tp is not None and order.min_reward_risk is not None:
        risk_distance = abs(price - sl)
        reward_distance = abs(tp - price)
        if risk_distance <= 0 or reward_distance < risk_distance * order.min_reward_risk:
            raise HTTPException(status_code=400, detail="Broker stop-distance adjustment violates the minimum reward/risk ratio")
    
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
        "volume": volume,
        "price": result.price,
        "submitted_quote": None if is_pending else price,
        "sl": sl,
        "tp": tp,
        "comment": result.comment,
        "position": getattr(result, "position", 0) or None
    }


@app.post("/close")
async def close_position(close_req: CloseRequest, authorization: str = Header("")):
    verify_auth(authorization)
    """Close a position."""
    if not mt5_initialized:
        raise HTTPException(status_code=400, detail="MT5 not initialized")
    
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
    
    return {
        "success": True,
        "ticket": close_req.ticket,
        "closed_volume": close_volume,
        "close_price": price,
        "comment": result.comment
    }


@app.post("/modify")
async def modify_position(mod_req: ModifyRequest, authorization: str = Header("")):
    verify_auth(authorization)
    """Modify SL/TP of a position."""
    if not mt5_initialized:
        raise HTTPException(status_code=400, detail="MT5 not initialized")
    
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
async def get_positions(authorization: str = Header("")):
    verify_auth(authorization)
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
                "open_time": datetime.fromtimestamp(pos.time).strftime('%Y-%m-%d %H:%M:%S')
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
async def get_history(hours: int = 0, authorization: str = Header("")):
    verify_auth(authorization)
    """Get trade history."""
    if not mt5_initialized:
        raise HTTPException(status_code=400, detail="MT5 not initialized")
    
    if hours > 0:
        from_time = datetime.now() - timedelta(hours=hours)
    else:
        from_time = datetime(2000, 1, 1)
    
    to_time = datetime.now() + timedelta(days=5)
    
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
            "time": datetime.utcfromtimestamp(deal.time).strftime('%Y-%m-%d %H:%M:%S'),
            "time_msc": int(getattr(deal, "time_msc", 0) or 0),
            "entry": entry_label
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
        "setup_time": datetime.fromtimestamp(setup_time).strftime('%Y-%m-%d %H:%M:%S') if setup_time else None,
        "done_time": datetime.fromtimestamp(done_time).strftime('%Y-%m-%d %H:%M:%S') if done_time else None,
    }


@app.get("/orders")
async def get_orders(authorization: str = Header("")):
    """Get currently active MT5 orders, including unfilled pending orders."""
    verify_auth(authorization)
    if not mt5_initialized:
        raise HTTPException(status_code=400, detail="MT5 not initialized")
    orders = mt5.orders_get()
    if orders is None:
        error = mt5.last_error()
        raise HTTPException(status_code=502, detail=f"Could not read active orders: {error}")
    serialized = [_serialize_order(order, is_active=True) for order in orders]
    return {"success": True, "count": len(serialized), "orders": serialized}


@app.get("/history/orders")
async def get_order_history(hours: int = 0, authorization: str = Header("")):
    """Get historical orders to observe fills, cancellation, rejection, and expiry."""
    verify_auth(authorization)
    if not mt5_initialized:
        raise HTTPException(status_code=400, detail="MT5 not initialized")
    from_time = datetime.now() - timedelta(hours=hours) if hours > 0 else datetime(2000, 1, 1)
    to_time = datetime.now() + timedelta(days=5)
    orders = mt5.history_orders_get(from_time, to_time)
    if orders is None:
        error = mt5.last_error()
        raise HTTPException(status_code=502, detail=f"Could not read order history: {error}")
    serialized = [_serialize_order(order, is_active=False) for order in orders]
    return {"success": True, "count": len(serialized), "orders": serialized}


@app.get("/data/range/{symbol}")
async def get_data_range(symbol: str, timeframe: str = "1h", start: str = "", end: str = "", authorization: str = Header("")):
    verify_auth(authorization)
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

    from datetime import datetime as dt
    start_dt = dt.fromisoformat(start) if start else dt(2000, 1, 1)
    end_dt = dt.fromisoformat(end) if end else dt.now()

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
async def get_latest_data(symbol: str, timeframe: str = "1h", count: int = 500, authorization: str = Header("")):
    verify_auth(authorization)
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
    
    print(f"\nAPI Server starting on http://{SERVER_IP}:{PORT}")
    print(f"Docs (Swagger UI): http://{SERVER_IP}:{PORT}/docs")
    print("=" * 60 + "\n")
    
    uvicorn.run(app, host="0.0.0.0", port=PORT)
