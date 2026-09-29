"""Tests for backlog items A + B.

A — trade ↔ analysis linking: auto-attach the prompting chat to every
    trade_record (explicit Execute-Trade id, or latest same-symbol
    assistant analysis within 24h for Terminal orders), plus the connector
    order/close paths that make Terminal trading work on Linux hosts.
B — profit reconciler: close out trade_records whose MT5 positions were
    closed externally (SL/TP, manual terminal closes) so profit_loss
    reaches the RAG score.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.trade_reconcile import (
    _parse_deal_time,
    pair_close_deals,
    reconcile_trade_records,
)
from app.models.ai_memory import ChatMemory, TradeRecord
from app.models.schemas import OrderRequest
from app.services import trade_service
from app.services.trade_service import TradeError, _resolve_chat_link, _use_connector

ADMIN_USER_ID = 1


def _close_deal(pid, profit, price=2650.5, time="2026-09-28 10:00:00", comment="[sl]"):
    return {
        "position_id": pid, "entry": "CLOSE", "symbol": "XAUUSD",
        "direction": "SELL", "volume": 0.1, "price": price, "profit": profit,
        "swap": 0.0, "commission": 0.0, "comment": comment, "time": time,
    }


def _open_deal(pid):
    return {
        "position_id": pid, "entry": "OPEN", "symbol": "XAUUSD",
        "direction": "BUY", "volume": 0.1, "price": 2640.0, "profit": 0.0,
        "swap": 0.0, "commission": 0.0, "comment": "", "time": "2026-09-27 09:00:00",
    }


async def _make_open_trade(db, ticket, symbol="XAUUSD", executed_at=None):
    trade = TradeRecord(
        user_id=ADMIN_USER_ID,
        symbol=symbol,
        direction="BUY",
        entry_price=2640.0,
        volume=0.1,
        status="open",
        mt5_ticket=ticket,
        executed_at=executed_at or datetime.now(timezone.utc) - timedelta(hours=2),
        comment="[IMPULSE_V2]",
    )
    db.add(trade)
    await db.commit()
    return trade


async def _make_chat(db, symbol, hours_ago=1, role="assistant", user_id=ADMIN_USER_ID):
    chat = ChatMemory(
        user_id=user_id,
        symbol=symbol,
        role=role,
        content="Gold bullish above 2640.",
        created_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    )
    db.add(chat)
    await db.commit()
    return chat


# ── B: pure deal-pairing helpers ────────────────────────────────────────────

class TestPairCloseDeals:
    def test_ignores_open_deals(self):
        assert pair_close_deals([_open_deal(100)]) == {}

    def test_single_close(self):
        closes = pair_close_deals([_open_deal(100), _close_deal(100, 12.5)])
        assert closes[100]["profit"] == 12.5
        assert closes[100]["price"] == 2650.5
        assert closes[100]["time"] == "2026-09-28 10:00:00"

    def test_partial_closes_sum_profit_keep_latest_price(self):
        deals = [
            _open_deal(200),
            _close_deal(200, 10.0, price=2650.0, time="2026-09-28 10:00:00"),
            _close_deal(200, 5.0, price=2655.0, time="2026-09-28 11:00:00"),
        ]
        closes = pair_close_deals(deals)
        assert closes[200]["profit"] == 15.0
        assert closes[200]["price"] == 2655.0
        assert closes[200]["time"] == "2026-09-28 11:00:00"

    def test_deal_without_position_id_skipped(self):
        deal = _close_deal(None, 3.0)
        assert pair_close_deals([deal]) == {}

    def test_multiple_positions_kept_separate(self):
        closes = pair_close_deals([_close_deal(1, 1.0), _close_deal(2, -2.0)])
        assert closes[1]["profit"] == 1.0
        assert closes[2]["profit"] == -2.0


class TestParseDealTime:
    def test_string_format(self):
        dt = _parse_deal_time("2026-09-28 10:30:00")
        assert dt == datetime(2026, 9, 28, 10, 30, tzinfo=timezone.utc)

    def test_naive_datetime_gets_utc(self):
        dt = _parse_deal_time(datetime(2026, 9, 28, 10, 30))
        assert dt.tzinfo is not None
        assert dt.utcoffset().total_seconds() == 0

    def test_aware_datetime_passthrough(self):
        aware = datetime(2026, 9, 28, 10, 30, tzinfo=timezone.utc)
        assert _parse_deal_time(aware) is aware

    def test_epoch_seconds(self):
        dt = _parse_deal_time(0)
        assert dt == datetime(1970, 1, 1, tzinfo=timezone.utc)

    def test_garbage_returns_none(self):
        assert _parse_deal_time("not-a-date") is None
        assert _parse_deal_time(None) is None


# ── B: reconciler against the DB ────────────────────────────────────────────

class TestReconcileTradeRecords:
    async def test_closes_open_trade_with_close_deal(self, db_session, monkeypatch):
        await _make_open_trade(db_session, ticket=111)

        async def fake_fetch_deals(hours):
            return [_open_deal(111), _close_deal(111, -7.25, price=2635.0)]

        monkeypatch.setattr("app.core.trade_reconcile.fetch_deals", fake_fetch_deals)

        updated = await reconcile_trade_records()
        assert updated == 1

        db_session.expire_all()
        trade = (await db_session.execute(
            select(TradeRecord).where(TradeRecord.mt5_ticket == 111)
        )).scalar_one()
        assert trade.status == "closed"
        assert trade.profit_loss == -7.25
        assert trade.exit_price == 2635.0
        assert trade.closed_at is not None
        # SQLite hands datetimes back naive; the parser must re-attach UTC
        assert _parse_deal_time(trade.closed_at).tzinfo is not None

    async def test_sums_partial_closes(self, db_session, monkeypatch):
        await _make_open_trade(db_session, ticket=222)

        async def fake_fetch_deals(hours):
            return [
                _open_deal(222),
                _close_deal(222, 4.0, price=2650.0, time="2026-09-28 10:00:00"),
                _close_deal(222, 6.0, price=2652.0, time="2026-09-28 11:00:00"),
            ]

        monkeypatch.setattr("app.core.trade_reconcile.fetch_deals", fake_fetch_deals)

        assert await reconcile_trade_records() == 1

        db_session.expire_all()
        trade = (await db_session.execute(
            select(TradeRecord).where(TradeRecord.mt5_ticket == 222)
        )).scalar_one()
        assert trade.profit_loss == 10.0
        assert trade.exit_price == 2652.0

    async def test_unmatched_ticket_stays_open(self, db_session, monkeypatch):
        await _make_open_trade(db_session, ticket=333)

        async def fake_fetch_deals(hours):
            return [_close_deal(999, 5.0)]

        monkeypatch.setattr("app.core.trade_reconcile.fetch_deals", fake_fetch_deals)

        assert await reconcile_trade_records() == 0

        db_session.expire_all()
        trade = (await db_session.execute(
            select(TradeRecord).where(TradeRecord.mt5_ticket == 333)
        )).scalar_one()
        assert trade.status == "open"
        assert trade.profit_loss is None

    async def test_unreachable_mt5_leaves_records_alone(self, db_session, monkeypatch):
        await _make_open_trade(db_session, ticket=444)

        async def fake_fetch_deals(hours):
            return None

        monkeypatch.setattr("app.core.trade_reconcile.fetch_deals", fake_fetch_deals)

        assert await reconcile_trade_records() == 0

        db_session.expire_all()
        trade = (await db_session.execute(
            select(TradeRecord).where(TradeRecord.mt5_ticket == 444)
        )).scalar_one()
        assert trade.status == "open"

    async def test_already_closed_record_ignored(self, db_session, monkeypatch):
        trade = await _make_open_trade(db_session, ticket=555)
        trade.status = "closed"
        trade.profit_loss = 1.0
        await db_session.commit()

        calls = []

        async def fake_fetch_deals(hours):
            calls.append(hours)

        monkeypatch.setattr("app.core.trade_reconcile.fetch_deals", fake_fetch_deals)

        assert await reconcile_trade_records() == 0
        assert calls == []

    async def test_record_without_ticket_ignored(self, db_session, monkeypatch):
        await _make_open_trade(db_session, ticket=None)

        calls = []

        async def fake_fetch_deals(hours):
            calls.append(hours)

        monkeypatch.setattr("app.core.trade_reconcile.fetch_deals", fake_fetch_deals)

        assert await reconcile_trade_records() == 0
        assert calls == []

    async def test_lookback_grows_with_trade_age(self, db_session, monkeypatch):
        await _make_open_trade(
            db_session, ticket=666,
            executed_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
        seen = {}

        async def fake_fetch_deals(hours):
            seen["hours"] = hours
            return []

        monkeypatch.setattr("app.core.trade_reconcile.fetch_deals", fake_fetch_deals)

        assert await reconcile_trade_records() == 0
        assert seen["hours"] >= 10 * 24 + 24


# ── A: chat ↔ trade auto-linking ────────────────────────────────────────────

class TestResolveChatLink:
    async def test_explicit_id_wins(self):
        assert await _resolve_chat_link(ADMIN_USER_ID, "XAUUSD", 42) == 42

    async def test_no_user_returns_none(self):
        assert await _resolve_chat_link(None, "XAUUSD", None) is None

    async def test_no_symbol_returns_none(self):
        assert await _resolve_chat_link(ADMIN_USER_ID, "", None) is None

    async def test_links_latest_same_symbol_analysis(self, db_session):
        chat = await _make_chat(db_session, "XAUUSD")
        assert await _resolve_chat_link(ADMIN_USER_ID, "XAUUSD", None) == chat.id

    async def test_matches_broker_suffix_both_directions(self, db_session):
        chat = await _make_chat(db_session, "XAUUSD")
        assert await _resolve_chat_link(ADMIN_USER_ID, "XAUUSD.p", None) == chat.id

        chat2 = await _make_chat(db_session, "EURUSD.s")
        assert await _resolve_chat_link(ADMIN_USER_ID, "EURUSD", None) == chat2.id

    async def test_different_symbol_not_linked(self, db_session):
        await _make_chat(db_session, "EURUSD")
        assert await _resolve_chat_link(ADMIN_USER_ID, "XAUUSD", None) is None

    async def test_analysis_older_than_24h_not_linked(self, db_session):
        await _make_chat(db_session, "XAUUSD", hours_ago=48)
        assert await _resolve_chat_link(ADMIN_USER_ID, "XAUUSD", None) is None

    async def test_user_messages_not_linked(self, db_session):
        await _make_chat(db_session, "XAUUSD", role="user")
        assert await _resolve_chat_link(ADMIN_USER_ID, "XAUUSD", None) is None

    async def test_other_users_analysis_not_linked(self, db_session):
        await _make_chat(db_session, "XAUUSD", user_id=2)
        assert await _resolve_chat_link(ADMIN_USER_ID, "XAUUSD", None) is None

    async def test_latest_analysis_wins(self, db_session):
        old = await _make_chat(db_session, "XAUUSD", hours_ago=20)
        new = await _make_chat(db_session, "XAUUSD", hours_ago=1)
        assert await _resolve_chat_link(ADMIN_USER_ID, "XAUUSD", None) == new.id
        assert new.id != old.id


# ── A: connector routing ────────────────────────────────────────────────────

class TestUseConnector:
    def test_explicit_flag_with_url(self, monkeypatch):
        monkeypatch.setattr(settings, "MT5_USE_EXTERNAL_CONNECTOR", True)
        monkeypatch.setattr(settings, "MT5_CONNECTOR_URL", "http://x:5001")
        assert _use_connector() is True

    def test_flag_without_url_falls_through(self, monkeypatch):
        monkeypatch.setattr(settings, "MT5_USE_EXTERNAL_CONNECTOR", True)
        monkeypatch.setattr(settings, "MT5_CONNECTOR_URL", "")
        assert _use_connector() is False

    def test_autodetect_when_mt5_package_missing(self, monkeypatch):
        monkeypatch.setattr(settings, "MT5_USE_EXTERNAL_CONNECTOR", False)
        monkeypatch.setattr(settings, "MT5_CONNECTOR_URL", "http://x:5001")
        monkeypatch.setattr(trade_service, "_get_mt5", lambda: None)
        assert _use_connector() is True

    def test_direct_when_mt5_package_available(self, monkeypatch):
        monkeypatch.setattr(settings, "MT5_USE_EXTERNAL_CONNECTOR", False)
        monkeypatch.setattr(settings, "MT5_CONNECTOR_URL", "http://x:5001")
        monkeypatch.setattr(trade_service, "_get_mt5", lambda: object())
        assert _use_connector() is False

    def test_no_url_no_connector(self, monkeypatch):
        monkeypatch.setattr(settings, "MT5_CONNECTOR_URL", "")
        assert _use_connector() is False


# ── A: connector order / close paths ────────────────────────────────────────

def _patch_connector(monkeypatch, *, symbol_info=None, place=None,
                     positions=None, close=None):
    monkeypatch.setattr(trade_service, "_use_connector", lambda: True)

    async def fake_get_symbol(sym):
        return symbol_info if symbol_info is not None else {}

    async def fake_place(payload):
        return place

    async def fake_get_positions():
        return {"success": True, "positions": positions or []}

    async def fake_close(ticket, volume):
        return close

    monkeypatch.setattr(trade_service.connector_client, "get_symbol", fake_get_symbol)
    monkeypatch.setattr(trade_service.connector_client, "place_order", fake_place)
    monkeypatch.setattr(trade_service.connector_client, "get_positions", fake_get_positions)
    monkeypatch.setattr(trade_service.connector_client, "close_position", fake_close)


class TestConnectorOrderPath:
    async def test_order_saves_record_and_links_chat(self, db_session, monkeypatch):
        chat = await _make_chat(db_session, "XAUUSD")
        expected_chat_id = chat.id
        _patch_connector(
            monkeypatch,
            symbol_info={"volume_min": 0.01, "ask": 2641.0, "bid": 2640.0},
            place={"success": True, "ticket": 555, "volume": 0.1, "price": 2641.2,
                   "sl": 2630.0, "tp": 2660.0, "comment": "filled"},
        )

        order = OrderRequest(symbol="XAUUSD", action="BUY", volume=0.1,
                             sl=2630.0, tp=2660.0)
        result = await trade_service.place_order(order, ADMIN_USER_ID)

        assert result["success"] is True
        assert result["ticket"] == 555
        assert result["price"] == 2641.2

        db_session.expire_all()
        trade = (await db_session.execute(
            select(TradeRecord).where(TradeRecord.mt5_ticket == 555)
        )).scalar_one()
        assert trade.status == "open"
        assert trade.symbol == "XAUUSD"
        assert trade.entry_price == 2641.2
        assert trade.stop_loss == 2630.0
        assert trade.take_profit == 2660.0
        assert trade.ai_message == str(expected_chat_id)

    async def test_order_volume_below_broker_minimum_rejected(self, db_session, monkeypatch):
        _patch_connector(monkeypatch, symbol_info={"volume_min": 0.10})
        order = OrderRequest(symbol="XAUUSD", action="BUY", volume=0.01)
        with pytest.raises(TradeError, match="below minimum"):
            await trade_service.place_order(order, ADMIN_USER_ID)

    async def test_connector_rejection_raises_trade_error(self, db_session, monkeypatch):
        _patch_connector(monkeypatch, place={"success": False, "error": "no liquidity"})
        order = OrderRequest(symbol="XAUUSD", action="BUY", volume=0.1)
        with pytest.raises(TradeError, match="no liquidity"):
            await trade_service.place_order(order, ADMIN_USER_ID)

    async def test_invalid_action_rejected_before_connector(self, monkeypatch):
        _patch_connector(monkeypatch)
        order = OrderRequest(symbol="XAUUSD", action="HOLD", volume=0.1)
        with pytest.raises(TradeError, match="Invalid action"):
            await trade_service.place_order(order, ADMIN_USER_ID)


class TestConnectorClosePath:
    async def test_close_writes_profit_from_history(self, db_session, monkeypatch):
        await _make_open_trade(db_session, ticket=555)
        _patch_connector(
            monkeypatch,
            positions=[{"ticket": 555, "symbol": "XAUUSD", "volume": 0.1,
                        "direction": "BUY", "tp": 2660.0}],
            close={"success": True, "ticket": 555, "closed_volume": 0.1,
                   "close_price": 2650.0, "comment": "ok"},
        )

        async def fake_fetch_position_close(ticket, hours=2):
            assert ticket == 555
            return {"profit": 12.5, "price": 2650.5,
                    "time": "2026-09-28 12:00:00"}

        monkeypatch.setattr(trade_service, "fetch_position_close", fake_fetch_position_close)

        result = await trade_service.close_position(555, None, ADMIN_USER_ID)
        assert result["success"] is True
        assert result["close_price"] == 2650.5

        db_session.expire_all()
        trade = (await db_session.execute(
            select(TradeRecord).where(TradeRecord.mt5_ticket == 555)
        )).scalar_one()
        assert trade.status == "closed"
        assert trade.profit_loss == 12.5
        assert trade.exit_price == 2650.5

    async def test_close_unknown_position_raises(self, monkeypatch):
        _patch_connector(monkeypatch, positions=[])
        with pytest.raises(TradeError, match="not found"):
            await trade_service.close_position(9999, None, ADMIN_USER_ID)

    async def test_close_volume_exceeding_position_rejected(self, monkeypatch):
        _patch_connector(
            monkeypatch,
            positions=[{"ticket": 555, "symbol": "XAUUSD", "volume": 0.1}],
        )
        with pytest.raises(TradeError, match="exceeds"):
            await trade_service.close_position(555, 0.5, ADMIN_USER_ID)

