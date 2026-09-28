import numpy as np
import pytest
from datetime import datetime, timezone
from types import SimpleNamespace
from sqlalchemy import select

from app.core.embed_service import compute_similarity
from app.core.rag_service import (
    _clean_for_embedding, find_similar_analyses, get_strategy_scores,
    get_underperforming_strategies, build_rag_context,
)
from app.core.strategy_scorer import _ensure_aware, classify_prompt_status
from app.models.chat_embedding import ChatEmbedding
from app.models.ai_memory import ChatMemory, AutopilotTrade
from app.models.strategy_score import StrategyScore


# ── Pure helpers (no ML model, no DB) ───────────────────────────────────────

class TestCleanForEmbedding:
    def test_strips_python_code_blocks(self):
        text = (
            "Gold is holding support at 2650.\n"
            "```python\nimport pandas as pd\ndf = pd.DataFrame({'a': [1,2]})\n```\n"
            "Bias remains bullish while above 2640."
        )
        cleaned = _clean_for_embedding(text)
        assert "```" not in cleaned
        assert "import pandas" not in cleaned
        assert "Bias remains bullish" in cleaned

    def test_handles_empty_and_none(self):
        assert _clean_for_embedding("") == ""
        assert _clean_for_embedding(None) == ""

    def test_collapses_whitespace(self):
        cleaned = _clean_for_embedding("a\n\n\n   b\t\tc")
        assert cleaned == "a b c"

    def test_caps_length(self):
        cleaned = _clean_for_embedding("word " * 5000)
        assert len(cleaned) <= 4000


class TestEnsureAware:
    def test_naive_gets_utc(self):
        naive = datetime(2026, 8, 17, 3, 14, 32)
        fixed = _ensure_aware(naive)
        assert fixed.tzinfo is not None
        assert fixed.utcoffset().total_seconds() == 0
        assert fixed == naive.replace(tzinfo=timezone.utc)

    def test_aware_untouched(self):
        aware = datetime(2026, 8, 17, 3, 14, 32, tzinfo=timezone.utc)
        assert _ensure_aware(aware) is aware

    def test_none_passes_through(self):
        assert _ensure_aware(None) is None


class TestComputeSimilarity:
    def test_identical_vectors(self):
        v = np.array([1.0, 2.0, 3.0])
        assert compute_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        a = np.array([1.0, 0.0])
        b = np.array([0.0, 1.0])
        assert compute_similarity(a, b) == pytest.approx(0.0)

    def test_zero_vector_returns_zero(self):
        a = np.array([0.0, 0.0])
        b = np.array([1.0, 1.0])
        assert compute_similarity(a, b) == 0.0


# ── DB-backed: SQL compatibility (works on SQLite; the cast() used in the
#    query is also valid on PostgreSQL, which was the actual bug) ────────────

def _vec(seed: float) -> bytes:
    return np.array([seed, 0.5, 0.1], dtype=np.float32).tobytes()


@pytest.mark.asyncio
class TestRagQueries:
    async def test_find_similar_returns_scored_rows(self, db_session):
        db_session.add(ChatMemory(user_id=1, symbol="XAUUSD", role="assistant",
                                  content="Support holding at 2650, bullish"))
        await db_session.commit()
        chat = (await db_session.execute(select(ChatMemory))).scalars().first()
        db_session.add(ChatEmbedding(chat_memory_id=chat.id, embedding=_vec(1.0)))
        await db_session.commit()

        results = await find_similar_analyses([1.0, 0.5, 0.1], "XAUUSD")
        assert len(results) == 1
        score, row = results[0]
        assert "Support holding" in row.content
        # identical vectors → similarity 1.0 → score = 0.5*1 + 0.3*0 + 0.2*0
        assert score == pytest.approx(0.5, abs=1e-3)

    async def test_find_similar_filters_by_symbol(self, db_session):
        db_session.add(ChatMemory(user_id=1, symbol="BTCUSD", role="assistant",
                                  content="unrelated"))
        await db_session.commit()
        chat = (await db_session.execute(select(ChatMemory))).scalars().first()
        db_session.add(ChatEmbedding(chat_memory_id=chat.id, embedding=_vec(1.0)))
        await db_session.commit()

        results = await find_similar_analyses([1.0, 0.5, 0.1], "XAUUSD")
        assert results == []

    async def test_get_strategy_scores_requires_min_trades(self, db_session):
        db_session.add_all([
            StrategyScore(prompt_text="fluke", symbol="XAUUSD", direction="buy",
                          source="autopilot", total_trades=9, win_rate=100.0),
            StrategyScore(prompt_text="proven", symbol="XAUUSD", direction="buy",
                          source="autopilot", total_trades=12, win_rate=80.0),
        ])
        await db_session.commit()

        scores = await get_strategy_scores("XAUUSD")
        assert len(scores) == 1
        assert scores[0].prompt_text == "proven"

    async def test_strategy_score_defaults_are_timezone_safe(self, db_session):
        # Regression: naive datetimes broke asyncpg inserts (timestamptz).
        score = StrategyScore(prompt_text="p", symbol="XAUUSD", direction="buy",
                              source="autopilot", total_trades=0,
                              first_used=_ensure_aware(datetime(2026, 8, 17, 3, 14, 32)),
                              last_used=_ensure_aware(datetime(2026, 8, 18, 3, 14, 32)))
        db_session.add(score)
        await db_session.commit()
        await db_session.refresh(score)
        assert score.id is not None


# ── Phase 3: loser warnings, status classification, AI rewrite endpoint ─────

class TestClassifyPromptStatus:
    def test_winner(self):
        assert classify_prompt_status(75.0, 12) == "winner"

    def test_winner_at_min_sample(self):
        assert classify_prompt_status(60.0, 10) == "winner"

    def test_winner_below_new_min_sample(self):
        # 9 trades is below the >=10 proven-sample bar — not a winner yet
        assert classify_prompt_status(75.0, 9) == "neutral"

    def test_winner_below_threshold_rate(self):
        assert classify_prompt_status(59.9, 10) == "neutral"

    def test_needs_work(self):
        assert classify_prompt_status(20.0, 10) == "needs_work"

    def test_needs_work_at_boundary(self):
        assert classify_prompt_status(39.9, 5) == "needs_work"

    def test_neutral_at_exactly_40(self):
        assert classify_prompt_status(40.0, 10) == "neutral"

    def test_neutral_small_sample_bad_rate(self):
        # 4 trades is below the >=5 sample bar — not flagged
        assert classify_prompt_status(10.0, 4) == "neutral"

    def test_neutral_mixed(self):
        assert classify_prompt_status(50.0, 12) == "neutral"


@pytest.mark.asyncio
class TestUnderperformingStrategies:
    async def test_filters_by_sample_and_rate(self, db_session):
        db_session.add_all([
            StrategyScore(prompt_text="bad", symbol="XAUUSD", direction="buy",
                          source="autopilot", total_trades=10, win_rate=25.0),
            StrategyScore(prompt_text="ok", symbol="XAUUSD", direction="buy",
                          source="autopilot", total_trades=10, win_rate=55.0),
            StrategyScore(prompt_text="tiny-sample", symbol="XAUUSD", direction="buy",
                          source="autopilot", total_trades=2, win_rate=5.0),
            StrategyScore(prompt_text="other-symbol", symbol="BTCUSD", direction="buy",
                          source="autopilot", total_trades=10, win_rate=10.0),
        ])
        await db_session.commit()

        losers = await get_underperforming_strategies("XAUUSD")
        assert [l.prompt_text for l in losers] == ["bad"]

    async def test_orders_worst_first_and_limits(self, db_session):
        db_session.add_all([
            StrategyScore(prompt_text="meh", symbol="XAUUSD", direction="buy",
                          source="autopilot", total_trades=6, win_rate=35.0),
            StrategyScore(prompt_text="worst", symbol="XAUUSD", direction="buy",
                          source="autopilot", total_trades=8, win_rate=12.0),
        ])
        await db_session.commit()

        losers = await get_underperforming_strategies("XAUUSD")
        assert [l.prompt_text for l in losers] == ["worst", "meh"]

    async def test_empty_when_no_match(self, db_session):
        assert await get_underperforming_strategies("XAUUSD") == []


@pytest.mark.asyncio
class TestBuildRagContextLosers:
    async def test_includes_underperforming_section(self, db_session, monkeypatch):
        monkeypatch.setattr("app.core.rag_service.embed_text", lambda t: [0.0, 0.0])
        db_session.add(StrategyScore(prompt_text="fade the breakout always",
                                     symbol="XAUUSD", direction="buy",
                                     source="autopilot", total_trades=12,
                                     win_rate=20.0, total_pnl=-150.0))
        await db_session.commit()

        ctx = await build_rag_context("XAUUSD", "should I buy gold?")
        assert "UNDERPERFORMING STRATEGIES" in ctx
        assert "fade the breakout" in ctx

    async def test_loser_not_listed_as_best(self, db_session, monkeypatch):
        # Only a losing strategy exists — it must appear as a loser,
        # never inside the "BEST PERFORMING" section (contradictory advice).
        monkeypatch.setattr("app.core.rag_service.embed_text", lambda t: [0.0, 0.0])
        db_session.add(StrategyScore(prompt_text="fade the breakout always",
                                     symbol="XAUUSD", direction="buy",
                                     source="autopilot", total_trades=12,
                                     win_rate=20.0, total_pnl=-150.0))
        await db_session.commit()

        ctx = await build_rag_context("XAUUSD", "should I buy gold?")
        assert "UNDERPERFORMING STRATEGIES" in ctx
        assert "BEST PERFORMING STRATEGIES" not in ctx

    async def test_no_loser_section_when_all_healthy(self, db_session, monkeypatch):
        monkeypatch.setattr("app.core.rag_service.embed_text", lambda t: [0.0, 0.0])
        db_session.add(StrategyScore(prompt_text="solid trend follow",
                                     symbol="XAUUSD", direction="buy",
                                     source="autopilot", total_trades=12,
                                     win_rate=70.0, total_pnl=220.0))
        await db_session.commit()

        ctx = await build_rag_context("XAUUSD", "should I buy gold?")
        assert "BEST PERFORMING STRATEGIES" in ctx
        assert "UNDERPERFORMING STRATEGIES" not in ctx


class FakeAsyncOpenAI:
    """Stands in for openai.AsyncOpenAI — returns a canned rewrite."""

    def __init__(self, **kwargs):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        msg = SimpleNamespace(content="REWRITTEN PROMPT: add trend filter and wider stop.")
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


async def _fake_resolve(*args, **kwargs):
    return "fake-key"


async def _noop_scores():
    return None


@pytest.mark.asyncio
class TestStrategyScoresStatus:
    async def test_scores_include_status_field(self, client, auth_headers, db_session, monkeypatch):
        monkeypatch.setattr("app.core.strategy_scorer.update_strategy_scores", _noop_scores)
        db_session.add_all([
            StrategyScore(prompt_text="winner prompt", symbol="XAUUSD", direction="buy",
                          source="autopilot", total_trades=12, winning_trades=9,
                          win_rate=75.0, total_pnl=120.0),
            StrategyScore(prompt_text="loser prompt", symbol="XAUUSD", direction="buy",
                          source="autopilot", total_trades=10, winning_trades=2,
                          win_rate=20.0, total_pnl=-80.0),
            StrategyScore(prompt_text="undecided", symbol="XAUUSD", direction="buy",
                          source="autopilot", total_trades=2, winning_trades=1,
                          win_rate=50.0, total_pnl=5.0),
        ])
        await db_session.commit()

        resp = await client.get("/api/analytics/strategy-scores", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        by_text = {r["prompt_text"]: r for r in resp.json()}
        assert by_text["winner prompt"]["status"] == "winner"
        assert by_text["loser prompt"]["status"] == "needs_work"
        assert by_text["undecided"]["status"] == "neutral"


@pytest.mark.asyncio
class TestRewriteEndpoint:
    async def test_requires_fields(self, client, auth_headers):
        # Missing fields entirely → FastAPI/pydantic rejects with 422
        resp = await client.post("/api/analytics/prompts/rewrite", json={}, headers=auth_headers)
        assert resp.status_code == 422
        # Empty strings pass schema validation → rejected by handler with 400
        resp = await client.post(
            "/api/analytics/prompts/rewrite",
            json={"prompt_text": "", "symbol": ""},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    async def test_404_without_scoreboard_data(self, client, auth_headers):
        resp = await client.post(
            "/api/analytics/prompts/rewrite",
            json={"prompt_text": "unknown", "symbol": "XAUUSD"},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    async def test_requires_auth(self, client):
        resp = await client.post(
            "/api/analytics/prompts/rewrite",
            json={"prompt_text": "p", "symbol": "XAUUSD"},
        )
        assert resp.status_code == 401

    async def test_success_returns_suggestion_without_saving(self, client, auth_headers,
                                                             db_session, monkeypatch):
        db_session.add(StrategyScore(prompt_text="bad prompt", symbol="XAUUSD",
                                     direction="buy", source="autopilot",
                                     total_trades=8, winning_trades=2,
                                     win_rate=25.0, total_pnl=-40.0))
        db_session.add(AutopilotTrade(user_id=1, prompt_number=1, prompt_text="bad prompt",
                                      symbol="XAUUSD", direction="buy", lot_size=0.01,
                                      profit=-12.5, result="Entered on weak signal"))
        await db_session.commit()

        monkeypatch.setattr("app.core.providers.resolve_api_key", _fake_resolve)
        monkeypatch.setattr("openai.AsyncOpenAI", FakeAsyncOpenAI)

        resp = await client.post(
            "/api/analytics/prompts/rewrite",
            json={"prompt_text": "bad prompt", "symbol": "XAUUSD"},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["original"] == "bad prompt"
        assert data["rewritten"].startswith("REWRITTEN PROMPT")
        assert data["stats"]["total_trades"] == 8
        assert data["stats"]["win_rate"] == 25.0
        assert data["losers_analyzed"] == 1

        # Suggestion only: no new personal prompt was created by the rewrite call
        from app.models.ai_memory import UserPrompt
        prompts = (await db_session.execute(select(UserPrompt))).scalars().all()
        assert prompts == []
