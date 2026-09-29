"""Default SL/TP fill-in: no trade is ever placed naked.

0.2% of price is applied whenever a request arrives with a missing or zero
stop-loss / take-profit, on all three execution paths: autopilot,
Terminal (direct MT5 and connector), and AI-Analyst Execute-Trade.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.utils import DEFAULT_SL_TP_PCT, apply_default_sl_tp
from app.models.ai_memory import TradeRecord
from app.models.schemas import OrderRequest
from app.services import trade_service
from app.services.trade_service import place_order

ADMIN_USER_ID = 1
PCT = DEFAULT_SL_TP_PCT  # 0.002


# ── helper (pure) ───────────────────────────────────────────────────────────

class TestApplyDefaultSlTp:
    def test_buy_missing_both(self):
        sl, tp = apply_default_sl_tp("BUY", 2641.0, None, None, digits=2)
        assert sl == round(2641.0 * (1 - PCT), 2)
        assert tp == round(2641.0 * (1 + PCT), 2)
        assert sl < 2641.0 < tp

    def test_sell_missing_both_mirrored(self):
        sl, tp = apply_default_sl_tp("SELL", 2641.0, None, None, digits=2)
        assert sl == round(2641.0 * (1 + PCT), 2)
        assert tp == round(2641.0 * (1 - PCT), 2)
        assert sl > 2641.0 > tp

    def test_only_sl_missing(self):
        sl, tp = apply_default_sl_tp("BUY", 2641.0, None, 2700.0, digits=2)
        assert sl == round(2641.0 * (1 - PCT), 2)
        assert tp == 2700.0

    def test_only_tp_missing(self):
        sl, tp = apply_default_sl_tp("BUY", 2641.0, 2600.0, None, digits=2)
        assert sl == 2600.0
        assert tp == round(2641.0 * (1 + PCT), 2)

    def test_zero_values_are_treated_as_missing(self):
        # AI models frequently return stop_loss: 0.0 / take_profit: 0.0
        sl, tp = apply_default_sl_tp("BUY", 2641.0, 0.0, 0.0, digits=2)
        assert sl == round(2641.0 * (1 - PCT), 2)
        assert tp == round(2641.0 * (1 + PCT), 2)

    def test_values_present_left_untouched(self):
        sl, tp = apply_default_sl_tp("BUY", 2641.0, 2600.0, 2700.0, digits=2)
        assert sl == 2600.0
        assert tp == 2700.0

    def test_rounding_to_digits(self):
        sl, tp = apply_default_sl_tp("BUY", 1.10000, None, None, digits=5)
        assert sl == round(1.10000 * (1 - PCT), 5)
        assert tp == round(1.10000 * (1 + PCT), 5)

    def test_no_digits_keeps_full_precision(self):
        sl, tp = apply_default_sl_tp("BUY", 2641.0, None, None)
        assert sl == pytest.approx(2641.0 * (1 - PCT))

    def test_missing_price_passes_through(self):
        assert apply_default_sl_tp("BUY", None, None, None) == (None, None)
        assert apply_default_sl_tp("BUY", 0, None, None) == (None, None)

    def test_pending_action_uses_side(self):
        # SELL_LIMIT must get SELL-side stops even though it's a limit order
        sl, tp = apply_default_sl_tp("SELL_LIMIT", 2641.0, None, None, digits=2)
        assert sl > 2641.0 > tp
        sl, tp = apply_default_sl_tp("BUY_STOP", 2641.0, None, None, digits=2)
        assert sl < 2641.0 < tp


# ── path 1: Terminal / Execute-Trade via the connector (Linux production) ──

def _patch_connector(monkeypatch, *, symbol_info=None):
    """Local copy of the A-test connector patch that captures the payload."""
    monkeypatch.setattr(trade_service, "_use_connector", lambda: True)
    captured = {}

    async def fake_get_symbol(sym):
        return symbol_info if symbol_info is not None else {}

    async def fake_place(payload):
        captured.update(payload)
        si = symbol_info or {}
        return {
            "success": True, "ticket": 555, "volume": payload["volume"],
            "price": payload.get("price") or si.get("ask") or 2641.0,
            "sl": payload.get("sl"), "tp": payload.get("tp"), "comment": "filled",
        }

    monkeypatch.setattr(trade_service.connector_client, "get_symbol", fake_get_symbol)
    monkeypatch.setattr(trade_service.connector_client, "place_order", fake_place)
    return captured


class TestConnectorPathDefaults:
    async def test_market_order_without_sl_tp_gets_defaults(self, db_session, monkeypatch):
        captured = _patch_connector(
            monkeypatch,
            symbol_info={"volume_min": 0.01, "ask": 2641.0, "bid": 2640.0, "digits": 2},
        )

        order = OrderRequest(symbol="XAUUSD", action="BUY", volume=0.1)
        result = await place_order(order, ADMIN_USER_ID)

        assert result["success"] is True
        # ref price = ask for BUY
        assert captured["sl"] == round(2641.0 * (1 - PCT), 2)
        assert captured["tp"] == round(2641.0 * (1 + PCT), 2)

        db_session.expire_all()
        trade = (await db_session.execute(
            select(TradeRecord).where(TradeRecord.mt5_ticket == 555)
        )).scalar_one()
        assert trade.stop_loss == round(2641.0 * (1 - PCT), 2)
        assert trade.take_profit == round(2641.0 * (1 + PCT), 2)

    async def test_sell_uses_bid_and_mirrored_defaults(self, db_session, monkeypatch):
        captured = _patch_connector(
            monkeypatch,
            symbol_info={"volume_min": 0.01, "ask": 2641.0, "bid": 2640.0, "digits": 2},
        )

        order = OrderRequest(symbol="XAUUSD", action="SELL", volume=0.1)
        await place_order(order, ADMIN_USER_ID)

        assert captured["sl"] == round(2640.0 * (1 + PCT), 2)
        assert captured["tp"] == round(2640.0 * (1 - PCT), 2)

    async def test_only_missing_side_is_filled(self, db_session, monkeypatch):
        captured = _patch_connector(
            monkeypatch,
            symbol_info={"volume_min": 0.01, "ask": 2641.0, "bid": 2640.0, "digits": 2},
        )

        order = OrderRequest(symbol="XAUUSD", action="BUY", volume=0.1, sl=2600.0, tp=0.0)
        await place_order(order, ADMIN_USER_ID)

        assert captured["sl"] == 2600.0
        assert captured["tp"] == round(2641.0 * (1 + PCT), 2)

    async def test_no_symbol_info_leaves_values_untouched(self, db_session, monkeypatch):
        captured = _patch_connector(monkeypatch, symbol_info=None)

        order = OrderRequest(symbol="XAUUSD", action="BUY", volume=0.1)
        await place_order(order, ADMIN_USER_ID)

        assert "sl" not in captured
        assert "tp" not in captured


# ── path 2: direct MT5 (Windows) ───────────────────────────────────────────

class _FakeMT5:
    TRADE_RETCODE_DONE = 10009
    ORDER_TIME_GTC = 0

    def __init__(self):
        self.sent = None

    def initialize(self, **kwargs):
        return True

    def order_send(self, request):
        self.sent = request
        return SimpleNamespace(retcode=10009, order=777, comment="ok")


async def _fake_select_symbol(symbol):
    info = SimpleNamespace(volume_min=0.01, volume_step=0.01, point=0.01,
                           digits=2, trade_stops_level=0, filling_mode=2)
    tick = SimpleNamespace(ask=2641.0, bid=2640.0)
    return info, tick


class TestDirectPathDefaults:
    async def test_market_order_without_sl_tp_gets_defaults(self, db_session, monkeypatch):
        fake = _FakeMT5()
        monkeypatch.setattr(trade_service, "_use_connector", lambda: False)
        monkeypatch.setattr(trade_service, "_get_mt5", lambda: fake)
        monkeypatch.setattr(trade_service, "_select_symbol", _fake_select_symbol)

        order = OrderRequest(symbol="XAUUSD", action="BUY", volume=0.1)
        result = await place_order(order, ADMIN_USER_ID)

        assert result["success"] is True
        assert fake.sent["sl"] == round(2641.0 * (1 - PCT), 2)
        assert fake.sent["tp"] == round(2641.0 * (1 + PCT), 2)
        assert result["sl"] == fake.sent["sl"]
        assert result["tp"] == fake.sent["tp"]

        db_session.expire_all()
        trade = (await db_session.execute(
            select(TradeRecord).where(TradeRecord.mt5_ticket == 777)
        )).scalar_one()
        assert trade.stop_loss == fake.sent["sl"]
        assert trade.take_profit == fake.sent["tp"]

    async def test_provided_sl_tp_survive(self, db_session, monkeypatch):
        fake = _FakeMT5()
        monkeypatch.setattr(trade_service, "_use_connector", lambda: False)
        monkeypatch.setattr(trade_service, "_get_mt5", lambda: fake)
        monkeypatch.setattr(trade_service, "_select_symbol", _fake_select_symbol)

        order = OrderRequest(symbol="XAUUSD", action="BUY", volume=0.1,
                             sl=2600.0, tp=2700.0)
        await place_order(order, ADMIN_USER_ID)

        assert fake.sent["sl"] == 2600.0
        assert fake.sent["tp"] == 2700.0


# ── path 3: autopilot execute_trade ────────────────────────────────────────

async def _run_autopilot_order(monkeypatch, *, direction="BUY", sl=None, tp=None):
    """Patch autopilot's HTTP layer, run execute_trade, return (result, payload)."""
    from app.api import autopilot

    captured = {}

    async def fake_async_request(method, url, **kwargs):
        if method == "GET":
            return {"bid": 2640.0, "ask": 2641.0, "point": 0.01, "digits": 2,
                    "trade_stops_level": 100}
        captured.update(kwargs.get("json") or {})
        return {"success": True, "ticket": 42, "price": 2640.5}

    monkeypatch.setattr(autopilot, "async_request", fake_async_request)
    result = await autopilot.execute_trade(
        ADMIN_USER_ID, "XAUUSD", direction, 0.1, None, sl, tp,
        prompt_num=1, connector_url="http://x:5001",
    )
    return result, captured


class TestAutopilotPathDefaults:
    async def test_buy_without_sl_tp_gets_defaults(self, monkeypatch):
        result, payload = await _run_autopilot_order(monkeypatch, direction="BUY")
        assert result["success"] is True
        # ref price = bid (autopilot takes bid first)
        assert payload["sl"] == round(2640.0 * (1 - PCT), 2)
        assert payload["tp"] == round(2640.0 * (1 + PCT), 2)

    async def test_sell_defaults_mirrored(self, monkeypatch):
        result, payload = await _run_autopilot_order(monkeypatch, direction="SELL")
        assert result["success"] is True
        assert payload["sl"] == round(2640.0 * (1 + PCT), 2)
        assert payload["tp"] == round(2640.0 * (1 - PCT), 2)

    async def test_ai_zero_values_get_defaults(self, monkeypatch):
        result, payload = await _run_autopilot_order(
            monkeypatch, direction="BUY", sl=0.0, tp=0.0)
        assert result["success"] is True
        assert payload["sl"] == round(2640.0 * (1 - PCT), 2)
        assert payload["tp"] == round(2640.0 * (1 + PCT), 2)

    async def test_only_missing_side_is_filled(self, monkeypatch):
        result, payload = await _run_autopilot_order(
            monkeypatch, direction="BUY", sl=2600.0, tp=None)
        assert result["success"] is True
        assert payload["sl"] == 2600.0
        assert payload["tp"] == round(2640.0 * (1 + PCT), 2)
