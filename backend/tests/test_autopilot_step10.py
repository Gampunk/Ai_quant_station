"""
The autopilot loop, its daily brake, and real UTC from a broker whose clock runs
3 hours ahead, on the fake terminal.

Negative controls:
- in _loop_iteration (app/api/autopilot.py), let the exception out of the
  try in autopilot_loop by removing `except Exception`: test_an_error_does_not_end_the_loop fails
- in _daily_totals, count profit by executed_at instead of closed_at:
  test_profit_counts_on_the_day_the_trade_closed fails
- in core/mt5_connector.py, stop converting deal times: test_times_arrive_in_utc fails
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.api import autopilot
from app.core.broker_clock import broker_clock
from app.core.config import settings
from app.core.mt5_connector import connector_client
from app.models.ai_memory import AutopilotSettings, AutopilotTrade
from tests.fake_connector import free_port, start_fake, stop_fake

USER_ID = 1
NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


# ── Daily totals and the brake ───────────────────────────────────────────────
async def _trade(db, *, opened, closed=None, profit=None):
    t = AutopilotTrade(user_id=USER_ID, prompt_number=1, prompt_text="t", symbol="XAUUSD", direction="BUY",
                       lot_size=0.1, execution_status="executed", executed_at=opened, closed_at=closed,
                       profit=profit, result=None if closed is None else ("PROFIT" if profit > 0 else "LOSS"))
    db.add(t)
    await db.commit()
    return t


async def test_profit_counts_on_the_day_the_trade_closed(db_session):
    yesterday = NOW() - timedelta(days=1)
    await _trade(db_session, opened=yesterday, closed=NOW(), profit=-30.0)        # closed today
    await _trade(db_session, opened=NOW(), closed=None)                            # open
    await _trade(db_session, opened=yesterday, closed=yesterday, profit=-99.0)    # yesterday's
    opened, pnl = await autopilot._daily_totals(USER_ID)
    assert opened == 1 and pnl == -30.0


@pytest.fixture
def quiet_loop(monkeypatch):
    """Everything around a cycle made instant, and cycles counted instead of run."""
    calls = []

    async def fake_cycle(user_id, cycle_id=None):
        calls.append(user_id)

    async def nothing(*a, **k):
        return None

    async def live(*a, **k):
        return True

    monkeypatch.setattr(autopilot, "run_autopilot_cycle", fake_cycle)
    monkeypatch.setattr(autopilot, "sync_trade_results", nothing)
    monkeypatch.setattr(autopilot, "_has_live_ticks", live)  # the market is open
    monkeypatch.setattr(settings, "MT5_CONNECTOR_URL", "http://127.0.0.1:9")  # cycles are faked
    state = autopilot._get_state(USER_ID)
    state.update(enabled=True, running=True, paused_day=None, last_trade_time=None)
    state["stats"].update(paused_reason=None, stopped_reason=None, error_count=0)
    yield calls
    # Leave no running autopilot behind: later tests expect it off.
    autopilot._user_states.pop(USER_ID, None)


async def _settings(db, **values):
    db.add(AutopilotSettings(user_id=USER_ID, max_daily_loss=-50.0, cooldown_minutes=0, **values))
    await db.commit()


async def test_the_brake_pauses_and_resumes_the_next_day(db_session, quiet_loop):
    await _settings(db_session)
    loser = await _trade(db_session, opened=NOW(), closed=NOW(), profit=-60.0)
    await autopilot._loop_iteration(USER_ID)
    await autopilot._loop_iteration(USER_ID)
    state = autopilot._get_state(USER_ID)
    assert quiet_loop == [], "no cycle may run while paused"
    assert "Resumes at 00:00 UTC" in state["stats"]["paused_reason"]
    assert sum("Daily loss limit reached" in e["message"] for e in state["logs"]) == 1, "logged once, not every cycle"
    assert state["running"] is True, "it used to switch itself off for good"

    # A new UTC day: yesterday's loss no longer counts.
    loser.closed_at = NOW() - timedelta(days=1)
    await db_session.commit()
    state["paused_day"] = (NOW() - timedelta(days=1)).date().isoformat()
    await autopilot._loop_iteration(USER_ID)
    assert quiet_loop == [USER_ID] and state["stats"]["paused_reason"] is None


async def test_the_brake_can_be_switched_off(db_session, quiet_loop):
    await _settings(db_session, daily_loss_limit_enabled=False)
    await _trade(db_session, opened=NOW(), closed=NOW(), profit=-60.0)
    await autopilot._loop_iteration(USER_ID)
    assert quiet_loop == [USER_ID]


async def test_the_switch_is_saved_and_reported(client, trader_headers):
    resp = await client.post("/api/autopilot/settings", headers=trader_headers,
                             json={"max_daily_loss": -80, "daily_loss_limit_enabled": False})
    assert resp.status_code == 200, resp.text
    status = (await client.get("/api/autopilot/status", headers=trader_headers)).json()
    assert status["settings"]["daily_loss_limit_enabled"] is False


async def test_an_error_does_not_end_the_loop(db_session, quiet_loop, monkeypatch):
    await _settings(db_session, interval_seconds=1)
    attempts = []
    state = autopilot._get_state(USER_ID)

    async def flaky(user_id, cycle_id=None):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("provider returned garbage")
        state["enabled"] = False  # stop after the second cycle

    async def instant(_seconds):
        return None

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(autopilot, "run_autopilot_cycle", flaky)
    monkeypatch.setattr(autopilot, "sync_all_trades_from_mt5", nothing)
    monkeypatch.setattr(autopilot.asyncio, "sleep", instant)
    await asyncio.wait_for(autopilot.autopilot_loop(USER_ID), timeout=30)
    assert len(attempts) == 2, "the cycle after the error must still run"
    assert state["stats"]["error_count"] == 1
    assert any("provider returned garbage" in e["message"] for e in state["logs"])


async def test_a_dead_loop_shows_as_stopped():
    state = autopilot._get_state(USER_ID)
    state.update(enabled=True, running=True)

    async def dies():
        raise RuntimeError("boom")

    task = asyncio.ensure_future(dies())
    autopilot._watch_loop(USER_ID, task)
    with pytest.raises(RuntimeError):
        await task
    await asyncio.sleep(0)
    assert state["running"] is False
    assert "boom" in state["stats"]["stopped_reason"]


# ── A broker on UTC+3 ────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def fake_utc_plus_3():
    proc, url = start_fake(free_port(), "--server-offset", "3")
    yield url
    stop_fake(proc)


@pytest.fixture
async def broker_plus_3(fake_utc_plus_3, monkeypatch):
    monkeypatch.setattr(settings, "MT5_CONNECTOR_URL", fake_utc_plus_3)
    monkeypatch.setattr(settings, "MT5_API_TOKEN", "")
    monkeypatch.setattr(settings, "MT5_BROKER_UTC_OFFSET", 0)
    broker_clock.__init__()
    await connector_client.initialize()
    await connector_client.clock()             # first reading: prices have not moved yet
    await asyncio.sleep(1.2)
    broker_clock.observe(await connector_client.clock())
    assert broker_clock.offset_hours == 3.0 and broker_clock.source == "detected"
    yield
    for p in (await connector_client.get_positions()).get("positions", []):
        await connector_client.close_position(p["ticket"])
    broker_clock.__init__()


def _parse(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


async def test_times_arrive_in_utc(broker_plus_3):
    q = await connector_client.get_symbol("XAUUSD")
    placed = await autopilot.execute_trade(USER_ID, "XAUUSD", "BUY", sl=round(q["ask"] - 10, 2))
    assert placed["success"], placed
    pos = next(p for p in (await connector_client.get_positions())["positions"] if p["ticket"] == placed["ticket"])
    assert abs((_parse(pos["open_time"]) - NOW()).total_seconds()) < 120, pos["open_time"]
    deal = next(d for d in (await connector_client.get_history(hours=1))["deals"]
                if d["position_id"] == placed["ticket"])
    assert abs((_parse(deal["time"]) - NOW()).total_seconds()) < 120, deal["time"]


async def test_candles_arrive_in_utc(broker_plus_3):
    candles = (await connector_client.get_latest_data("XAUUSD", "15m", 4))["data"]
    last = datetime.fromtimestamp(candles[-1]["time"], timezone.utc)
    assert timedelta(0) <= NOW() - last < timedelta(minutes=16), last


async def test_autopilot_records_the_close_in_utc(broker_plus_3, db_session):
    """Finding 22: close times were stored three hours in the future."""
    q = await connector_client.get_symbol("XAUUSD")
    placed = await autopilot.execute_trade(USER_ID, "XAUUSD", "BUY", sl=round(q["ask"] - 10, 2))
    db_session.add(AutopilotSettings(user_id=USER_ID))
    db_session.add(AutopilotTrade(user_id=USER_ID, prompt_number=1, prompt_text="t", symbol="XAUUSD",
                                  direction="BUY", lot_size=placed["volume"], mt5_ticket=placed["ticket"],
                                  execution_status="executed", executed_at=NOW()))
    await db_session.commit()
    await connector_client.close_position(placed["ticket"])
    await autopilot.sync_trade_results(USER_ID)
    db_session.expire_all()
    trade = (await db_session.execute(select(AutopilotTrade).where(
        AutopilotTrade.mt5_ticket == placed["ticket"]))).scalar_one()
    closed = trade.closed_at if trade.closed_at.tzinfo else trade.closed_at.replace(tzinfo=timezone.utc)
    assert abs((closed - NOW()).total_seconds()) < 120, closed
    # A close sent by software: the broker's deal reason says "expert".
    assert trade.result == "EXPERT_CLOSE" and trade.exit_reason_source == "broker"
    assert 0 <= trade.duration_minutes <= 2


async def test_history_route_keeps_the_position_and_reason(broker_plus_3, client, viewer_headers):
    """The route's response model used to drop both, so nothing could match a deal to its trade."""
    q = await connector_client.get_symbol("XAUUSD")
    placed = await autopilot.execute_trade(USER_ID, "XAUUSD", "BUY", sl=round(q["ask"] - 10, 2))
    await connector_client.close_position(placed["ticket"])
    deals = (await client.get("/api/mt5/history", params={"hours": 1}, headers=viewer_headers)).json()["deals"]
    close = next(d for d in deals if d.get("position_id") == placed["ticket"] and d["entry"] == "CLOSE")
    assert close["reason"] == "expert"
    assert abs((_parse(close["time"]) - NOW()).total_seconds()) < 120
