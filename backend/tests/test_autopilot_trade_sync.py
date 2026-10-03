from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.api import autopilot
from app.core.config import settings


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value

    def scalar(self):
        return self.value


class _FakeDb:
    def __init__(self, settings, trade, cycle):
        self.settings = settings
        self.trade = trade
        self.cycle = cycle
        self.events = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, statement):
        sql = str(statement).lower()
        if "sum(" in sql:
            return _Result(0.0)
        if "autopilot_settings" in sql:
            return _Result(self.settings)
        if "autopilot_trades" in sql:
            return _Result([self.trade])
        if "autopilot_order_events" in sql:
            return _Result(None)
        raise AssertionError(f"Unexpected database query: {sql}")

    async def get(self, _model, _key):
        return self.cycle

    async def commit(self):
        return None

    def add(self, row):
        self.events.append(row)


@pytest.mark.asyncio
async def test_partial_close_stays_open_until_position_is_gone(monkeypatch):
    trade, cycle = _trade_and_cycle()
    db = _FakeDb(SimpleNamespace(), trade, cycle)

    async def fake_history(hours=0):
        return {"success": True, "deals": [
                _open_deal(), _close_deal(4.0, "2026-09-29 10:00:00")
            ]}

    async def fake_positions():
        return {"success": True, "positions": [{"ticket": 500}]}

    monkeypatch.setattr(autopilot, "AsyncSessionLocal", lambda: db)
    monkeypatch.setattr(settings, "MT5_CONNECTOR_URL", "http://127.0.0.1:9")  # every call is faked
    monkeypatch.setattr(autopilot.connector_client, "get_history", fake_history)
    monkeypatch.setattr(autopilot.connector_client, "get_positions", fake_positions)
    monkeypatch.setattr(autopilot, "add_log", lambda *_args, **_kwargs: None)

    assert await autopilot.sync_trade_results(17) == 0
    assert trade.result is None
    assert trade.profit is None
    assert cycle.realized_profit is None


@pytest.mark.asyncio
async def test_closed_position_aggregates_all_close_deals_and_costs(monkeypatch):
    trade, cycle = _trade_and_cycle()
    db = _FakeDb(SimpleNamespace(), trade, cycle)
    deals = [
        _open_deal(),
        _close_deal(10.0, "2026-09-29 10:00:00", swap=-1.0, commission=-0.2, reason="TP"),
        _close_deal(5.0, "2026-09-29 10:05:00", comment="tp", commission=-0.1, reason="TP"),
    ]

    async def fake_history(hours=0):
        return {"success": True, "deals": deals}

    async def fake_positions():
        return {"success": True, "positions": []}

    monkeypatch.setattr(autopilot, "AsyncSessionLocal", lambda: db)
    monkeypatch.setattr(settings, "MT5_CONNECTOR_URL", "http://127.0.0.1:9")  # every call is faked
    monkeypatch.setattr(autopilot.connector_client, "get_history", fake_history)
    monkeypatch.setattr(autopilot.connector_client, "get_positions", fake_positions)
    monkeypatch.setattr(autopilot, "add_log", lambda *_args, **_kwargs: None)

    assert await autopilot.sync_trade_results(18) == 1
    assert trade.profit == pytest.approx(13.7)
    assert trade.result == "TP_HIT"
    assert trade.exit_reason == "TP_HIT"
    assert trade.exit_reason_source == "broker"
    assert trade.closed_at == datetime(2026, 9, 29, 10, 5, tzinfo=timezone.utc)
    assert cycle.realized_profit == pytest.approx(13.7)
    assert cycle.exit_reason == "TP_HIT"


@pytest.mark.asyncio
async def test_partial_close_reasons_are_not_flattened(monkeypatch):
    trade, cycle = _trade_and_cycle()
    db = _FakeDb(SimpleNamespace(), trade, cycle)
    deals = [
        _open_deal(),
        _close_deal(8.0, "2026-09-29 10:00:00", reason="TP"),
        _close_deal(-3.0, "2026-09-29 10:05:00", reason="CLIENT"),
    ]

    async def fake_history(hours=0):
        return {"success": True, "deals": deals}

    async def fake_positions():
        return {"success": True, "positions": []}

    monkeypatch.setattr(autopilot, "AsyncSessionLocal", lambda: db)
    monkeypatch.setattr(settings, "MT5_CONNECTOR_URL", "http://127.0.0.1:9")  # every call is faked
    monkeypatch.setattr(autopilot.connector_client, "get_history", fake_history)
    monkeypatch.setattr(autopilot.connector_client, "get_positions", fake_positions)
    monkeypatch.setattr(autopilot, "add_log", lambda *_args, **_kwargs: None)

    assert await autopilot.sync_trade_results(19) == 1
    assert trade.result == "MIXED_EXIT"
    assert trade.exit_reason == "MIXED_EXIT"
    assert trade.exit_reason_source == "mixed"
    assert cycle.exit_reason == "MIXED_EXIT"


def _trade_and_cycle():
    trade = SimpleNamespace(
        # The broker's order ticket and position ID can differ.
        mt5_ticket=900,
        mt5_order_ticket=None,
        cycle_id="0123456789ab-cdef-0123-456789abcdef",
        executed_at=datetime.now(timezone.utc) - timedelta(hours=1),
        execution_status="executed",
        order_status=None,
        result=None,
        exit_reason=None,
        exit_reason_source=None,
        profit=None,
        exit_price=None,
        closed_at=None,
        duration_minutes=None,
        id=101,
        prompt_number=1,
        symbol="XAUUSD",
    )
    cycle = SimpleNamespace(
        trade_result=None,
        exit_reason=None,
        exit_reason_source=None,
        realized_profit=None,
        trade_closed_at=None,
        duration_minutes=None,
    )
    return trade, cycle


def _close_deal(profit, time, swap=0.0, commission=0.0, comment="", reason="UNKNOWN"):
    return {
        "entry": "CLOSE",
        "position_id": 500,
        "ticket": 600,
        "deal_ticket": 1600,
        "order_ticket": 600,
        "profit": profit,
        "swap": swap,
        "commission": commission,
        "price": 2500.0,
        "time": time,
        "comment": comment,
        "reason": reason,
        "reason_code": 5 if reason == "TP" else -1,
    }


def _open_deal():
    return {
        "entry": "OPEN",
        "position_id": 500,
        "ticket": 900,
        "deal_ticket": 1900,
        "order_ticket": 900,
        "comment": "[AUTOPILOT] P1 X0123456789ab",
        "time": "2026-09-29 09:00:00",
    }


def test_a_comment_mentioning_tp_does_not_make_a_target_hit():
    """Only the broker's deal reason counts (finding 10); upstream also guessed from the comment."""
    deals = [{"reason": "expert", "comment": "closed at tp zone", "profit": 3.0}]
    classification, exit_reason, source = autopilot._classify_exit_reason(deals, 3.0)
    assert classification == "EXPERT_CLOSE" and source == "broker"
    for comment, profit in (("sl moved", -2.0), ("tp hit", 4.0)):
        unknown = [{"reason": "", "comment": comment, "profit": profit}]
        classification, exit_reason, source = autopilot._classify_exit_reason(unknown, profit)
        assert exit_reason == "UNKNOWN" and source == "unavailable", comment
