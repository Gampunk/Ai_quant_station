"""Phase 4b model routing + Phase 5 insight endpoints + RAG visibility schema."""
import pytest
from datetime import datetime, timezone, timedelta

from app.core.strategy_scorer import get_best_model_for_symbol
from app.models.ai_memory import AutopilotTrade
from app.models.schemas import ChatRequest, ChatResponse


def _trade(symbol="XAUUSD", provider="nvidia", model="model-a",
           profit=10.0, days_ago=1, prompt_text="p"):
    return AutopilotTrade(
        user_id=1, prompt_number=1, prompt_text=prompt_text,
        symbol=symbol, direction="BUY", lot_size=0.01,
        provider=provider, model=model, profit=profit,
        executed_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


# ── Model routing (Phase 4b) ────────────────────────────────────────────────

@pytest.mark.asyncio
class TestModelRouting:
    async def test_returns_best_by_win_rate(self, db_session):
        # Default bar is now 10 trades per provider/model:
        # nvidia: 7/10 wins (70%) beats groq: 3/10 (30%)
        db_session.add_all(
            [_trade(provider="nvidia", model="model-a", profit=10) for _ in range(7)]
            + [_trade(provider="nvidia", model="model-a", profit=-5) for _ in range(3)]
            + [_trade(provider="groq", model="model-b", profit=10) for _ in range(3)]
            + [_trade(provider="groq", model="model-b", profit=-5) for _ in range(7)]
        )
        await db_session.commit()

        best = await get_best_model_for_symbol("XAUUSD")
        assert best is not None
        assert best["provider"] == "nvidia"
        assert best["model"] == "model-a"
        assert best["trades"] == 10
        assert best["win_rate"] == pytest.approx(70.0, abs=0.1)

    async def test_none_below_min_trades(self, db_session):
        # 2 trades — far below the 10-trade bar
        db_session.add_all([
            _trade(provider="nvidia", model="model-a", profit=10),
            _trade(provider="nvidia", model="model-a", profit=10),
        ])
        await db_session.commit()
        assert await get_best_model_for_symbol("XAUUSD") is None

    async def test_none_just_below_min_trades(self, db_session):
        # 9 trades — one short of the proven-sample bar
        db_session.add_all(
            [_trade(provider="nvidia", model="model-a", profit=10) for _ in range(9)]
        )
        await db_session.commit()
        assert await get_best_model_for_symbol("XAUUSD") is None

    async def test_none_without_symbol(self, db_session):
        assert await get_best_model_for_symbol("") is None
        assert await get_best_model_for_symbol(None) is None

    async def test_filters_by_symbol(self, db_session):
        # BTCUSD has 10 qualifying trades — XAUUSD must still return None
        db_session.add_all(
            [_trade(symbol="BTCUSD", provider="nvidia", model="model-a", profit=10)
             for _ in range(10)]
        )
        await db_session.commit()
        assert await get_best_model_for_symbol("XAUUSD") is None
        best_btc = await get_best_model_for_symbol("BTCUSD")
        assert best_btc is not None
        assert best_btc["trades"] == 10

    async def test_null_profit_rows_not_counted(self, db_session):
        db_session.add_all([_trade(profit=None) for _ in range(10)])
        await db_session.commit()
        assert await get_best_model_for_symbol("XAUUSD") is None


# ── Insight endpoints (Phase 5) ─────────────────────────────────────────────

@pytest.mark.asyncio
class TestModelPerformanceEndpoint:
    async def test_returns_aggregates(self, client, auth_headers, db_session):
        db_session.add_all([
            _trade(provider="nvidia", model="model-a", profit=10),
            _trade(provider="nvidia", model="model-a", profit=-4),
            _trade(provider="groq", model="model-b", profit=5),
        ])
        await db_session.commit()

        resp = await client.get("/api/analytics/model-performance", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        by_model = {r["model"]: r for r in rows}
        assert by_model["model-a"]["trades"] == 2
        assert by_model["model-a"]["wins"] == 1
        assert by_model["model-a"]["win_rate"] == 50.0
        assert by_model["model-a"]["total_pnl"] == pytest.approx(6.0)
        assert by_model["model-b"]["trades"] == 1

    async def test_symbol_filter(self, client, auth_headers, db_session):
        db_session.add_all([
            _trade(symbol="XAUUSD", provider="nvidia", model="gold-model", profit=10),
            _trade(symbol="BTCUSD", provider="nvidia", model="btc-model", profit=10),
        ])
        await db_session.commit()

        resp = await client.get(
            "/api/analytics/model-performance?symbol=XAUUSD", headers=auth_headers
        )
        assert resp.status_code == 200
        models = [r["model"] for r in resp.json()]
        assert models == ["gold-model"]

    async def test_requires_auth(self, client):
        resp = await client.get("/api/analytics/model-performance")
        assert resp.status_code == 401


@pytest.mark.asyncio
class TestAccuracyTimelineEndpoint:
    async def test_returns_daily_series(self, client, auth_headers, db_session):
        today = datetime.now(timezone.utc)
        db_session.add_all([
            _trade(profit=10, days_ago=0),
            _trade(profit=-5, days_ago=0),
            _trade(profit=7, days_ago=1),
            _trade(profit=None, days_ago=0),  # open trade — excluded
        ])
        await db_session.commit()

        resp = await client.get(
            "/api/analytics/accuracy-timeline?days=30", headers=auth_headers
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["days"] == 30
        timeline = data["timeline"]
        assert len(timeline) == 2  # today + yesterday

        yday_entry = next(t for t in timeline if t["date"] == (today - timedelta(days=1)).date().isoformat())
        assert yday_entry["trades"] == 1
        assert yday_entry["win_rate"] == 100.0

        today_entry = next(t for t in timeline if t["date"] == today.date().isoformat())
        assert today_entry["trades"] == 2
        assert today_entry["wins"] == 1
        assert today_entry["win_rate"] == 50.0

    async def test_days_clamped(self, client, auth_headers):
        resp = await client.get(
            "/api/analytics/accuracy-timeline?days=9999", headers=auth_headers
        )
        assert resp.status_code == 200
        assert resp.json()["days"] == 90

    async def test_requires_auth(self, client):
        resp = await client.get("/api/analytics/accuracy-timeline")
        assert resp.status_code == 401


# ── RAG visibility schema (Phase 3 add-on) ─────────────────────────────────

class TestRagVisibilitySchema:
    def test_debug_rag_defaults_off(self):
        req = ChatRequest(
            messages=[{"role": "user", "content": "hi"}],
            provider="nvidia", model="m",
        )
        assert req.debug_rag is False

    def test_debug_rag_can_be_enabled(self):
        req = ChatRequest(
            messages=[{"role": "user", "content": "hi"}],
            provider="nvidia", model="m", debug_rag=True,
        )
        assert req.debug_rag is True

    def test_response_rag_context_optional(self):
        resp = ChatResponse(message="hello")
        assert resp.rag_context is None

    def test_response_rag_context_serialized(self):
        resp = ChatResponse(message="hello", rag_context="BEST PERFORMING...")
        assert resp.model_dump()["rag_context"] == "BEST PERFORMING..."
