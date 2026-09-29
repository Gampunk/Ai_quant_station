"""
Backend against the real connector.py running on the fake MetaTrader5 terminal.

Covers the one route to the broker end to end: autopilot, the Terminal page's
trade endpoints, the market data routes, and the background candle fetches.
"""
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.api import autopilot
from app.core.config import settings
from app.core.mt5_connector import ConnectorError, connector_client
from app.models.ai_memory import AutopilotSettings, AutopilotTrade, PositionAudit, TradeRecord
from tests.fake_connector import free_port, start_fake, stop_fake

USER_ID = 1  # admin, created by conftest
CONTRACT_TOKEN = "contract-test-token"
_free_port, _start_fake = free_port, start_fake


@pytest.fixture(scope="module")
def fake_url():
    proc, url = _start_fake(_free_port())
    yield url
    stop_fake(proc)


@pytest.fixture(autouse=True)
def _point_server_at_fake(fake_url, monkeypatch):
    """The connector address is a server setting; point it at the fake."""
    monkeypatch.setattr(settings, "MT5_CONNECTOR_URL", fake_url)
    monkeypatch.setattr(settings, "MT5_API_TOKEN", "")


def _deal(deals, ticket, entry):
    return next(d for d in deals if d["position_id"] == ticket and d["entry"] == entry)


# ── Autopilot ────────────────────────────────────────────────────────────────
async def test_autopilot_initializes_and_fetches_candles():
    assert await autopilot.initialize_mt5_connector(USER_ID) is True
    candles = await autopilot.get_market_data(USER_ID, "XAUUSD", timeframe="15m", count=120)
    assert len(candles) == 120
    assert {"time", "open", "high", "low", "close"} <= set(candles[0])


async def test_autopilot_trade_round_trip_is_recorded_by_sync(db_session):
    bid = (await connector_client.get_symbol("XAUUSD"))["bid"]
    placed = await autopilot.execute_trade(USER_ID, "XAUUSD", "BUY", 0.10, sl=bid - 10, tp=bid + 20)
    assert placed["success"] is True, placed
    ticket = placed["ticket"]
    assert ticket in [p["ticket"] for p in await autopilot.check_open_positions(USER_ID)]

    db_session.add(AutopilotSettings(user_id=USER_ID))
    db_session.add(AutopilotTrade(
        user_id=USER_ID, prompt_number=1, prompt_text="contract test", symbol="XAUUSD",
        direction="BUY", lot_size=0.10, mt5_ticket=ticket, execution_status="executed",
    ))
    await db_session.commit()

    assert (await connector_client.close_position(ticket))["success"] is True
    close_deal = _deal((await connector_client.get_history(hours=24))["deals"], ticket, "CLOSE")

    await autopilot.sync_trade_results(USER_ID)

    db_session.expire_all()
    trade = (await db_session.execute(select(AutopilotTrade).where(AutopilotTrade.mt5_ticket == ticket))).scalar_one()
    assert trade.result is not None, "autopilot sync did not record the closed trade"
    assert trade.profit == close_deal["profit"]
    assert trade.exit_price == close_deal["price"]
    assert trade.closed_at is not None


async def test_autopilot_records_the_filled_price_not_the_quote():
    quote = await connector_client.get_symbol("XAUUSD")
    placed = await autopilot.execute_trade(USER_ID, "XAUUSD", "BUY", 0.10, sl=quote["bid"] - 10)
    assert placed["success"] is True, placed
    opened = _deal((await connector_client.get_history(hours=24))["deals"], placed["ticket"], "OPEN")
    assert placed["price"] == opened["price"], "stored the quote instead of the fill"
    assert placed["price"] != quote["ask"], "fill and quote are identical, so this proves nothing"
    # The quote at send time travels with the fill, so slippage can be measured.
    assert placed["requested_price"] is not None
    assert placed["requested_price"] != placed["price"]
    await connector_client.close_position(placed["ticket"])


# ── Terminal page: manual trading ────────────────────────────────────────────
async def test_terminal_order_close_and_modify(client: AsyncClient, trader_headers, db_session):
    """Manual trading used to need the Windows-only MT5 package on the server."""
    bid = (await connector_client.get_symbol("XAUUSD"))["bid"]
    order = await client.post("/api/trade/order", headers=trader_headers, json={
        "symbol": "XAUUSD", "action": "BUY", "volume": 0.05, "sl": bid - 15, "tp": bid + 30,
    })
    assert order.status_code == 200, order.text
    ticket = order.json()["ticket"]

    record = (await db_session.execute(select(TradeRecord).where(TradeRecord.mt5_ticket == ticket))).scalar_one()
    opened = _deal((await connector_client.get_history(hours=24))["deals"], ticket, "OPEN")
    assert record.entry_price == opened["price"] and record.status == "open"
    # The fake fills a couple of points away from the quote, like a real broker.
    assert record.requested_price is not None, "quoted price not recorded"
    assert record.requested_price != record.entry_price
    assert abs(record.requested_price - record.entry_price) < 1.0

    modified = await client.post("/api/trade/modify", headers=trader_headers,
                                 json={"ticket": ticket, "sl": round(bid - 20, 2)})
    assert modified.status_code == 200, modified.text

    closed = await client.post("/api/trade/close", headers=trader_headers, json={"ticket": ticket})
    assert closed.status_code == 200, closed.text
    shut = _deal((await connector_client.get_history(hours=24))["deals"], ticket, "CLOSE")

    db_session.expire_all()
    record = (await db_session.execute(select(TradeRecord).where(TradeRecord.mt5_ticket == ticket))).scalar_one()
    assert record.status == "closed"
    assert record.exit_price == shut["price"] and record.profit_loss == shut["profit"]
    assert record.requested_exit_price is not None, "quoted close price not recorded"
    assert record.requested_exit_price != record.exit_price
    audits = {a.action: a for a in (await db_session.execute(
        select(PositionAudit).where(PositionAudit.mt5_ticket == ticket))).scalars().all()}
    assert sorted(audits) == ["close", "modify"]
    # The audits read the position's stop from the connector, which used to leave it out.
    assert audits["modify"].original_sl == round(bid - 15, 2)
    assert audits["close"].original_sl == round(bid - 20, 2)


async def test_pending_order_records_no_requested_price(client: AsyncClient, trader_headers, db_session):
    """A pending order has not filled, so there is no quote to compare against yet."""
    bid = (await connector_client.get_symbol("XAUUSD"))["bid"]
    order = await client.post("/api/trade/order", headers=trader_headers, json={
        "symbol": "XAUUSD", "action": "BUY_LIMIT", "volume": 0.05, "price": round(bid - 50, 2),
        "sl": round(bid - 60, 2),
    })
    assert order.status_code == 200, order.text
    record = (await db_session.execute(
        select(TradeRecord).where(TradeRecord.mt5_ticket == order.json()["ticket"]))).scalar_one()
    assert record.order_type == "pending"
    assert record.requested_price is None


async def test_terminal_passes_on_the_connectors_refusal(client: AsyncClient, trader_headers):
    resp = await client.post("/api/trade/order", headers=trader_headers,
                             json={"symbol": "NOPE", "action": "BUY", "volume": 0.05, "sl": 1.0})
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


async def test_terminal_reports_an_unreachable_connector(client: AsyncClient, trader_headers, monkeypatch):
    monkeypatch.setattr(settings, "MT5_CONNECTOR_URL", f"http://127.0.0.1:{_free_port()}")
    resp = await client.post("/api/trade/order", headers=trader_headers,
                             json={"symbol": "XAUUSD", "action": "BUY", "volume": 0.05, "sl": 1.0})
    assert resp.status_code == 502
    assert "unreachable" in resp.json()["detail"].lower()


# ── Market data routes ───────────────────────────────────────────────────────
async def test_health_reports_the_terminal_as_connected(client: AsyncClient, viewer_headers):
    """This route used to read a key the connector never sends, so it always said not initialized."""
    resp = await client.get("/api/mt5/health", headers=viewer_headers)
    assert resp.status_code == 200 and resp.json()["mt5_initialized"] is True


@pytest.mark.parametrize("method, path, body", [
    ("get", "/api/mt5/account", None),
    ("get", "/api/mt5/positions", None),
    ("get", "/api/mt5/history", None),
    ("get", "/api/mt5/symbols", None),
    ("get", "/api/mt5/symbols/all", None),
    ("get", "/api/mt5/symbol/XAUUSD", None),
    ("post", "/api/mt5/data/latest", {"symbol": "XAUUSD", "timeframe": "15m", "count": 50}),
])
async def test_viewer_can_read_market_data(client: AsyncClient, viewer_headers, method, path, body):
    resp = await (client.post(path, headers=viewer_headers, json=body) if body
                  else client.get(path, headers=viewer_headers))
    assert resp.status_code == 200, f"{path}: {resp.status_code} {resp.text[:200]}"


async def test_market_data_needs_a_login(client: AsyncClient):
    assert (await client.get("/api/mt5/positions")).status_code in (401, 403)


async def test_shared_connector_token_no_longer_reads_market_data(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "MT5_API_TOKEN", "the-shared-token")
    resp = await client.get("/api/mt5/positions", headers={"x-mt5-token": "the-shared-token"})
    assert resp.status_code in (401, 403)


async def test_only_traders_can_initialize_the_terminal(client: AsyncClient, viewer_headers, trader_headers):
    assert (await client.post("/api/mt5/initialize", headers=viewer_headers)).status_code == 403
    assert (await client.post("/api/mt5/initialize", headers=trader_headers)).status_code == 200


async def test_background_candle_fetch_uses_the_connector():
    """The hourly price sync used to crash here on Linux and report success."""
    from app.core.mt5_service import fetch_latest_candles, init_mt5_connection
    assert await init_mt5_connection() is True
    assert len(await fetch_latest_candles("XAUUSD", count=30, timeframe="1m")) == 30


# ── Token between backend and connector ──────────────────────────────────────
async def test_token_is_accepted_and_a_wrong_one_is_refused(monkeypatch):
    proc, url = _start_fake(_free_port(), "--token", CONTRACT_TOKEN)
    try:
        monkeypatch.setattr(settings, "MT5_CONNECTOR_URL", url)
        for token in ("", "the-wrong-token"):
            monkeypatch.setattr(settings, "MT5_API_TOKEN", token)
            with pytest.raises(ConnectorError) as err:
                await connector_client.health()
            assert err.value.status_code == 401
        monkeypatch.setattr(settings, "MT5_API_TOKEN", CONTRACT_TOKEN)
        assert (await connector_client.health())["mt5_connected"] is True
    finally:
        proc.terminate()
        proc.wait(timeout=10)
