"""RAG Health & Plateau Report — config, telemetry, 4 plateau checks, endpoint."""
import numpy as np
import pytest
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.rag_health import (
    get_rag_config,
    get_embedding_plateaus,
    get_scoreboard_plateaus,
    get_improvement_plateaus,
    get_model_routing_plateaus,
    get_per_symbol_stats,
    get_rag_health,
)
from app.core.rag_service import build_rag_context
from app.models.chat_embedding import ChatEmbedding
from app.models.ai_memory import ChatMemory, AutopilotTrade
from app.models.rag_log import RagLog
from app.models.strategy_score import StrategyScore


def _vec(seed: float) -> bytes:
    return np.array([seed, 0.5, 0.1], dtype=np.float32).tobytes()


async def _seed_embeddings(db, symbol: str, vectors: list[bytes]) -> None:
    chats = [
        ChatMemory(user_id=1, symbol=symbol, role="assistant",
                   content=f"analysis {i}")
        for i in range(len(vectors))
    ]
    db.add_all(chats)
    await db.commit()
    rows = (await db.execute(select(ChatMemory).where(
        ChatMemory.symbol == symbol
    ))).scalars().all()
    for chat, emb in zip(rows, vectors):
        db.add(ChatEmbedding(chat_memory_id=chat.id, embedding=emb))
    await db.commit()


def _trade(symbol="XAUUSD", provider="nvidia", model="model-a",
           profit=10.0, prompt_text="p"):
    return AutopilotTrade(
        user_id=1, prompt_number=1, prompt_text=prompt_text,
        symbol=symbol, direction="BUY", lot_size=0.01,
        provider=provider, model=model, profit=profit,
        executed_at=datetime.now(timezone.utc),
    )


# ── Config ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestRagConfig:
    async def test_config_values(self):
        cfg = await get_rag_config()
        assert cfg["similar_count"] == 5
        assert cfg["top_count"] == 3
        assert cfg["losers_count"] == 3
        assert cfg["embedding_model"] == "all-MiniLM-L6-v2"
        assert cfg["code_block_stripping"] is True
        assert cfg["min_trades_for_best"] == 10
        assert cfg["min_trades_for_flag"] == 5
        assert cfg["flag_threshold"] == 0.40
        assert cfg["min_embeddings_per_symbol"] == 20
        assert cfg["sim_variance_min"] == 0.05
        assert cfg["recent_rag_logs"] == []

    async def test_recent_rag_logs_last_50_desc(self, db_session):
        db_session.add_all([
            RagLog(symbol="XAUUSD", similar_count=1, top_count=0,
                   losers_count=0, context_chars=100) for _ in range(3)
        ])
        await db_session.commit()

        cfg = await get_rag_config()
        logs = cfg["recent_rag_logs"]
        assert len(logs) == 3
        assert logs[0]["created_at"] is not None
        # ISO string carries the UTC offset (aware, never naive)
        parsed = datetime.fromisoformat(logs[0]["created_at"])
        assert parsed.tzinfo is not None
        ids_desc = [l["created_at"] for l in logs]
        assert ids_desc == sorted(ids_desc, reverse=True)


# ── Telemetry written by build_rag_context ──────────────────────────────────

@pytest.mark.asyncio
class TestRagLogWrite:
    async def test_build_rag_context_writes_log(self, db_session, monkeypatch):
        monkeypatch.setattr("app.core.rag_service.embed_text", lambda t: [0.0, 0.0])
        db_session.add(StrategyScore(prompt_text="stuck loser", symbol="XAUUSD",
                                     direction="buy", source="autopilot",
                                     total_trades=16, win_rate=25.0,
                                     total_pnl=-90.0))
        await db_session.commit()

        await build_rag_context("XAUUSD", "should I buy gold?")

        logs = (await db_session.execute(select(RagLog))).scalars().all()
        assert len(logs) == 1
        log = logs[0]
        assert log.symbol == "XAUUSD"
        assert log.similar_count == 0
        assert log.top_count == 1          # loser met the >=10 bar
        assert log.losers_count == 1
        assert log.context_chars > 0
        assert log.created_at is not None


# ── (a) Embedding plateaus ──────────────────────────────────────────────────

@pytest.mark.asyncio
class TestEmbeddingPlateau:
    async def test_flags_below_20(self, db_session):
        await _seed_embeddings(db_session, "XAUUSD",
                               [_vec(1.0) for _ in range(8)])

        plateaus = await get_embedding_plateaus()
        entry = next(p for p in plateaus if p["type"] == "embedding")
        assert entry["symbol"] == "XAUUSD"
        assert entry["count"] == 8
        assert "Only 8 embeddings, need 20+" in entry["reason"]

    async def test_no_count_flag_at_20_plus(self, db_session):
        await _seed_embeddings(db_session, "XAUUSD",
                               [_vec(1.0) for _ in range(25)])

        plateaus = await get_embedding_plateaus()
        assert not [p for p in plateaus if p["type"] == "embedding"]

    async def test_flags_flat_variance(self, db_session):
        # 25 identical vectors → passes the count bar, variance = 0 → flagged
        await _seed_embeddings(db_session, "XAUUSD",
                               [_vec(1.0) for _ in range(25)])

        plateaus = await get_embedding_plateaus()
        entry = next(p for p in plateaus if p["type"] == "embedding_variance")
        assert entry["symbol"] == "XAUUSD"
        assert entry["variance"] < 0.05
        assert "embeddings too flat" in entry["reason"]

    async def test_no_variance_flag_for_polarized_corpus(self, db_session):
        # Two opposing clusters → pairwise sims split +1/-1 → variance ~1
        unit = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        vectors = [unit.tobytes()] * 13 + [(-unit).tobytes()] * 12
        await _seed_embeddings(db_session, "XAUUSD", vectors)

        plateaus = await get_embedding_plateaus()
        assert not [p for p in plateaus if p["type"] == "embedding_variance"]


# ── (b) Scoreboard plateaus ─────────────────────────────────────────────────

@pytest.mark.asyncio
class TestScoreboardPlateau:
    async def test_flags_10_zero_top_calls(self, db_session):
        db_session.add_all([
            RagLog(symbol="XAUUSD", similar_count=2, top_count=0,
                   losers_count=0, context_chars=50) for _ in range(10)
        ])
        await db_session.commit()

        plateaus = await get_scoreboard_plateaus()
        entry = next(p for p in plateaus if p["type"] == "scoreboard")
        assert entry["symbol"] == "XAUUSD"
        assert entry["y_top_zero_count"] == 10
        assert "no strategy reached 10 trades" in entry["reason"]

    async def test_no_flag_when_recent_call_had_top(self, db_session):
        # 12 calls: 10 zeros first, then 2 with top=2 — latest 10 include top>0
        db_session.add_all([
            RagLog(symbol="XAUUSD", similar_count=0, top_count=0,
                   losers_count=0, context_chars=0) for _ in range(10)
        ])
        db_session.add_all([
            RagLog(symbol="XAUUSD", similar_count=0, top_count=2,
                   losers_count=0, context_chars=0) for _ in range(2)
        ])
        await db_session.commit()

        plateaus = await get_scoreboard_plateaus()
        assert not [p for p in plateaus if p["type"] == "scoreboard"]

    async def test_no_flag_below_window(self, db_session):
        db_session.add_all([
            RagLog(symbol="XAUUSD", similar_count=0, top_count=0,
                   losers_count=0, context_chars=0) for _ in range(9)
        ])
        await db_session.commit()

        plateaus = await get_scoreboard_plateaus()
        assert not [p for p in plateaus if p["type"] == "scoreboard"]


# ── (c) Prompt-improvement plateaus ─────────────────────────────────────────

@pytest.mark.asyncio
class TestImprovementPlateau:
    async def test_flags_stuck_needs_work(self, db_session):
        db_session.add(StrategyScore(prompt_text="fade every breakout",
                                     symbol="XAUUSD", direction="buy",
                                     source="autopilot",
                                     total_trades=16, win_rate=30.0,
                                     total_pnl=-200.0))
        await db_session.commit()

        plateaus = await get_improvement_plateaus()
        entry = next(p for p in plateaus if p["type"] == "prompt_improvement")
        assert entry["symbol"] == "XAUUSD"
        assert entry["total_trades"] == 16
        assert "no rewrite detected" in entry["reason"]

    async def test_no_flag_within_extra_band(self, db_session):
        # needs_work, but only 8 trades (<= 5 + 10) — too soon to call it stuck
        db_session.add(StrategyScore(prompt_text="early loser",
                                     symbol="XAUUSD", direction="buy",
                                     source="autopilot",
                                     total_trades=8, win_rate=25.0,
                                     total_pnl=-40.0))
        await db_session.commit()

        plateaus = await get_improvement_plateaus()
        assert not [p for p in plateaus if p["type"] == "prompt_improvement"]

    async def test_no_flag_for_healthy_prompt(self, db_session):
        db_session.add(StrategyScore(prompt_text="solid winner",
                                     symbol="XAUUSD", direction="buy",
                                     source="autopilot",
                                     total_trades=16, win_rate=70.0,
                                     total_pnl=300.0))
        await db_session.commit()

        plateaus = await get_improvement_plateaus()
        assert not [p for p in plateaus if p["type"] == "prompt_improvement"]


# ── (d) Model-routing plateaus ──────────────────────────────────────────────

@pytest.mark.asyncio
class TestRoutingPlateau:
    async def test_flags_low_win_rate_model(self, db_session):
        # 10 trades, 4 wins → best model 40% < 55% target
        db_session.add_all(
            [_trade(profit=10) for _ in range(4)]
            + [_trade(profit=-5) for _ in range(6)]
        )
        await db_session.commit()

        plateaus = await get_model_routing_plateaus()
        entry = next(p for p in plateaus if p["type"] == "model_routing")
        assert entry["symbol"] == "XAUUSD"
        assert entry["win_rate"] == 40.0
        assert entry["trades"] == 10
        assert "< 55%" in entry["reason"]

    async def test_no_flag_when_healthy(self, db_session):
        db_session.add_all(
            [_trade(profit=10) for _ in range(7)]
            + [_trade(profit=-5) for _ in range(3)]
        )
        await db_session.commit()

        plateaus = await get_model_routing_plateaus()
        assert not [p for p in plateaus if p["type"] == "model_routing"]

    async def test_no_flag_below_min_trades(self, db_session):
        db_session.add_all([_trade(profit=-5) for _ in range(9)])
        await db_session.commit()

        plateaus = await get_model_routing_plateaus()
        assert not [p for p in plateaus if p["type"] == "model_routing"]


# ── Per-symbol stats ────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestPerSymbolStats:
    async def test_merges_embeddings_and_scores(self, db_session):
        await _seed_embeddings(db_session, "XAUUSD", [_vec(1.0)] * 3)
        db_session.add(StrategyScore(prompt_text="p1", symbol="XAUUSD",
                                     direction="buy", source="autopilot",
                                     total_trades=10, win_rate=62.0))
        await db_session.commit()

        rows = await get_per_symbol_stats()
        by_symbol = {r["symbol"]: r for r in rows}
        xau = by_symbol["XAUUSD"]
        assert xau["total_embeddings"] == 3
        assert xau["avg_win_rate"] == pytest.approx(0.62, abs=1e-6)
        assert xau["distinct_prompts"] == 1

    async def test_trade_only_symbol_appears_with_defaults(self, db_session):
        db_session.add(_trade(symbol="BTCUSD"))
        await db_session.commit()

        rows = await get_per_symbol_stats()
        by_symbol = {r["symbol"]: r for r in rows}
        assert by_symbol["BTCUSD"]["total_embeddings"] == 0
        assert by_symbol["BTCUSD"]["avg_win_rate"] is None
        assert by_symbol["BTCUSD"]["distinct_prompts"] == 0


# ── Endpoint ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestRagHealthEndpoint:
    async def test_requires_auth(self, client):
        resp = await client.get("/api/rag-health")
        assert resp.status_code == 401

    async def test_returns_payload_shape(self, client, auth_headers):
        resp = await client.get("/api/rag-health", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert set(data) == {"config", "plateaus", "per_symbol_stats"}
        assert data["config"]["min_trades_for_best"] == 10
        assert isinstance(data["plateaus"], list)
        assert isinstance(data["per_symbol_stats"], list)

    async def test_plateaus_flow_end_to_end(self, client, auth_headers, db_session):
        # 8 embeddings (count plateau) + 10 zero-top logs (scoreboard plateau)
        await _seed_embeddings(db_session, "XAUUSD", [_vec(1.0)] * 8)
        db_session.add_all([
            RagLog(symbol="XAUUSD", similar_count=0, top_count=0,
                   losers_count=0, context_chars=0) for _ in range(10)
        ])
        await db_session.commit()

        resp = await client.get("/api/rag-health", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        types = {p["type"] for p in resp.json()["plateaus"]}
        assert "embedding" in types
        assert "scoreboard" in types

    async def test_full_report_composes(self, db_session):
        report = await get_rag_health()
        assert set(report) == {"config", "plateaus", "per_symbol_stats"}
        assert report["config"]["embedding_model"] == "all-MiniLM-L6-v2"
