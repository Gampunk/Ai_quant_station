"""
Fake MetaTrader5 module for tests and local development.

It implements the subset of the real MetaTrader5 Python API that connector.py
uses, with deterministic prices, an in-memory account, positions and deal
history. It lets the real connector code run on any OS with no broker.

It is only importable when its folder is put first on sys.path, which only
testing/run_fake_connector.py and the test suites do. IS_FAKE lets callers
assert they did not load the real package by accident.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time as _time
from collections import namedtuple
from datetime import datetime, timezone

import numpy as np

IS_FAKE = True
__version__ = "fake-1.0"

# ── Constants, matching the real package values ──────────────────────────────
TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_M30 = 1, 5, 15, 30
TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_D1 = 16385, 16388, 16408
TIMEFRAME_W1, TIMEFRAME_MN1 = 32769, 49153
_TF_SECONDS = {1: 60, 5: 300, 15: 900, 30: 1800, 16385: 3600, 16388: 14400,
               16408: 86400, 32769: 604800, 49153: 2592000}

ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT = 2, 3
ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP = 4, 5
ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
ORDER_TIME_GTC = 0
TRADE_ACTION_DEAL, TRADE_ACTION_PENDING, TRADE_ACTION_SLTP = 1, 5, 6
POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1
DEAL_ENTRY_IN, DEAL_ENTRY_OUT = 0, 1
ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_CONTEST, ACCOUNT_TRADE_MODE_REAL = 0, 1, 2

TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_INVALID_VOLUME = 10014
TRADE_RETCODE_INVALID_STOPS = 10016
TRADE_RETCODE_POSITION_CLOSED = 10036
TRADE_RETCODE_INVALID = 10013

# ── Record types, named like the real package ────────────────────────────────
AccountInfo = namedtuple("AccountInfo", "login server name balance equity margin margin_free profit currency leverage trade_mode")
SymbolInfo = namedtuple("SymbolInfo", "name description visible point digits volume_min volume_max volume_step trade_stops_level filling_mode trade_contract_size trade_tick_size trade_tick_value")
Tick = namedtuple("Tick", "time bid ask last volume")
OrderSendResult = namedtuple("OrderSendResult", "retcode deal order volume price bid ask comment request_id")
TradePosition = namedtuple("TradePosition", "ticket time type volume price_open sl tp price_current profit symbol comment magic")
TradeDeal = namedtuple("TradeDeal", "ticket order time type entry position_id volume price profit swap commission symbol comment magic")

_RATE_DTYPE = np.dtype([("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"),
                        ("close", "<f8"), ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")])

# name: (description, point, digits, start price, spread in points, contract size, stops level)
_SYMBOLS = {
    "XAUUSD": ("Gold vs US Dollar", 0.01, 2, 2650.00, 20, 100.0, 0),
    "XAGUSD": ("Silver vs US Dollar", 0.001, 3, 31.000, 30, 5000.0, 0),
    "EURUSD": ("Euro vs US Dollar", 0.00001, 5, 1.08000, 10, 100000.0, 0),
    "GBPUSD": ("Pound vs US Dollar", 0.00001, 5, 1.27000, 12, 100000.0, 0),
    "USDJPY": ("US Dollar vs Yen", 0.001, 3, 150.000, 12, 100000.0, 0),
    "BTCUSD": ("Bitcoin vs US Dollar", 0.01, 2, 65000.00, 1500, 1.0, 0),
}

_lock = threading.RLock()
_S: dict = {}


def _reset(trade_mode: int = ACCOUNT_TRADE_MODE_DEMO, now: int | None = None,
           balance: float = 10_000.0, init_ok: bool = True, account_available: bool = True,
           slippage_points: int = 2) -> None:
    """Test helper. Restore a clean account. `now` is a unix timestamp that fixes the clock."""
    with _lock:
        env_now = os.getenv("FAKE_MT5_NOW")
        _S.clear()
        _S.update(
            initialized=False, init_ok=init_ok, path=None, account_available=account_available,
            trade_mode=trade_mode, balance=balance,
            now=now if now is not None else (int(env_now) if env_now else None),
            next_ticket=100_001, positions={}, deals=[], orders={},
            ticks={}, last_error=(1, "Success"), slippage_points=slippage_points,
        )
        for name, (_, point, digits, _, spread, _, _) in _SYMBOLS.items():
            bid = round(float(_mid(name, np.array([_now()]))[0]), digits)
            _S["ticks"][name] = (bid, round(bid + spread * point, digits))


def _now() -> int:
    return _S["now"] if _S["now"] is not None else int(_time.time())


def _set_price(symbol: str, bid: float, ask: float | None = None) -> None:
    """Test helper. Move the market and trigger any stop loss or take profit."""
    with _lock:
        _, point, digits, _, spread, _, _ = _SYMBOLS[symbol]
        bid = round(bid, digits)
        ask = round(ask if ask is not None else bid + spread * point, digits)
        _S["ticks"][symbol] = (bid, ask)
        for pos in list(_S["positions"].values()):
            if pos["symbol"] != symbol:
                continue
            buy = pos["type"] == POSITION_TYPE_BUY
            mark = bid if buy else ask
            if pos["sl"] and ((buy and mark <= pos["sl"]) or (not buy and mark >= pos["sl"])):
                _close(pos, pos["volume"], pos["sl"], f"[sl {pos['sl']}]")
            elif pos["tp"] and ((buy and mark >= pos["tp"]) or (not buy and mark <= pos["tp"])):
                _close(pos, pos["volume"], pos["tp"], f"[tp {pos['tp']}]")


def _advance(seconds: int) -> None:
    """Test helper. Move the fixed clock forward."""
    with _lock:
        _S["now"] = _now() + seconds


# ── Connection ───────────────────────────────────────────────────────────────
def initialize(path: str | None = None, **_kwargs) -> bool:
    with _lock:
        if not _S["init_ok"]:
            _S["last_error"] = (-10003, "IPC initialize failed, fake terminal refused")
            return False
        _S["initialized"] = True
        _S["path"] = path
        return True


def shutdown() -> None:
    with _lock:
        _S["initialized"] = False


def last_error():
    return _S["last_error"]


def version():
    return (500, 5000, "fake")


# ── Account and symbols ──────────────────────────────────────────────────────
def _contract(symbol):
    return _SYMBOLS[symbol][5]


def _profit(symbol, pos_type, volume, open_price, price):
    direction = 1 if pos_type == POSITION_TYPE_BUY else -1
    raw = (price - open_price) * direction * volume * _contract(symbol)
    if symbol.startswith("USD") and not symbol.endswith("USD"):
        raw = raw / price
    return round(raw, 2)


def _floating(pos):
    bid, ask = _S["ticks"][pos["symbol"]]
    price = bid if pos["type"] == POSITION_TYPE_BUY else ask
    return price, _profit(pos["symbol"], pos["type"], pos["volume"], pos["price_open"], price)


def account_info():
    with _lock:
        if not _S["account_available"]:
            return None
        floating = sum(_floating(p)[1] for p in _S["positions"].values())
        margin = round(sum(p["volume"] * _contract(p["symbol"]) * p["price_open"] / 100
                           for p in _S["positions"].values()), 2)
        equity = round(_S["balance"] + floating, 2)
        return AccountInfo(login=5_000_001, server="FakeBroker-Demo", name="Fake Demo",
                           balance=round(_S["balance"], 2), equity=equity, margin=margin,
                           margin_free=round(equity - margin, 2), profit=round(floating, 2),
                           currency="USD", leverage=100, trade_mode=_S["trade_mode"])


def _symbol(name):
    desc, point, digits, _, _, contract, stops = _SYMBOLS[name]
    # A USD account: one tick is worth point * contract dollars, converted when USD is the base.
    tick_value = point * contract
    if name.startswith("USD") and _S.get("ticks"):
        tick_value /= _S["ticks"][name][0]
    return SymbolInfo(name=name, description=desc, visible=True, point=point, digits=digits,
                      volume_min=0.01, volume_max=100.0, volume_step=0.01,
                      trade_stops_level=stops, filling_mode=1, trade_contract_size=contract,
                      trade_tick_size=point, trade_tick_value=tick_value)


def symbols_get(group: str | None = None):
    return tuple(_symbol(n) for n in _SYMBOLS)


def symbol_info(symbol):
    return _symbol(symbol) if symbol in _SYMBOLS else None


def symbol_info_tick(symbol):
    with _lock:
        if symbol not in _SYMBOLS:
            return None
        bid, ask = _S["ticks"][symbol]
        return Tick(time=_now(), bid=bid, ask=ask, last=bid, volume=0)


def symbol_select(symbol, enable=True):
    return symbol in _SYMBOLS


# ── Price history, continuous and deterministic per symbol ───────────────────
def _hash01(x):
    """Deterministic pseudo-random values in [0, 1) for each element, vectorized."""
    v = np.sin(x * 12.9898 + 78.233) * 43758.5453
    return v - np.floor(v)


def _mid(symbol, t):
    """Mid price as a pure function of time, so any window agrees on shared bars."""
    base = _SYMBOLS[symbol][3]
    salt = int(hashlib.sha256(symbol.encode()).hexdigest()[:6], 16)
    t = t.astype(np.float64) + salt
    wave = (0.030 * np.sin(t / 1_900_000.0) + 0.010 * np.sin(t / 310_000.0)
            + 0.004 * np.sin(t / 47_000.0) + 0.0015 * np.sin(t / 7_300.0))
    noise = (_hash01(np.floor(t / 60.0)) - 0.5) * 0.0012
    return base * (1.0 + wave + noise)


def _series(symbol, timeframe, end_time, count):
    step = _TF_SECONDS[timeframe]
    last_open = end_time - (end_time % step)
    times = last_open - step * np.arange(count - 1, -1, -1, dtype=np.int64)
    digits = _SYMBOLS[symbol][2]
    opens = _mid(symbol, times)
    closes = _mid(symbol, times + step)
    span = np.abs(closes - opens) + _SYMBOLS[symbol][3] * 0.0004 * np.sqrt(step / 60.0)
    highs = np.maximum(opens, closes) + span * _hash01(times.astype(np.float64) + 0.25)
    lows = np.minimum(opens, closes) - span * _hash01(times.astype(np.float64) + 0.75)
    out = np.zeros(count, dtype=_RATE_DTYPE)
    out["time"] = times
    out["open"], out["high"] = np.round(opens, digits), np.round(highs, digits)
    out["low"], out["close"] = np.round(lows, digits), np.round(closes, digits)
    out["tick_volume"] = 100 + (times % 997)
    out["spread"] = _SYMBOLS[symbol][4]
    return out


def copy_rates_from_pos(symbol, timeframe, start_pos, count):
    if symbol not in _SYMBOLS or timeframe not in _TF_SECONDS or count <= 0:
        return None
    step = _TF_SECONDS[timeframe]
    return _series(symbol, timeframe, _now() - start_pos * step, int(count))


def copy_rates_range(symbol, timeframe, date_from, date_to):
    if symbol not in _SYMBOLS or timeframe not in _TF_SECONDS:
        return None
    start, end = _ts(date_from), min(_ts(date_to), _now())
    step = _TF_SECONDS[timeframe]
    count = max(0, (end - start) // step)
    count = min(count, 100_000)
    return _series(symbol, timeframe, end, int(count)) if count else np.zeros(0, dtype=_RATE_DTYPE)


def _ts(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    return int(value)


# ── Trading ──────────────────────────────────────────────────────────────────
def _slipped(symbol, price, worse_upward):
    """Fill a little away from the quote, the way a real broker does."""
    point = _SYMBOLS[symbol][1]
    digits = _SYMBOLS[symbol][2]
    offset = _S["slippage_points"] * point
    return round(price + offset if worse_upward else price - offset, digits)


def _result(retcode, comment, order=0, deal=0, volume=0.0, price=0.0, bid=0.0, ask=0.0):
    return OrderSendResult(retcode=retcode, deal=deal, order=order, volume=volume, price=price,
                           bid=bid, ask=ask, comment=comment, request_id=1)


def _ticket():
    t = _S["next_ticket"]
    _S["next_ticket"] += 1
    return t


def _deal(pos, entry, volume, price, profit, comment):
    deal_type = pos["type"] if entry == DEAL_ENTRY_IN else 1 - pos["type"]
    ticket = _ticket()
    _S["deals"].append(TradeDeal(ticket=ticket, order=ticket, time=_now(), type=deal_type, entry=entry,
                                 position_id=pos["ticket"], volume=volume, price=price, profit=profit,
                                 swap=0.0, commission=0.0, symbol=pos["symbol"], comment=comment,
                                 magic=pos["magic"]))
    return ticket


def _close(pos, volume, price, comment):
    profit = _profit(pos["symbol"], pos["type"], volume, pos["price_open"], price)
    deal = _deal(pos, DEAL_ENTRY_OUT, volume, price, profit, comment)
    _S["balance"] += profit
    pos["volume"] = round(pos["volume"] - volume, 2)
    if pos["volume"] <= 0:
        del _S["positions"][pos["ticket"]]
    return deal, profit


def _stops_valid(side_buy, price, sl, tp):
    if sl and ((side_buy and sl >= price) or (not side_buy and sl <= price)):
        return False
    if tp and ((side_buy and tp <= price) or (not side_buy and tp >= price)):
        return False
    return True


def order_send(request: dict):
    with _lock:
        if not _S["initialized"]:
            _S["last_error"] = (-10004, "No IPC connection")
            return None
        action = request.get("action")
        symbol = request.get("symbol")
        if symbol not in _SYMBOLS:
            return _result(TRADE_RETCODE_INVALID, "Invalid symbol")
        bid, ask = _S["ticks"][symbol]
        digits = _SYMBOLS[symbol][2]

        if action == TRADE_ACTION_SLTP:
            pos = _S["positions"].get(request.get("position"))
            if pos is None:
                return _result(TRADE_RETCODE_POSITION_CLOSED, "Position doesn't exist", bid=bid, ask=ask)
            buy = pos["type"] == POSITION_TYPE_BUY
            sl, tp = request.get("sl") or 0.0, request.get("tp") or 0.0
            if not _stops_valid(buy, bid if buy else ask, sl, tp):
                return _result(TRADE_RETCODE_INVALID_STOPS, "Invalid stops", bid=bid, ask=ask)
            pos["sl"], pos["tp"] = round(sl, digits), round(tp, digits)
            return _result(TRADE_RETCODE_DONE, "Request executed", order=pos["ticket"], bid=bid, ask=ask)

        volume = float(request.get("volume") or 0)
        if volume < 0.01 or volume > 100:
            return _result(TRADE_RETCODE_INVALID_VOLUME, "Invalid volume", bid=bid, ask=ask)

        if action == TRADE_ACTION_PENDING:
            ticket = _ticket()
            _S["orders"][ticket] = dict(request, ticket=ticket, time=_now())
            return _result(TRADE_RETCODE_DONE, "Request executed", order=ticket, volume=volume,
                           price=request.get("price", 0.0), bid=bid, ask=ask)

        if action != TRADE_ACTION_DEAL:
            return _result(TRADE_RETCODE_INVALID, "Unsupported action", bid=bid, ask=ask)

        order_type = request.get("type")
        if request.get("position"):
            pos = _S["positions"].get(request["position"])
            if pos is None:
                return _result(TRADE_RETCODE_POSITION_CLOSED, "Position doesn't exist", bid=bid, ask=ask)
            long = pos["type"] == POSITION_TYPE_BUY
            price = _slipped(symbol, bid if long else ask, worse_upward=not long)
            deal, _ = _close(pos, min(volume, pos["volume"]), price, request.get("comment", ""))
            return _result(TRADE_RETCODE_DONE, "Request executed", order=deal, deal=deal,
                           volume=volume, price=price, bid=bid, ask=ask)

        if order_type not in (ORDER_TYPE_BUY, ORDER_TYPE_SELL):
            return _result(TRADE_RETCODE_INVALID, "Invalid order type", bid=bid, ask=ask)
        buy = order_type == ORDER_TYPE_BUY
        price = _slipped(symbol, ask if buy else bid, worse_upward=buy)
        sl, tp = request.get("sl") or 0.0, request.get("tp") or 0.0
        if not _stops_valid(buy, price, sl, tp):
            return _result(TRADE_RETCODE_INVALID_STOPS, "Invalid stops", bid=bid, ask=ask)
        ticket = _ticket()
        pos = dict(ticket=ticket, time=_now(), type=POSITION_TYPE_BUY if buy else POSITION_TYPE_SELL,
                   volume=volume, price_open=price, sl=round(sl, digits), tp=round(tp, digits),
                   symbol=symbol, comment=request.get("comment", ""), magic=request.get("magic", 0))
        _S["positions"][ticket] = pos
        deal = _deal(pos, DEAL_ENTRY_IN, volume, price, 0.0, pos["comment"])
        return _result(TRADE_RETCODE_DONE, "Request executed", order=ticket, deal=deal,
                       volume=volume, price=price, bid=bid, ask=ask)


def positions_get(symbol: str | None = None, group: str | None = None, ticket: int | None = None):
    with _lock:
        out = []
        for pos in _S["positions"].values():
            if ticket is not None and pos["ticket"] != ticket:
                continue
            if symbol is not None and pos["symbol"] != symbol:
                continue
            current, profit = _floating(pos)
            out.append(TradePosition(ticket=pos["ticket"], time=pos["time"], type=pos["type"],
                                     volume=pos["volume"], price_open=pos["price_open"], sl=pos["sl"],
                                     tp=pos["tp"], price_current=current, profit=profit,
                                     symbol=pos["symbol"], comment=pos["comment"], magic=pos["magic"]))
        return tuple(out)


def history_deals_get(date_from, date_to, group: str | None = None, **_kwargs):
    with _lock:
        start, end = _ts(date_from), _ts(date_to)
        return tuple(d for d in _S["deals"] if start <= d.time <= end)


_reset()
