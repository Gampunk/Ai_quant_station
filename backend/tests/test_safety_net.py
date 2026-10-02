"""
Step 13: the kill switch, one order at a time, the midnight equity baseline, and
heartbeat alerts.

Negative controls:
- In app/core/risk.py _submit_order_locked, delete the kill switch check.
  The "refuses every new order" test must fail.
- In submit_order, call _submit_order_locked without `async with order_lock()`.
  test_two_orders_at_once_are_checked_one_after_the_other must fail.
- In app/api/risk.py change_halt, delete the admin check for resuming.
  test_only_an_admin_resumes_and_must_say_why must fail.
- In app/core/heartbeat.py check_once, drop `and check not in self.down`.
  test_a_connector_outage_alerts_once_and_recovery_once must fail.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.api import autopilot
from app.core import alerts as alerts_module
from app.core import heartbeat as heartbeat_module
from app.core import risk
from app.core.config import settings
from app.core.mt5_connector import ConnectorError, connector_client
from app.models.ai_memory import AutopilotSettings
from app.models.risk import Alert, RiskDay, RiskDecision, TradingHalt
from tests.fake_connector import free_port, start_fake, stop_fake


@pytest.fixture(scope="module")
def fake_url():
    proc, url = start_fake(free_port())
    yield url
    stop_fake(proc)


@pytest.fixture
async def broker(fake_url, monkeypatch):
    monkeypatch.setattr(settings, "MT5_CONNECTOR_URL", fake_url)
    monkeypatch.setattr(settings, "MT5_API_TOKEN", "")
    await connector_client.initialize()
    yield
    for p in (await connector_client.get_positions()).get("positions", []):
        await connector_client.close_position(p["ticket"])


async def _stop_trading(client, headers, reason="test stop"):
    resp = await client.post("/api/risk/halt", headers=headers, json={"halted": True, "reason": reason})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _order(client, headers):
    q = await connector_client.get_symbol("XAUUSD")
    return await client.post("/api/trade/order", headers=headers, json={
        "symbol": "XAUUSD", "action": "BUY", "volume": 0.01, "sl": round(q["ask"] - 10, 2)})


# ── Kill switch ─────────────────────────────────────────────────────────────
async def test_the_kill_switch_refuses_every_new_order(broker, client: AsyncClient, trader_headers, db_session):
    assert (await _order(client, trader_headers)).status_code == 200
    await _stop_trading(client, trader_headers, "broker is misbehaving")

    resp = await _order(client, trader_headers)
    assert resp.status_code == 422 and "broker is misbehaving" in resp.json()["detail"]
    db_session.expire_all()
    refused = (await db_session.execute(
        select(RiskDecision).where(RiskDecision.reason_code == "trading_halted"))).scalars().all()
    assert len(refused) == 1


async def test_closing_still_works_while_trading_is_stopped(broker, client: AsyncClient, trader_headers):
    ticket = (await _order(client, trader_headers)).json()["ticket"]
    await _stop_trading(client, trader_headers)
    resp = await client.post("/api/trade/close", headers=trader_headers, json={"ticket": ticket})
    assert resp.status_code == 200, resp.text


async def test_who_may_stop_trading(client: AsyncClient, viewer_headers, trader_headers):
    resp = await client.post("/api/risk/halt", headers=viewer_headers, json={"halted": True, "reason": "x"})
    assert resp.status_code == 403
    await _stop_trading(client, trader_headers)
    state = (await client.get("/api/risk/halt", headers=viewer_headers)).json()
    assert state["halted"] is True and state["changed_by_name"] == "test_trader"


async def test_only_an_admin_resumes_and_must_say_why(client: AsyncClient, trader_headers, auth_headers, db_session):
    await _stop_trading(client, trader_headers)
    resume = {"halted": False, "reason": "all clear"}
    assert (await client.post("/api/risk/halt", headers=trader_headers, json=resume)).status_code == 403
    no_reason = await client.post("/api/risk/halt", headers=auth_headers, json={"halted": False})
    assert no_reason.status_code == 400
    assert (await client.post("/api/risk/halt", headers=auth_headers, json=resume)).status_code == 200
    db_session.expire_all()
    rows = (await db_session.execute(select(TradingHalt).order_by(TradingHalt.id))).scalars().all()
    assert [(r.halted, r.changed_by_name) for r in rows] == [(True, "test_trader"), (False, "admin")]


async def test_the_kill_switch_stops_a_running_autopilot(client: AsyncClient, trader_headers, db_session):
    db_session.add(AutopilotSettings(user_id=2, enabled=True))
    await db_session.commit()
    state = autopilot._get_state(2)
    state.update(enabled=True, running=True, task=asyncio.create_task(asyncio.sleep(3600)))
    task = state["task"]

    body = await _stop_trading(client, trader_headers)
    assert 2 in body["autopilots_stopped"]
    assert task.cancelled() and state["enabled"] is False
    db_session.expire_all()
    row = (await db_session.execute(select(AutopilotSettings).where(AutopilotSettings.user_id == 2))).scalar_one()
    assert row.enabled is False, "it would restart at the next boot"
    autopilot._user_states.pop(2, None)


async def test_no_autopilot_starts_or_restarts_while_stopped(client: AsyncClient, trader_headers, db_session):
    db_session.add(AutopilotSettings(user_id=2, enabled=True))
    await db_session.commit()
    await _stop_trading(client, trader_headers)
    assert (await client.post("/api/autopilot/start", headers=trader_headers)).status_code == 409
    db_session.add(AutopilotSettings(user_id=1, enabled=True))
    await db_session.commit()
    assert await autopilot._start_autopilot_internal(1) is False
    autopilot._user_states.pop(1, None)
    autopilot._user_states.pop(2, None)


async def test_close_all_needs_the_typed_phrase(broker, client: AsyncClient, trader_headers):
    for _ in range(2):
        assert (await _order(client, trader_headers)).status_code == 200
    wrong = await client.post("/api/risk/close-all", headers=trader_headers, json={"confirm": "yes"})
    assert wrong.status_code == 400
    assert len((await connector_client.get_positions())["positions"]) == 2

    resp = await client.post("/api/risk/close-all", headers=trader_headers, json={"confirm": "CLOSE ALL"})
    assert resp.status_code == 200 and len(resp.json()["closed"]) == 2 and not resp.json()["failed"]
    assert (await connector_client.get_positions())["positions"] == []


async def test_viewers_cannot_close_everything(client: AsyncClient, viewer_headers):
    resp = await client.post("/api/risk/close-all", headers=viewer_headers, json={"confirm": "CLOSE ALL"})
    assert resp.status_code == 403


# ── One order at a time (finding 24) ────────────────────────────────────────
async def test_two_orders_at_once_are_checked_one_after_the_other(broker, client: AsyncClient, auth_headers,
                                                                  trader_headers, monkeypatch):
    resp = await client.put("/api/risk/settings", headers=auth_headers,
                            json={"reason": "test", "max_open_positions": 1})
    assert resp.status_code == 200
    real_place = connector_client.place_order

    async def slow_place(payload):
        await asyncio.sleep(0.5)  # widen the window between the check and the position appearing
        return await real_place(payload)
    monkeypatch.setattr(connector_client, "place_order", slow_place)

    first, second = await asyncio.gather(_order(client, trader_headers), _order(client, trader_headers))
    assert sorted([first.status_code, second.status_code]) == [200, 422], (first.text, second.text)
    assert len((await connector_client.get_positions())["positions"]) == 1


# ── Starting equity at 00:00 UTC (finding 25) ──────────────────────────────
async def test_midnight_records_the_day_and_orders_use_it(broker, db_session):
    equity = await risk.record_day_start()
    assert equity
    db_session.expire_all()
    [row] = (await db_session.execute(select(RiskDay))).scalars().all()
    assert row.source == "midnight" and row.start_equity == equity
    async with risk.AsyncSessionLocal() as db:
        account = await connector_client.get_account()
        assert await risk.start_of_day_equity(db, account) == equity
        assert await risk.day_start_source(db, account) == "midnight"


async def test_a_missed_midnight_falls_back_to_the_first_check(broker, client: AsyncClient, viewer_headers):
    status = (await client.get("/api/risk/status", headers=viewer_headers)).json()
    assert status["start_of_day_source"] == "first_check" and status["start_of_day_equity"]


async def test_midnight_with_the_connector_down_records_nothing(db_session, monkeypatch):
    async def down():
        raise ConnectorError(503, "connector unreachable")
    monkeypatch.setattr(connector_client, "get_account", down)
    assert await risk.record_day_start() is None
    db_session.expire_all()
    assert (await db_session.execute(select(RiskDay))).scalars().all() == []


# ── Heartbeat alerts ────────────────────────────────────────────────────────
@pytest.fixture
def sent(monkeypatch):
    """Capture Telegram messages instead of sending them."""
    messages = []

    async def fake_send(text):
        messages.append(text)
        return True
    monkeypatch.setattr(alerts_module, "send_telegram", fake_send)
    return messages


async def test_a_connector_outage_alerts_once_and_recovery_once(sent, monkeypatch, db_session):
    monkeypatch.setattr(settings, "MT5_CONNECTOR_URL", "http://127.0.0.1:9")  # every call below is faked
    beat = heartbeat_module.Heartbeat()
    healthy = {"value": False}

    async def health():
        if not healthy["value"]:
            raise ConnectorError(503, "connection refused")
        return {"mt5_connected": True}
    monkeypatch.setattr(connector_client, "health", health)

    async def closed():
        return False
    monkeypatch.setattr(autopilot, "_is_market_open", closed)

    assert await beat.check_once() == [], "one failed check is not yet an outage"
    assert len(await beat.check_once()) == 1
    for _ in range(3):
        assert await beat.check_once() == [], "an ongoing outage must not alert again every minute"
    healthy["value"] = True
    [recovered] = await beat.check_once()
    assert recovered.state == "up"
    assert len(sent) == 2 and "unreachable" in sent[0] and "back" in sent[1]
    db_session.expire_all()
    rows = (await db_session.execute(select(Alert).order_by(Alert.id))).scalars().all()
    assert [(r.check, r.state, r.delivered) for r in rows] == [("connector", "down", True), ("connector", "up", True)]


async def test_quiet_prices_alert_only_after_ten_minutes_of_market_hours(sent, monkeypatch):
    monkeypatch.setattr(settings, "MT5_CONNECTOR_URL", "http://127.0.0.1:9")  # every call below is faked

    async def health():
        return {"mt5_connected": True}

    async def clock():
        return {"live": False, "offset_hours": None}

    async def market_open():
        return True
    monkeypatch.setattr(connector_client, "health", health)
    monkeypatch.setattr(connector_client, "clock", clock)
    monkeypatch.setattr(autopilot, "_is_market_open", market_open)
    beat = heartbeat_module.Heartbeat()
    for _ in range(9):
        assert await beat.check_once() == []
    [alert] = await beat.check_once()
    assert alert.check == "prices" and alert.state == "down"


async def test_a_stalled_autopilot_alerts(sent, db_session, monkeypatch):
    monkeypatch.setattr(settings, "MT5_CONNECTOR_URL", "")  # only the autopilot check runs
    db_session.add(AutopilotSettings(user_id=3, enabled=True, interval_seconds=300))
    await db_session.commit()
    state = autopilot._get_state(3)
    now = datetime.now(timezone.utc)
    state.update(enabled=True, running=True, task=asyncio.create_task(asyncio.sleep(3600)),
                 last_beat=now - timedelta(minutes=5))
    beat = heartbeat_module.Heartbeat()
    try:
        assert await beat.check_once(now) == [], "five minutes on a five-minute interval is normal"
        [alert] = await beat.check_once(now + timedelta(minutes=10))
        assert alert.check == "autopilot:3" and "15 minutes" in alert.message
    finally:
        state["task"].cancel()
        autopilot._user_states.pop(3, None)


async def test_without_telegram_settings_nothing_is_sent(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "")
    assert await alerts_module.send_telegram("hello") is False


async def test_alerts_are_listed_for_the_page(sent, client: AsyncClient, viewer_headers):
    await alerts_module.raise_alert("connector", "down", "The MT5 connector is unreachable")
    body = (await client.get("/api/risk/alerts", headers=viewer_headers)).json()
    assert body["alerts"][0]["message"] == "The MT5 connector is unreachable"
