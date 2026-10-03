"""
Real connector.py, fake MetaTrader5 terminal.

Negative control for the demo guard: add `return` as the first line of
require_demo_account() in connector.py. Four tests must fail: order, close and
modify on a real account, and trading with an unreadable account.
"""
import os
import subprocess
import sys

import pytest

import connector
from conftest import CONNECTOR_DIR, FAKE_DIR, TEST_TOKEN, buy


# ── Startup configuration ────────────────────────────────────────────────────
def _config_in_subprocess(extra_env):
    env = {k: v for k, v in os.environ.items() if not k.startswith("MT5_")}
    env.update({"MT5_CONNECTOR_PORT": "5999", "MT5_API_TOKEN": "import-time-placeholder",
                "PYTHONPATH": os.pathsep.join([FAKE_DIR, CONNECTOR_DIR])})
    env.update(extra_env)
    out = subprocess.run(
        [sys.executable, "-c", "import connector; print(connector.BIND_HOST, connector.REQUIRE_DEMO)"],
        env=env, capture_output=True, text=True, timeout=60, cwd=CONNECTOR_DIR,
    )
    assert out.returncode == 0, out.stderr
    host, demo = out.stdout.split()
    return host, demo == "True"


def test_binds_to_localhost_by_default():
    assert _config_in_subprocess({})[0] == "127.0.0.1"


def test_bind_host_can_be_set_explicitly():
    assert _config_in_subprocess({"MT5_CONNECTOR_HOST": "0.0.0.0"})[0] == "0.0.0.0"


@pytest.mark.parametrize("value, expected", [
    (None, True), ("true", True), ("TRUE", True), ("", True), ("banana", True),
    ("false", False), ("False", False), ("0", False), ("no", False),
])
def test_demo_guard_is_on_unless_explicitly_false(value, expected):
    env = {} if value is None else {"MT5_REQUIRE_DEMO": value}
    assert _config_in_subprocess(env)[1] is expected


# ── Read endpoints ───────────────────────────────────────────────────────────
def test_initialize_reports_demo_account(client, mt5, monkeypatch):
    monkeypatch.setattr(connector, "mt5_initialized", False)
    body = client.post("/initialize").json()
    assert body["account"]["trade_mode"] == "demo"
    assert body["require_demo"] is True


def test_initialize_failure_is_reported(mt5, monkeypatch):
    from fastapi.testclient import TestClient
    mt5._reset(init_ok=False)
    monkeypatch.setattr(connector, "mt5_initialized", False)
    monkeypatch.setattr(connector, "CONNECTOR_API_TOKEN", TEST_TOKEN)
    with TestClient(connector.app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as c:
        resp = c.post("/initialize")
    assert resp.status_code == 500


def test_symbol_quote(client):
    body = client.get("/symbol/XAUUSD").json()
    assert body["ask"] > body["bid"] > 0
    assert body["digits"] == 2


def test_unknown_symbol_is_404(client):
    assert client.get("/symbol/NOPE").status_code == 404


def test_latest_candles_are_valid_and_ordered(client):
    data = client.get("/data/latest/XAUUSD", params={"timeframe": "15m", "count": 200}).json()["data"]
    assert len(data) == 200
    times = [row["time"] for row in data]
    assert times == sorted(times) and len(set(times)) == 200
    assert all(t2 - t1 == 900 for t1, t2 in zip(times, times[1:]))
    for row in data:
        assert row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"]


def test_latest_candles_are_deterministic(client):
    a = client.get("/data/latest/XAUUSD", params={"timeframe": "1h", "count": 50}).json()
    b = client.get("/data/latest/XAUUSD", params={"timeframe": "1h", "count": 50}).json()
    assert a == b


# ── Trading lifecycle ────────────────────────────────────────────────────────
def test_buy_opens_a_position(client):
    order = buy(client, sl=None, tp=None).json()
    assert order["success"] is True
    positions = client.get("/positions").json()
    assert positions["open_count"] == 1
    assert positions["positions"][0]["ticket"] == order["ticket"]
    assert positions["positions"][0]["direction"] == "BUY"


def test_invalid_volume_is_rejected(client):
    assert buy(client, volume=0.001).status_code == 400


def test_close_records_open_and_close_deals_with_profit(client, mt5):
    ticket = buy(client).json()["ticket"]
    quote = client.get("/symbol/XAUUSD").json()
    mt5._set_price("XAUUSD", quote["bid"] + 10.0)
    closed = client.post("/close", json={"ticket": ticket}).json()
    assert closed["success"] is True
    assert client.get("/positions").json()["open_count"] == 0

    deals = [d for d in client.get("/history").json()["deals"] if d["position_id"] == ticket]
    assert [d["entry"] for d in deals] == ["OPEN", "CLOSE"]
    assert deals[1]["profit"] > 0


def test_modify_sets_stop_and_target(client):
    ticket = buy(client).json()["ticket"]
    bid = client.get("/symbol/XAUUSD").json()["bid"]
    resp = client.post("/modify", json={"ticket": ticket, "sl": bid - 15, "tp": bid + 30})
    assert resp.status_code == 200, resp.text
    assert resp.json()["sl"] == round(bid - 15, 2)


def test_stop_loss_hit_closes_with_sl_comment(client, mt5):
    ask = client.get("/symbol/XAUUSD").json()["ask"]
    ticket = buy(client, sl=ask - 5, tp=ask + 10).json()["ticket"]
    mt5._set_price("XAUUSD", ask - 6)
    assert client.get("/positions").json()["open_count"] == 0
    close = [d for d in client.get("/history").json()["deals"] if d["position_id"] == ticket and d["entry"] == "CLOSE"]
    assert close and "sl" in close[0]["comment"] and close[0]["profit"] < 0


# ── Demo-only guard ──────────────────────────────────────────────────────────
@pytest.fixture
def real_account_ticket(client, mt5):
    """Open a position while demo, then switch the account to real."""
    ticket = buy(client).json()["ticket"]
    mt5._S["trade_mode"] = mt5.ACCOUNT_TRADE_MODE_REAL
    return ticket


def test_real_account_order_is_refused(client, real_account_ticket):
    resp = buy(client)
    assert resp.status_code == 403
    assert "not a demo account" in resp.json()["detail"]


def test_real_account_close_is_refused(client, real_account_ticket):
    assert client.post("/close", json={"ticket": real_account_ticket}).status_code == 403


def test_real_account_modify_is_refused(client, real_account_ticket):
    assert client.post("/modify", json={"ticket": real_account_ticket, "sl": 1.0}).status_code == 403


def test_real_account_can_still_be_read(client, real_account_ticket):
    assert client.get("/positions").status_code == 200
    assert client.get("/history").status_code == 200


def test_unreadable_account_refuses_to_trade(client, mt5):
    mt5._S["account_available"] = False
    assert buy(client).status_code == 503


def test_explicit_override_allows_real_account(client, real_account_ticket, monkeypatch):
    monkeypatch.setattr(connector, "REQUIRE_DEMO", False)
    assert buy(client).status_code == 200


# ── Execution price is the fill, not the quote ───────────────────────────────
def test_order_reports_the_filled_price_not_the_quote(client):
    quote = client.get("/symbol/XAUUSD").json()
    order = buy(client).json()
    assert order["requested_price"] == quote["ask"]
    assert order["price"] != quote["ask"], "fill price is still just the quote"

    deals = client.get("/history").json()["deals"]
    opened = next(d for d in deals if d["position_id"] == order["ticket"] and d["entry"] == "OPEN")
    assert order["price"] == opened["price"], "reported price does not match the executed deal"


def test_close_reports_the_filled_price_not_the_quote(client):
    ticket = buy(client).json()["ticket"]
    quote = client.get("/symbol/XAUUSD").json()
    closed = client.post("/close", json={"ticket": ticket}).json()
    assert closed["requested_price"] == quote["bid"]
    assert closed["close_price"] != quote["bid"]

    deals = client.get("/history").json()["deals"]
    shut = next(d for d in deals if d["position_id"] == ticket and d["entry"] == "CLOSE")
    assert closed["close_price"] == shut["price"]


def test_reported_prices_explain_the_profit(client):
    """Entry and exit as reported must account for the profit the broker paid."""
    order = buy(client, volume=0.10).json()
    closed = client.post("/close", json={"ticket": order["ticket"]}).json()
    shut = next(d for d in client.get("/history").json()["deals"]
                if d["position_id"] == order["ticket"] and d["entry"] == "CLOSE")
    expected = (closed["close_price"] - order["price"]) * 0.10 * 100  # gold, 100 oz contract
    assert abs(expected - shut["profit"]) < 0.01


# ── Positions and times ──────────────────────────────────────────────────────
FIXED_NOW_TEXT = "2026-09-14 10:00:00"


def test_positions_report_the_stop_loss(client):
    ask = client.get("/symbol/XAUUSD").json()["ask"]
    buy(client, sl=ask - 5, tp=ask + 10)
    pos = client.get("/positions").json()["positions"][0]
    assert pos["sl"] == round(ask - 5, 2)
    assert pos["tp"] == round(ask + 10, 2)


def test_position_without_stop_reports_none(client):
    buy(client, sl=None, tp=None)
    pos = client.get("/positions").json()["positions"][0]
    assert pos["sl"] is None and pos["tp"] is None


@pytest.fixture
def machine_time_zone(monkeypatch):
    """Run as if the connector machine were set to India time, UTC+5:30."""
    import time
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_times_do_not_depend_on_the_machine_time_zone(client, machine_time_zone):
    ticket = buy(client).json()["ticket"]
    pos = client.get("/positions").json()["positions"][0]
    deal = next(d for d in client.get("/history").json()["deals"] if d["position_id"] == ticket)
    # Both read the same MT5 timestamp, so both must show it the same way.
    assert pos["open_time"] == deal["time"] == FIXED_NOW_TEXT


def test_initialize_ignores_a_caller_supplied_terminal_path(client, mt5, monkeypatch):
    monkeypatch.setattr(connector, "mt5_initialized", False)
    resp = client.post("/initialize", params={"terminal_path_input": r"C:\evil\payload.exe"})
    assert resp.status_code == 200
    assert mt5._S["path"] == connector.STARTUP_PATH


def test_data_range_rejects_a_bad_date(client):
    resp = client.get("/data/range/XAUUSD", params={"timeframe": "1h", "start": "yesterday"})
    assert resp.status_code == 400


# ── Order limits and stop placement ──────────────────────────────────────────
def test_symbol_reports_what_position_sizing_needs(client):
    body = client.get("/symbol/XAUUSD").json()
    assert body["trade_contract_size"] == 100.0
    assert body["trade_tick_size"] == 0.01 and body["trade_tick_value"] == 1.0
    assert body["volume_step"] == 0.01
    assert body["trade_stops_level"] == 0 and body["min_stop_distance"] == 0.1
    assert body["max_volume"] == connector.MAX_VOLUME


def test_volume_above_the_connector_cap_is_refused(client, mt5):
    resp = buy(client, volume=connector.MAX_VOLUME + 0.01)
    assert resp.status_code == 403
    assert "MT5_MAX_VOLUME" in resp.json()["detail"]
    assert mt5.positions_get() == ()


def test_volume_at_the_cap_is_accepted(client):
    assert buy(client, volume=connector.MAX_VOLUME, sl=None, tp=None).status_code == 200


def test_volume_above_the_broker_maximum_is_refused(client, monkeypatch):
    monkeypatch.setattr(connector, "MAX_VOLUME", 1000.0)
    resp = buy(client, volume=150)
    assert resp.status_code == 400 and "broker's maximum" in resp.json()["detail"]


@pytest.mark.parametrize("side, sl_offset, tp_offset, word", [
    ("BUY", +1.0, None, "Stop loss"),     # stop above the entry of a buy
    ("BUY", -0.05, None, "Stop loss"),    # closer than the 0.10 minimum
    ("BUY", None, -1.0, "Take profit"),   # target below the entry of a buy
    ("SELL", -1.0, None, "Stop loss"),    # stop below the entry of a sell
    ("SELL", None, +1.0, "Take profit"),  # target above the entry of a sell
])
def test_misplaced_stops_are_refused_not_moved(client, mt5, side, sl_offset, tp_offset, word):
    quote = client.get("/symbol/XAUUSD").json()
    price = quote["ask"] if side == "BUY" else quote["bid"]
    body = {"action": side}
    if sl_offset is not None:
        body["sl"] = round(price + sl_offset, 2)
    if tp_offset is not None:
        body["tp"] = round(price + tp_offset, 2)
    resp = buy(client, **body)
    assert resp.status_code == 400, resp.text
    assert word in resp.json()["detail"]
    assert mt5.positions_get() == ()


# ── Broker clock and close reasons ───────────────────────────────────────────
@pytest.fixture
def broker_on_utc_plus_3(mt5, client, monkeypatch):
    mt5._S["server_offset"] = 3 * 3600
    monkeypatch.setattr(connector.time, "time", lambda: mt5._now())
    connector._clock_last.update(tick=None, at=None)
    return mt5


def test_clock_reports_nothing_until_prices_move(client, broker_on_utc_plus_3):
    body = client.get("/clock").json()
    assert body["offset_hours"] is None and body["live"] is False


def test_clock_detects_the_offset_once_prices_move(client, broker_on_utc_plus_3):
    client.get("/clock")
    broker_on_utc_plus_3._advance(60)
    body = client.get("/clock").json()
    assert body["live"] is True and body["offset_hours"] == 3.0


def test_clock_ignores_stale_prices(client, broker_on_utc_plus_3, monkeypatch):
    """A closed market: the last price is 40 minutes old, which is no offset at all."""
    mt5 = broker_on_utc_plus_3
    client.get("/clock")
    mt5._advance(60)
    real_now = mt5._now()
    monkeypatch.setattr(connector.time, "time", lambda: real_now + 40 * 60)
    assert client.get("/clock").json()["offset_hours"] is None


def test_history_reports_why_a_deal_closed(client, mt5):
    ask = client.get("/symbol/XAUUSD").json()["ask"]
    stopped = buy(client, sl=ask - 5, tp=ask + 10).json()["ticket"]
    mt5._set_price("XAUUSD", ask - 6)
    targeted = buy(client, sl=ask - 20, tp=ask + 1).json()["ticket"]
    mt5._set_price("XAUUSD", ask + 2)
    manual = buy(client, sl=ask - 20).json()["ticket"]
    client.post("/close", json={"ticket": manual})
    deals = client.get("/history").json()["deals"]
    reason = {d["position_id"]: d["reason"] for d in deals if d["entry"] == "CLOSE"}
    assert reason == {stopped: "sl", targeted: "tp", manual: "expert"}
