import asyncio
import re
import numpy as np
import logging
from sqlalchemy import text as sql_text, select, or_

from ..core.database import AsyncSessionLocal
from .embed_service import embed_text, compute_similarity
from .strategy_scorer import MIN_TRADES_FOR_BEST, MIN_TRADES_FOR_FLAG, FLAG_THRESHOLD
from ..models.chat_embedding import ChatEmbedding
from ..models.ai_memory import ChatMemory
from ..models.rag_log import RagLog

logger = logging.getLogger(__name__)

# RAG retrieval sizes — single source of truth, reported by GET /api/rag-health
SIMILAR_COUNT = 5   # X: similar past analyses injected
TOP_COUNT = 3       # Y: best-performing strategies injected
LOSERS_COUNT = 3    # Z: underperforming strategies injected
STRIP_CODE_BLOCKS = True  # strip ``` fences before embedding

_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)


def _clean_for_embedding(text: str) -> str:
    """Strip fenced code blocks before embedding (when STRIP_CODE_BLOCKS is on).

    AI responses often contain large Python/chart code sections that dilute
    the semantic signal of the actual analysis text.
    """
    if not text:
        return ""
    cleaned = _CODE_BLOCK_RE.sub(" ", text) if STRIP_CODE_BLOCKS else text
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # MiniLM truncates at 256 wordpieces; keep the head of the analysis
    return cleaned[:4000]


async def generate_embedding(chat_memory_id: int, text: str):
    embedding_text = _clean_for_embedding(text)
    if not embedding_text:
        return
    loop = asyncio.get_running_loop()
    vector = await loop.run_in_executor(None, embed_text, embedding_text)
    async with AsyncSessionLocal() as db:
        try:
            existing = await db.execute(
                sql_text("SELECT id FROM chat_embeddings WHERE chat_memory_id = :id"),
                {"id": chat_memory_id}
            )
            if not existing.fetchone():
                await db.execute(
                    sql_text(
                        "INSERT INTO chat_embeddings (chat_memory_id, embedding) VALUES (:id, :emb)"
                    ),
                    {"id": chat_memory_id, "emb": np.array(vector, dtype=np.float32).tobytes()}
                )
                await db.commit()
        except Exception as e:
            logger.warning(f"Failed to store embedding for chat_memory_id={chat_memory_id}: {e}")


async def find_similar_analyses(query_embedding: list[float], symbol: str, limit: int = SIMILAR_COUNT):
    query_np = np.array(query_embedding, dtype=np.float32)
    # Match both plain and broker-suffixed variants in either direction:
    # query "XAUUSD" finds XAUUSD.p; query "XAUUSD.p" finds plain XAUUSD.
    base_symbol = symbol.split(".")[0] if symbol else symbol
    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(
                sql_text("""
                    SELECT c.id, c.content, c.detected_setup,
                           t.profit_loss, uf.is_helpful,
                           ce.embedding
                    FROM chat_embeddings ce
                    JOIN chat_memories c ON c.id = ce.chat_memory_id
                    LEFT JOIN trade_records t ON cast(c.id as text) = t.ai_message
                    LEFT JOIN user_feedback uf ON uf.chat_memory_id = c.id
                    WHERE (c.symbol = :symbol OR c.symbol LIKE :symbol || '.%')
                      AND ce.embedding IS NOT NULL
                    ORDER BY c.created_at DESC
                    LIMIT 100
                """),
                {"symbol": base_symbol}
            )
            rows = result.fetchall()
        except Exception as e:
            logger.warning(f"Similarity search query failed: {e}")
            return []

    scored = []
    for row in rows:
        try:
            emb = np.frombuffer(row.embedding, dtype=np.float32)
            sim = compute_similarity(query_np, emb)
            profit = row.profit_loss or 0
            helpful = 1 if row.is_helpful else 0
            score = sim * 0.5 + (min(profit / 100, 1)) * 0.3 + helpful * 0.2
            scored.append((score, row))
        except Exception:
            continue

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:limit]


async def get_strategy_scores(symbol: str, limit: int = TOP_COUNT):
    base_symbol = symbol.split(".")[0] if symbol else symbol
    async with AsyncSessionLocal() as db:
        try:
            from ..models.strategy_score import StrategyScore
            result = await db.execute(
                select(StrategyScore)
                .where(
                    or_(StrategyScore.symbol == base_symbol,
                        StrategyScore.symbol.like(f"{base_symbol}.%")),
                    StrategyScore.total_trades >= MIN_TRADES_FOR_BEST,
                )
                .order_by(StrategyScore.win_rate.desc())
                .limit(limit)
            )
            return result.scalars().all()
        except Exception as e:
            logger.warning(f"Strategy score fetch failed: {e}")
            return []


async def get_underperforming_strategies(symbol: str, limit: int = LOSERS_COUNT):
    """Prompts with a meaningful sample and a poor win rate for this symbol."""
    base_symbol = symbol.split(".")[0] if symbol else symbol
    async with AsyncSessionLocal() as db:
        try:
            from ..models.strategy_score import StrategyScore
            result = await db.execute(
                select(StrategyScore)
                .where(
                    or_(StrategyScore.symbol == base_symbol,
                        StrategyScore.symbol.like(f"{base_symbol}.%")),
                    StrategyScore.total_trades >= MIN_TRADES_FOR_FLAG,
                    StrategyScore.win_rate < FLAG_THRESHOLD * 100,
                )
                .order_by(StrategyScore.win_rate.asc())
                .limit(limit)
            )
            return result.scalars().all()
        except Exception as e:
            logger.warning(f"Underperforming strategy fetch failed: {e}")
            return []


async def build_rag_context(symbol: str, user_question: str) -> str:
    loop = asyncio.get_running_loop()
    query_emb = await loop.run_in_executor(None, embed_text, user_question)
    similar = await find_similar_analyses(query_emb, symbol)
    scores = await get_strategy_scores(symbol)
    losers = await get_underperforming_strategies(symbol)

    context_parts = []

    if similar:
        context_parts.append("RELEVANT PAST ANALYSES:\n")
        for idx_item in similar:
            score, row = idx_item
            profit_tag = ""
            if row.profit_loss is not None:
                profit_tag = f"PROFIT: ${row.profit_loss:+.2f}"
            elif row.is_helpful is not None:
                profit_tag = "FEEDBACK: Helpful" if row.is_helpful else "FEEDBACK: Not Helpful"

            content_preview = (row.content or "")[:200]
            context_parts.append(
                f"[Analysis #{row.id}] {profit_tag}\n"
                f"  {content_preview}..."
            )

    if scores:
        # A loser must not also be listed as a top strategy (contradictory advice)
        loser_texts = {s.prompt_text for s in losers}
        top = [s for s in scores if s.prompt_text not in loser_texts]
        if top:
            context_parts.append("\nBEST PERFORMING STRATEGIES:\n")
            for s in top:
                win_rate_str = f"{s.win_rate:.0f}%" if s.win_rate else "N/A"
                total_pnl_str = f"${s.total_pnl:+.2f}" if s.total_pnl else "$0.00"
                context_parts.append(
                    f"  \"{s.prompt_text[:60]}...\" : {win_rate_str} win rate "
                    f"({s.total_trades} trades, {total_pnl_str})"
                )

    if losers:
        context_parts.append(
            "\nUNDERPERFORMING STRATEGIES (their approach has been losing — do NOT repeat it, propose an alternative):\n"
        )
        for s in losers:
            win_rate_str = f"{s.win_rate:.0f}%" if s.win_rate else "N/A"
            total_pnl_str = f"${s.total_pnl:+.2f}" if s.total_pnl else "$0.00"
            context_parts.append(
                f"  \"{s.prompt_text[:60]}...\" : {win_rate_str} win rate "
                f"({s.total_trades} trades, {total_pnl_str})"
            )

    context = "\n".join(context_parts)
    logger.info(
        f"[RAG] {symbol}: {len(similar)} similar, {len(scores)} top, "
        f"{len(losers)} losers -> {len(context)} chars"
    )
    # Best-effort telemetry for the RAG Health panel — never blocks/fails the pipeline
    try:
        async with AsyncSessionLocal() as db:
            db.add(RagLog(
                symbol=symbol,
                similar_count=len(similar),
                top_count=len(scores),
                losers_count=len(losers),
                context_chars=len(context),
            ))
            await db.commit()
    except Exception as e:
        logger.warning(f"RAG log insert failed: {e}")
    return context
