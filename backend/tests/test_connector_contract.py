"""
Backend against the real connector.py running on the fake MetaTrader5 terminal.

Proves the HTTP contract end to end: connect, candles, order, positions, close,
and the autopilot recording the closed trade's result from connector history.
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.api import autopilot
from app.core.config import settings
from app.core.mt5_connector import MT5ConnectorClient
from app.models.ai_memory import AutopilotSettings, AutopilotTrade

LAUNCHER = Path(__file__).resolve().parents[2] / "mt5_connector" / "testing" / "run_fake_connector.py"
USER_ID = 1  # admin, created by conftest


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


CONTRACT_TOKEN = "contract-test-token"


def _start_fake(port, *extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MT5_", "FAKE_MT5_"))}
    return subprocess.Popen(
        [sys.executable, str(LAUNCHER), "--port", str(port), *extra],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


@pytest.fixture(scope="module")
def connector_url():
    port = _free_port()
    proc = _start_fake(port)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            if proc.poll() is not None:
                pytest.fail(f"fake connector exited early:\n{proc.stdout.read()}")
            try:
                if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            pytest.fail("fake connector did not become healthy")
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(autouse=True)
async def _fresh_http_client():
    """The autopilot shares one HTTP client; each test has its own event loop."""
    autopilot._http_client = None
    yield
    await autopilot.shutdown_http_client()


async def test_initialize_and_fetch_candles(connector_url):
    assert await autopilot.initialize_mt5_connector(USER_ID, None, connector_url) is True

    candles = await autopilot.get_market_data(USER_ID, "XAUUSD", timeframe="15m", count=120, connector_url=connector_url)
    assert len(candles) == 120
    assert {"time", "open", "high", "low", "close"} <= set(candles[0])


async def test_trade_round_trip_is_recorded_by_autopilot_sync(connector_url, db_session):
    client = MT5ConnectorClient()
    client.base_url = connector_url
    try:
        quote = await client.get_symbol("XAUUSD")
        bid = quote["bid"]

        placed = await autopilot.execute_trade(
            USER_ID, "XAUUSD", "BUY", 0.10, sl=bid - 10, tp=bid + 20, connector_url=connector_url,
        )
        assert placed["success"] is True, placed
        ticket = placed["ticket"]

        open_tickets = [p["ticket"] for p in await autopilot.check_open_positions(connector_url, USER_ID)]
        assert ticket in open_tickets

        db_session.add(AutopilotSettings(user_id=USER_ID, mt5_connector_url=connector_url))
        db_session.add(AutopilotTrade(
            user_id=USER_ID, prompt_number=1, prompt_text="contract test", symbol="XAUUSD",
            direction="BUY", lot_size=0.10, mt5_ticket=ticket, execution_status="executed",
        ))
        await db_session.commit()

        closed = await client.close_position(ticket)
        assert closed["success"] is True

        history = await client.get_history(hours=24)
        close_deal = next(d for d in history["deals"] if d["position_id"] == ticket and d["entry"] == "CLOSE")
    finally:
        await client.close()

    await autopilot.sync_trade_results(USER_ID, connector_url)

    db_session.expire_all()
    trade = (await db_session.execute(
        select(AutopilotTrade).where(AutopilotTrade.mt5_ticket == ticket)
    )).scalar_one()
    assert trade.result is not None, "autopilot sync did not record the closed trade"
    assert trade.profit == close_deal["profit"]
    assert trade.exit_price == close_deal["price"]
    assert trade.closed_at is not None


async def test_backend_records_the_filled_price_not_the_quote(connector_url):
    client = MT5ConnectorClient()
    client.base_url = connector_url
    try:
        quote = await client.get_symbol("XAUUSD")
        placed = await autopilot.execute_trade(
            USER_ID, "XAUUSD", "BUY", 0.10, connector_url=connector_url,
        )
        assert placed["success"] is True, placed

        opened = next(d for d in (await client.get_history(hours=24))["deals"]
                      if d["position_id"] == placed["ticket"] and d["entry"] == "OPEN")
        assert placed["price"] == opened["price"], "backend stored the quote instead of the fill"
        assert placed["price"] != quote["ask"], "fill and quote are identical, so this proves nothing"
        await client.close_position(placed["ticket"])
    finally:
        await client.close()


async def test_token_is_accepted_and_a_wrong_one_is_refused(monkeypatch):
    """The backend sends the token as a header, which is what the connector now reads."""
    port = _free_port()
    proc = _start_fake(port, "--token", CONTRACT_TOKEN)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            if proc.poll() is not None:
                pytest.fail(f"fake connector exited early:\n{proc.stdout.read()}")
            try:
                httpx.get(f"{url}/health", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.5)

        monkeypatch.setattr(settings, "MT5_API_TOKEN", "")
        with pytest.raises(Exception, match="401"):
            await autopilot.async_request("GET", f"{url}/health")

        monkeypatch.setattr(settings, "MT5_API_TOKEN", "the-wrong-token")
        with pytest.raises(Exception, match="401"):
            await autopilot.async_request("GET", f"{url}/health")

        monkeypatch.setattr(settings, "MT5_API_TOKEN", CONTRACT_TOKEN)
        assert (await autopilot.async_request("GET", f"{url}/health"))["mt5_connected"] is True
    finally:
        proc.terminate()
        proc.wait(timeout=10)
