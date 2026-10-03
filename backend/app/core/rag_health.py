"""RAG Health & Plateau diagnostics — powers GET /api/rag-health.

Reports the live RAG pipeline configuration (retrieval counts, model,
thresholds, last N [RAG] log entries) and runs four plateau checks that
detect where self-improvement has stalled:

  (a) embedding          — < MIN_EMBEDDINGS_PER_SYMBOL per symbol, or
                           pairwise cosine-similarity variance too flat
  (b) scoreboard         — top_count == 0 for the last SCOREBOARD_WINDOW
                           RAG calls (nothing ever reached min trades)
  (c) prompt_improvement — needs_work prompt that kept losing past the
                           flag bar + IMPROVEMENT_EXTRA_TRADES with no rewrite
  (d) model_routing      — best provider/model win rate below target
                           after ROUTING_MIN_TRADES trades

Every section is isolated in its own try/except: a failing check degrades
to an empty list instead of taking the whole endpoint down.
"""
import logging
import numpy as np
from datetime import timezone
from sqlalchemy import text as sql_text, select

from ..core.database import AsyncSessionLocal
from .rag_service import SIMILAR_COUNT, TOP_COUNT, LOSERS_COUNT, STRIP_CODE_BLOCKS
from .strategy_scorer import (
    MIN_TRADES_FOR_BEST,
    MIN_TRADES_FOR_FLAG,
    FLAG_THRESHOLD,
    classify_prompt_status,
    get_best_model_for_symbol,
)
from .embed_service import EMBEDDING_MODEL_NAME
from ..models.rag_log import RagLog
from ..models.strategy_score import StrategyScore

logger = logging.getLogger(__name__)

# --- Diagnostic tuning (all reported in the config section) -----------------
MIN_EMBEDDINGS_PER_SYMBOL = 20   # (a) fewer than this -> plateau
SIM_VARIANCE_MIN = 0.05          # (a) pairwise sim variance below -> too flat
MIN_VARIANCE_VECTORS = 5         # (a) need at least this many to compute variance
MAX_RAG_LOGS = 50                # how many recent [RAG] entries to return
SCOREBOARD_WINDOW = 10           # (b) last N RAG calls inspected
IMPROVEMENT_EXTRA_TRADES = 10    # (c) flag bar + this many trades with no rewrite
ROUTING_MIN_TRADES = 10          # (d) minimum trades before judging a model
ROUTING_TARGET_WIN_RATE = 55.0   # (d) win rate below this -> plateau
MAX_ROUTING_SYMBOLS = 30         # (d) bound the per-symbol lookups


async def get_rag_config() -> dict:
    """Live RAG configuration + the last MAX_RAG_LOGS [RAG] telemetry rows."""
    logs: list[dict] = []
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(RagLog).order_by(RagLog.id.desc()).limit(MAX_RAG_LOGS)
            )
            for log in result.scalars().all():
                created = log.created_at
                if created is not None and created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                logs.append({
                    "symbol": log.symbol,
                    "similar_count": log.similar_count,
                    "top_count": log.top_count,
                    "losers_count": log.losers_count,
                    "context_chars": log.context_chars,
                    "created_at": created.isoformat() if created else None,
                })
    except Exception as e:
        logger.warning(f"RAG config log fetch failed: {e}")

    return {
        "similar_count": SIMILAR_COUNT,
        "top_count": TOP_COUNT,
        "losers_count": LOSERS_COUNT,
        "embedding_model": EMBEDDING_MODEL_NAME,
        "code_block_stripping": STRIP_CODE_BLOCKS,
        "min_trades_for_best": MIN_TRADES_FOR_BEST,
        "min_trades_for_flag": MIN_TRADES_FOR_FLAG,
        "flag_threshold": FLAG_THRESHOLD,
        "min_embeddings_per_symbol": MIN_EMBEDDINGS_PER_SYMBOL,
        "sim_variance_min": SIM_VARIANCE_MIN,
        "recent_rag_logs": logs,
    }


async def _embedding_counts(db) -> dict[str, int]:
    result = await db.execute(sql_text("""
        SELECT c.symbol as symbol, COUNT(*) as cnt
        FROM chat_embeddings ce
        JOIN chat_memories c ON c.id = ce.chat_memory_id
        WHERE c.symbol IS NOT NULL
        GROUP BY c.symbol
    """))
    return {row.symbol: int(row.cnt) for row in result.fetchall()}


async def get_embedding_plateaus() -> list[dict]:
    """(a) Symbol has too few embeddings, or they are all near-duplicates."""
    plateaus: list[dict] = []
    try:
        async with AsyncSessionLocal() as db:
            counts = await _embedding_counts(db)
            for symbol, count in counts.items():
                if count < MIN_EMBEDDINGS_PER_SYMBOL:
                    plateaus.append({
                        "type": "embedding",
                        "symbol": symbol,
                        "count": count,
                        "status": "plateaued",
                        "reason": (
                            f"Only {count} embeddings, "
                            f"need {MIN_EMBEDDINGS_PER_SYMBOL}+"
                        ),
                    })
                    continue  # too small for a meaningful variance read

                emb_result = await db.execute(
                    sql_text("""
                        SELECT ce.embedding
                        FROM chat_embeddings ce
                        JOIN chat_memories c ON c.id = ce.chat_memory_id
                        WHERE c.symbol = :symbol
                        ORDER BY ce.id DESC
                        LIMIT 100
                    """),
                    {"symbol": symbol},
                )
                vectors = [
                    np.frombuffer(row.embedding, dtype=np.float32)
                    for row in emb_result.fetchall()
                    if row.embedding
                ]
                if len(vectors) < MIN_VARIANCE_VECTORS:
                    continue

                matrix = np.vstack(vectors)
                norms = np.linalg.norm(matrix, axis=1, keepdims=True)
                norms[norms == 0] = 1.0
                matrix = matrix / norms
                sims = matrix @ matrix.T
                upper = sims[np.triu_indices_from(sims, k=1)]
                variance = float(np.var(upper))
                if variance < SIM_VARIANCE_MIN:
                    plateaus.append({
                        "type": "embedding_variance",
                        "symbol": symbol,
                        "variance": round(variance, 4),
                        "status": "plateaued",
                        "reason": (
                            f"Cosine similarity variance {variance:.3f} "
                            f"< {SIM_VARIANCE_MIN} — embeddings too flat"
                        ),
                    })
    except Exception as e:
        logger.warning(f"Embedding plateau check failed: {e}")
    return plateaus


async def get_scoreboard_plateaus() -> list[dict]:
    """(b) Last SCOREBOARD_WINDOW RAG calls for a symbol all had top=0."""
    plateaus: list[dict] = []
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                sql_text("""
                    SELECT symbol, top_count FROM (
                        SELECT symbol, top_count,
                               ROW_NUMBER() OVER (
                                   PARTITION BY symbol ORDER BY id DESC
                               ) AS rn
                        FROM rag_logs
                        WHERE symbol IS NOT NULL
                    ) ranked
                    WHERE rn <= :window
                """),
                {"window": SCOREBOARD_WINDOW},
            )
            per_symbol: dict[str, list[int]] = {}
            for row in result.fetchall():
                per_symbol.setdefault(row.symbol, []).append(int(row.top_count or 0))

            for symbol, tops in per_symbol.items():
                if len(tops) >= SCOREBOARD_WINDOW and all(t == 0 for t in tops):
                    plateaus.append({
                        "type": "scoreboard",
                        "symbol": symbol,
                        "y_top_zero_count": SCOREBOARD_WINDOW,
                        "status": "plateaued",
                        "reason": (
                            f"top=0 for last {SCOREBOARD_WINDOW} RAG calls — "
                            f"no strategy reached {MIN_TRADES_FOR_BEST} trades"
                        ),
                    })
    except Exception as e:
        logger.warning(f"Scoreboard plateau check failed: {e}")
    return plateaus


async def get_improvement_plateaus() -> list[dict]:
    """(c) needs_work prompt still losing past the flag bar + 10 trades."""
    plateaus: list[dict] = []
    try:
        min_total = MIN_TRADES_FOR_FLAG + IMPROVEMENT_EXTRA_TRADES
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(StrategyScore).where(StrategyScore.total_trades > min_total)
            )
            for score in result.scalars().all():
                win_rate = score.win_rate or 0.0
                total = score.total_trades or 0
                if classify_prompt_status(win_rate, total) != "needs_work":
                    continue
                plateaus.append({
                    "type": "prompt_improvement",
                    "symbol": score.symbol,
                    "prompt_text": (score.prompt_text or "")[:80],
                    "total_trades": total,
                    "win_rate": round(win_rate, 1),
                    "status": "plateaued",
                    "reason": (
                        f"needs_work at {total} trades (> {min_total}) "
                        f"— no rewrite detected"
                    ),
                })
    except Exception as e:
        logger.warning(f"Improvement plateau check failed: {e}")
    return plateaus


async def get_model_routing_plateaus() -> list[dict]:
    """(d) Best provider/model for a symbol is under target after enough trades."""
    plateaus: list[dict] = []
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                sql_text("""
                    SELECT DISTINCT symbol
                    FROM autopilot_trades
                    WHERE profit IS NOT NULL AND symbol IS NOT NULL
                    ORDER BY symbol
                    LIMIT :lim
                """),
                {"lim": MAX_ROUTING_SYMBOLS},
            )
            symbols = [row.symbol for row in result.fetchall()]

        for symbol in symbols:
            best = await get_best_model_for_symbol(
                symbol, min_trades=ROUTING_MIN_TRADES
            )
            if not best or best["win_rate"] >= ROUTING_TARGET_WIN_RATE:
                continue
            plateaus.append({
                "type": "model_routing",
                "symbol": symbol,
                "provider": best["provider"],
                "model": best["model"],
                "win_rate": round(best["win_rate"], 1),
                "trades": best["trades"],
                "status": "plateaued",
                "reason": (
                    f"Best model win rate {best['win_rate']:.1f}% "
                    f"< {ROUTING_TARGET_WIN_RATE:.0f}% "
                    f"after {best['trades']} trades"
                ),
            })
    except Exception as e:
        logger.warning(f"Model routing plateau check failed: {e}")
    return plateaus


async def get_per_symbol_stats() -> list[dict]:
    """Embeddings / average win rate / distinct prompts, merged across sources."""
    rows: list[dict] = []
    try:
        async with AsyncSessionLocal() as db:
            emb_counts = await _embedding_counts(db)

            score_rows = await db.execute(sql_text("""
                SELECT symbol,
                       AVG(win_rate) as avg_win_rate,
                       COUNT(DISTINCT prompt_text) as distinct_prompts
                FROM strategy_scores
                WHERE symbol IS NOT NULL
                GROUP BY symbol
            """))
            score_map: dict[str, tuple[float | None, int]] = {}
            for row in score_rows.fetchall():
                score_map[row.symbol] = (
                    float(row.avg_win_rate) if row.avg_win_rate is not None else None,
                    int(row.distinct_prompts or 0),
                )

            trade_rows = await db.execute(sql_text(
                "SELECT DISTINCT symbol FROM autopilot_trades WHERE symbol IS NOT NULL"
            ))
            symbols = (
                set(emb_counts)
                | set(score_map)
                | {row.symbol for row in trade_rows.fetchall()}
            )

            for symbol in sorted(s for s in symbols if s):
                avg_wr, prompts = score_map.get(symbol, (None, 0))
                rows.append({
                    "symbol": symbol,
                    "total_embeddings": emb_counts.get(symbol, 0),
                    "avg_win_rate": round(avg_wr / 100, 2) if avg_wr is not None else None,
                    "distinct_prompts": prompts,
                })
    except Exception as e:
        logger.warning(f"Per-symbol stats failed: {e}")
    return rows


async def get_rag_health() -> dict:
    """Full payload for GET /api/rag-health."""
    config: dict = {}
    plateaus: list[dict] = []
    stats: list[dict] = []

    try:
        config = await get_rag_config()
    except Exception as e:
        logger.warning(f"RAG health config failed: {e}")

    for check in (
        get_embedding_plateaus,
        get_scoreboard_plateaus,
        get_improvement_plateaus,
        get_model_routing_plateaus,
    ):
        try:
            plateaus.extend(await check())
        except Exception as e:
            logger.warning(f"RAG health check {check.__name__} failed: {e}")

    try:
        stats = await get_per_symbol_stats()
    except Exception as e:
        logger.warning(f"RAG health stats failed: {e}")

    return {
        "config": config,
        "plateaus": plateaus,
        "per_symbol_stats": stats,
    }
