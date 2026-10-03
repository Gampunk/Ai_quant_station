"""
The risk gate end to end: backend, real connector.py, fake MetaTrader5 terminal.

Covers changing the limits and their history, every source of orders going
through the gate, the autopilot's sizing, the daily loss baseline, and the
record of each decision.

Negative controls:
- in app/services/trade_service.py, call connector_client.place_order(payload)
  instead of submit_order: test_only_the_risk_gate_places_orders and the
  Terminal refusal tests fail
- in app/api/risk.py, change update_settings to depend on get_current_user:
  test_only_an_admin_changes_the_limits fails
"""
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.api import autopilot
from app.core.config import settings
from app.core.mt5_connector import connector_client
from app.models.risk import RiskDay, RiskDecision
from tests.fake_connector import free_port, start_fake, stop_fake

USER_ID = 1
APP = Path(__file__).resolve().parents[1] / "app"


@pytest.fixture(scope="module")
def fake_url():
    proc, url = start_fake(free_port())
    yield url
    stop_fake(proc)


@pytest.fixture(autouse=True)
async def _fake_and_flat(fake_url, monkeypatch):
    """Point the server at the fake, and leave no positions open between tests."""
    monkeypatch.setattr(settings, "MT5_CONNECTOR_URL", fake_url)
    monkeypatch.setattr(settings, "MT5_API_TOKEN", "")
    await connector_client.initialize()
    yield
    for p in (await connector_client.get_positions()).get("positions", []):
        await connector_client.close_position(p["ticket"])


async def _quote():
    return await connector_client.get_symbol("XAUUSD")


async def _decisions(db, **where):
    query = select(RiskDecision).order_by(RiskDecision.id)
    for key, value in where.items():
        query = query.where(getattr(RiskDecision, key) == value)
    db.expire_all()
    return (await db.execute(query)).scalars().all()


async def _set_limits(client, headers, reason="test", **values):
    resp = await client.put("/api/risk/settings", headers=headers, json={"reason": reason, **values})
    assert resp.status_code == 200, resp.text
    return resp.json()["settings"]


# ── Structure ────────────────────────────────────────────────────────────────
def test_only_the_risk_gate_places_orders():
    offenders = []
    for path in APP.rglob("*.py"):
        if path.name in ("risk.py", "mt5_connector.py") and path.parent.name == "core":
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"\.place_order\(", line) and not line.strip().startswith("#"):
                offenders.append(f"{path.relative_to(APP)}:{n}")
    assert offenders == [], f"orders sent around the risk gate: {offenders}"


# ── Settings ─────────────────────────────────────────────────────────────────
async def test_starting_values_are_the_agreed_ones(client: AsyncClient, viewer_headers):
    body = (await client.get("/api/risk/settings", headers=viewer_headers)).json()["settings"]
    assert body["autopilot_risk_pct"] == 1.0 and body["max_trade_risk_pct"] == 2.0
    assert body["max_open_positions"] == 0 and body["daily_loss_pct"] == 3.0
    assert body["min_margin_level"] == 200.0 and body["require_stop_loss"] is True


async def test_only_an_admin_changes_the_limits(client: AsyncClient, trader_headers, viewer_headers):
    for headers in (trader_headers, viewer_headers):
        resp = await client.put("/api/risk/settings", headers=headers, json={"reason": "x", "daily_loss_pct": 50})
        assert resp.status_code == 403


async def test_a_change_needs_a_reason_and_sane_values(client: AsyncClient, auth_headers):
    resp = await client.put("/api/risk/settings", headers=auth_headers, json={"reason": " ", "daily_loss_pct": 5})
    assert resp.status_code == 422 and "reason" in resp.json()["detail"]
    resp = await client.put("/api/risk/settings", headers=auth_headers, json={"reason": "x", "autopilot_risk_pct": 5})
    assert resp.status_code == 422 and "max_trade_risk_pct" in resp.json()["detail"]


async def test_every_change_is_kept_with_who_and_why(client: AsyncClient, auth_headers, viewer_headers):
    first = (await client.get("/api/risk/settings", headers=viewer_headers)).json()["settings"]
    changed = await _set_limits(client, auth_headers, reason="London session test", max_open_positions=8)
    assert changed["id"] > first["id"] and changed["max_open_positions"] == 8
    assert changed["daily_loss_pct"] == first["daily_loss_pct"], "unchanged values must carry over"
    history = (await client.get("/api/risk/settings/history", headers=viewer_headers)).json()["versions"]
    assert [v["id"] for v in history[:2]] == [changed["id"], first["id"]]
    assert history[0]["reason"] == "London session test" and history[0]["changed_by_name"] == "admin"


# ── Terminal and AI Analyst orders ───────────────────────────────────────────
async def test_terminal_order_is_checked_recorded_and_sent(client: AsyncClient, trader_headers, db_session):
    q = await _quote()
    resp = await client.post("/api/trade/order", headers=trader_headers, json={
        "symbol": "XAUUSD", "action": "BUY", "volume": 0.05, "sl": round(q["ask"] - 10, 2)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["risk_pct"] is not None and 0 < body["risk_pct"] <= 2
    [decision] = await _decisions(db_session, outcome="sent")
    assert decision.source == "terminal" and decision.mt5_ticket == body["ticket"]
    assert decision.volume == 0.05 and decision.settings_id is not None


async def test_terminal_order_links_the_latest_analysis(client: AsyncClient, trader_headers, db_session):
    """From the upstream branch: a Terminal order without a chat id is linked to the
    trader's latest analysis of the same symbol, so it feeds the RAG scores."""
    from app.models.ai_memory import ChatMemory, TradeRecord
    trader_id = (await client.get("/api/auth/me", headers=trader_headers)).json()["id"]
    chat = ChatMemory(user_id=trader_id, symbol="XAUUSD.p", role="assistant", content="Gold bullish.")
    db_session.add(chat)
    await db_session.commit()
    chat_id = chat.id
    q = await _quote()
    resp = await client.post("/api/trade/order", headers=trader_headers, json={
        "symbol": "XAUUSD", "action": "BUY", "volume": 0.01, "sl": round(q["ask"] - 10, 2)})
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    record = (await db_session.execute(select(TradeRecord).where(TradeRecord.mt5_ticket == resp.json()["ticket"]))).scalar_one()
    assert record.ai_message == str(chat_id)


async def test_the_atr_default_stop_is_sent_to_the_broker(client: AsyncClient, auth_headers, trader_headers, db_session):
    await _set_limits(client, auth_headers, default_stop_atr_mult=1.5)
    resp = await client.post("/api/trade/order", headers=trader_headers, json={
        "symbol": "XAUUSD", "action": "BUY", "volume": 0.01})
    assert resp.status_code == 200, resp.text
    [position] = (await connector_client.get_positions())["positions"]
    assert position["sl"] and position["sl"] < resp.json()["price"], "the filled stop never reached the broker"
    [decision] = await _decisions(db_session, outcome="sent")
    assert decision.sl == position["sl"] and decision.context["stop_filled"]["mult"] == 1.5


async def test_terminal_order_without_a_stop_is_refused(client: AsyncClient, trader_headers, db_session):
    resp = await client.post("/api/trade/order", headers=trader_headers,
                             json={"symbol": "XAUUSD", "action": "BUY", "volume": 0.05})
    assert resp.status_code == 422 and "stop loss" in resp.json()["detail"]
    assert (await connector_client.get_positions())["open_count"] == 0
    [decision] = await _decisions(db_session)
    assert decision.outcome == "refused" and decision.reason_code == "no_stop_loss"


async def test_a_ten_lot_typo_is_refused(client: AsyncClient, trader_headers, db_session):
    q = await _quote()
    resp = await client.post("/api/trade/order", headers=trader_headers, json={
        "symbol": "XAUUSD", "action": "BUY", "volume": 10, "sl": round(q["ask"] - 10, 2)})
    assert resp.status_code == 422 and "limit is 2.0%" in resp.json()["detail"]
    assert (await connector_client.get_positions())["open_count"] == 0


async def test_ai_analyst_orders_are_labelled(client: AsyncClient, trader_headers, db_session):
    q = await _quote()
    resp = await client.post("/api/trade/order", headers=trader_headers, json={
        "symbol": "XAUUSD", "action": "SELL", "volume": 0.02, "sl": round(q["bid"] + 10, 2), "chat_memory_id": 42})
    assert resp.status_code == 200, resp.text
    [decision] = await _decisions(db_session, outcome="sent")
    assert decision.source == "ai_analyst" and decision.context["chat_memory_id"] == 42


async def test_a_new_limit_applies_to_the_next_order(client: AsyncClient, auth_headers, trader_headers, db_session):
    q = await _quote()
    order = {"symbol": "XAUUSD", "action": "BUY", "volume": 0.05, "sl": round(q["ask"] - 10, 2)}
    tight = await _set_limits(client, auth_headers, max_trade_risk_pct=0.1, autopilot_risk_pct=0.1)
    resp = await client.post("/api/trade/order", headers=trader_headers, json=order)
    assert resp.status_code == 422
    [decision] = await _decisions(db_session)
    assert decision.reason_code == "trade_risk_too_high" and decision.settings_id == tight["id"]


async def test_open_position_limit(client: AsyncClient, auth_headers, trader_headers):
    await _set_limits(client, auth_headers, max_open_positions=1)
    q = await _quote()
    order = {"symbol": "XAUUSD", "action": "BUY", "volume": 0.01, "sl": round(q["ask"] - 10, 2)}
    assert (await client.post("/api/trade/order", headers=trader_headers, json=order)).status_code == 200
    second = await client.post("/api/trade/order", headers=trader_headers, json=order)
    assert second.status_code == 422 and "limit is 1" in second.json()["detail"]


async def test_removing_a_stop_is_refused_moving_it_is_not(client: AsyncClient, trader_headers):
    q = await _quote()
    placed = await client.post("/api/trade/order", headers=trader_headers, json={
        "symbol": "XAUUSD", "action": "BUY", "volume": 0.01, "sl": round(q["ask"] - 10, 2)})
    ticket = placed.json()["ticket"]
    removed = await client.post("/api/trade/modify", headers=trader_headers, json={"ticket": ticket, "sl": 0})
    assert removed.status_code == 422 and "stop loss" in removed.json()["detail"].lower()
    moved = await client.post("/api/trade/modify", headers=trader_headers,
                              json={"ticket": ticket, "sl": round(q["ask"] - 12, 2)})
    assert moved.status_code == 200, moved.text


async def test_closing_is_never_blocked(client: AsyncClient, auth_headers, trader_headers):
    q = await _quote()
    placed = await client.post("/api/trade/order", headers=trader_headers, json={
        "symbol": "XAUUSD", "action": "BUY", "volume": 0.01, "sl": round(q["ask"] - 10, 2)})
    await _set_limits(client, auth_headers, max_trade_risk_pct=0.01, autopilot_risk_pct=0.01, max_open_positions=1)
    closed = await client.post("/api/trade/close", headers=trader_headers, json={"ticket": placed.json()["ticket"]})
    assert closed.status_code == 200, closed.text


# ── Daily loss, measured on the whole account ───────────────────────────────
async def test_daily_loss_stops_new_orders(client: AsyncClient, trader_headers, viewer_headers, db_session):
    status = (await client.get("/api/risk/status", headers=viewer_headers)).json()
    assert status["start_of_day_equity"] == status["equity"], "the first check of the day records the baseline"
    # Pretend the day started 5% higher than the account is now.
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = (await db_session.execute(select(RiskDay).where(RiskDay.day == day))).scalar_one()
    row.start_equity = status["equity"] / 0.95
    await db_session.commit()

    q = await _quote()
    resp = await client.post("/api/trade/order", headers=trader_headers, json={
        "symbol": "XAUUSD", "action": "BUY", "volume": 0.01, "sl": round(q["ask"] - 10, 2)})
    assert resp.status_code == 422 and "daily limit" in resp.json()["detail"]
    status = (await client.get("/api/risk/status", headers=viewer_headers)).json()
    assert status["daily_loss_pct"] == pytest.approx(5.0, abs=0.01)


# ── Autopilot ────────────────────────────────────────────────────────────────
async def test_autopilot_sizes_from_equity_and_stop(db_session):
    q = await _quote()
    equity = (await connector_client.get_account())["equity"]
    stop = 8.0
    placed = await autopilot.execute_trade(USER_ID, "XAUUSD", "BUY", 0.50, sl=round(q["ask"] - stop, 2),
                                           prompt_num=7, context={"market_regime": "trending"})
    assert placed["success"] is True, placed
    expected = int(equity * 0.01 / (stop * 100) * 100) / 100   # 1% of equity, 100 per 1.00 per lot
    assert placed["volume"] == expected, "the AI's 0.50 lots must be replaced by the risk-based size"
    [decision] = await _decisions(db_session, source="autopilot")
    assert decision.outcome == "sent" and decision.risk_pct <= 1.0
    assert decision.context["ai_lot"] == 0.50 and decision.context["prompt_number"] == 7
    assert decision.context["market_regime"] == "trending"


async def test_autopilot_refusal_is_logged_not_raised(db_session):
    placed = await autopilot.execute_trade(USER_ID, "XAUUSD", "BUY", 0.10)  # no stop
    assert placed == {"success": False, "error": placed["error"], "refused": "no_stop_loss"}
    assert (await connector_client.get_positions())["open_count"] == 0
