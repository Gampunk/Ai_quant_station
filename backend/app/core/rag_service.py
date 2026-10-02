import asyncio
import numpy as np
import logging
from sqlalchemy import text as sql_text, select

from ..core.database import AsyncSessionLocal
from .embed_service import embed_text, compute_similarity
from ..models.chat_embedding import ChatEmbedding
from ..models.ai_memory import ChatMemory

logger = logging.getLogger(__name__)


async def generate_embedding(chat_memory_id: int, text: str):
    loop = asyncio.get_running_loop()
    vector = await loop.run_in_executor(None, embed_text, text)
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


async def find_similar_analyses(query_embedding: list[float], symbol: str, limit: int = 5):
    query_np = np.array(query_embedding, dtype=np.float32)
    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(
                sql_text("""
                    SELECT c.id, c.content, c.detected_setup,
                           t.profit_loss, uf.is_helpful,
                           ce.embedding
                    FROM chat_embeddings ce
                    JOIN chat_memories c ON c.id = ce.chat_memory_id
                    LEFT JOIN trade_records t ON t.ai_message = c.id
                    LEFT JOIN user_feedback uf ON uf.chat_memory_id = c.id
                    WHERE c.symbol = :symbol
                      AND ce.embedding IS NOT NULL
                    ORDER BY c.created_at DESC
                    LIMIT 100
                """),
                {"symbol": symbol}
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
            logger.debug("Skipped a stored embedding that could not be scored", exc_info=True)
            continue

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:limit]


async def get_strategy_scores(symbol: str, limit: int = 3):
    async with AsyncSessionLocal() as db:
        try:
            from ..models.strategy_score import StrategyScore
            result = await db.execute(
                select(StrategyScore)
                .where(StrategyScore.symbol == symbol)
                .order_by(StrategyScore.win_rate.desc())
                .limit(limit)
            )
            return result.scalars().all()
        except Exception as e:
            logger.warning(f"Strategy score fetch failed: {e}")
            return []


async def build_rag_context(symbol: str, user_question: str) -> str:
    loop = asyncio.get_running_loop()
    query_emb = await loop.run_in_executor(None, embed_text, user_question)
    similar = await find_similar_analyses(query_emb, symbol)
    scores = await get_strategy_scores(symbol)

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
        context_parts.append("\nBEST PERFORMING STRATEGIES:\n")
        for s in scores:
            win_rate_str = f"{s.win_rate:.0f}%" if s.win_rate else "N/A"
            total_pnl_str = f"${s.total_pnl:+.2f}" if s.total_pnl else "$0.00"
            context_parts.append(
                f"  \"{s.prompt_text[:60]}...\" : {win_rate_str} win rate "
                f"({s.total_trades} trades, {total_pnl_str})"
            )

    return "\n".join(context_parts)
