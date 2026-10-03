import logging
from datetime import datetime, timezone
from sqlalchemy import text as sql_text, select

from ..core.database import AsyncSessionLocal
from ..models.strategy_score import StrategyScore
from ..models.ai_memory import AutopilotTrade
from ..core.providers import estimate_cost

logger = logging.getLogger(__name__)

# --- Sample-size thresholds: single source of truth -------------------------
# Reported live by GET /api/rag-health; every caller imports from here so the
# badge, the RAG sections, model routing and autopilot selection can never
# drift apart again.
MIN_TRADES_FOR_BEST = 10   # proven sample before "winner" / best-model status
MIN_TRADES_FOR_FLAG = 5    # meaningful sample before a prompt can be flagged
FLAG_THRESHOLD = 0.40      # win rate below 40% counts as underperforming


def _ensure_aware(dt):
    """Return a timezone-aware datetime; naive values are assumed to be UTC.

    asyncpg rejects naive datetimes when binding to timestamptz columns
    ('can't subtract offset-naive and offset-aware datetimes'), which made
    every scoreboard update fail on PostgreSQL.
    """
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def classify_prompt_status(win_rate: float, total_trades: int) -> str:
    """Classify a prompt for the dashboard and RAG context.

    - winner:       proven sample (>=10 trades) with >=60% win rate
    - needs_work:   meaningful sample (>=5 trades) losing more than 60% of the time
    - neutral:      everything else (insufficient or mixed evidence)
    """
    if total_trades >= MIN_TRADES_FOR_BEST and win_rate >= 60:
        return "winner"
    if total_trades >= MIN_TRADES_FOR_FLAG and win_rate < FLAG_THRESHOLD * 100:
        return "needs_work"
    return "neutral"


def start_strategy_scorer():
    """Register the hourly strategy score update with APScheduler."""
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        scheduler = AsyncIOScheduler()
        scheduler.add_job(update_strategy_scores, 'interval', hours=1)
        scheduler.start()
        logger.info("Strategy Scorer Scheduler initialized (Hourly).")
    except Exception as e:
        logger.warning(f"Strategy Scorer scheduler not available: {e}")


async def update_strategy_scores():
    """Aggregate trade performance by prompt across autopilot_trades and trade_records."""
    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(sql_text("""
                SELECT prompt_text, symbol, direction, source,
                       COUNT(*) as total_trades,
                       SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END) as winning_trades,
                       SUM(profit) as total_pnl,
                       SUM(CASE WHEN profit > 0 THEN profit ELSE 0 END) as gross_profit,
                       SUM(CASE WHEN profit < 0 THEN ABS(profit) ELSE 0 END) as gross_loss,
                       SUM(CASE WHEN profit = 0 THEN 1 ELSE 0 END) as breakeven_trades,
                       AVG(CASE WHEN profit > 0 THEN profit END) as avg_profit,
                       AVG(CASE WHEN profit < 0 THEN profit END) as avg_loss,
                       AVG(confidence) as avg_confidence,
                       MIN(created_at) as first_used,
                       MAX(created_at) as last_used
                FROM (
                    SELECT prompt_text, symbol, direction, 'autopilot' as source,
                           profit, confidence, created_at
                    FROM autopilot_trades
                    WHERE result IS NOT NULL
                    UNION ALL
                    SELECT c.content as prompt_text, t.symbol, t.direction, 'ai_analyst' as source,
                           t.profit_loss as profit, NULL as confidence, t.executed_at as created_at
                    FROM trade_records t
                    JOIN chat_memories c ON cast(c.id as text) = t.ai_message
                    WHERE t.profit_loss IS NOT NULL
                ) AS trade_data
                GROUP BY prompt_text, symbol, direction, source
            """))

            rows = result.fetchall()

            # Compute per-prompt token/cost from autopilot_trades using actual prompt/completion tokens
            token_rows = await db.execute(sql_text("""
                SELECT prompt_text, symbol, direction,
                       SUM(COALESCE(total_tokens,0)) as total_tokens,
                       AVG(COALESCE(total_tokens,0)) as avg_tokens,
                       SUM(COALESCE(prompt_tokens,0)) as total_prompt,
                       SUM(COALESCE(completion_tokens,0)) as total_completion,
                       provider, model
                FROM autopilot_trades
                WHERE result IS NOT NULL AND total_tokens IS NOT NULL
                GROUP BY prompt_text, symbol, direction, provider, model
            """))
            token_map = {}
            for tr in token_rows.fetchall():
                key = (tr.prompt_text, tr.symbol, tr.direction)
                if key not in token_map:
                    token_map[key] = {"total_tokens": 0, "total_cost": 0.0}
                actual_cost = 0.0
                if tr.provider and tr.model:
                    actual_cost = estimate_cost(
                        tr.total_prompt or 0, tr.total_completion or 0,
                        tr.provider, tr.model,
                    )
                token_map[key]["total_tokens"] += tr.total_tokens or 0
                token_map[key]["total_cost"] += actual_cost

            updated_count = 0

            for row in rows:
                prompt_text = row.prompt_text
                symbol = row.symbol
                direction = row.direction
                source = row.source
                total = row.total_trades or 0
                wins = row.winning_trades or 0
                pnl = row.total_pnl or 0.0
                avg_profit = row.avg_profit
                avg_loss = row.avg_loss
                gross_profit = float(row.gross_profit or 0.0)
                gross_loss = float(row.gross_loss or 0.0)
                avg_conf = row.avg_confidence
                first = _ensure_aware(row.first_used)
                last = _ensure_aware(row.last_used)
                win_rate = (wins / total * 100) if total > 0 else 0.0
                if gross_loss > 0:
                    profit_factor = round(gross_profit / gross_loss, 2)
                else:
                    profit_factor = float("inf") if gross_profit > 0 else None

                token_key = (prompt_text, symbol, direction)
                token_data = token_map.get(token_key, {})
                total_tok = token_data.get("total_tokens", 0)
                total_cost_val = token_data.get("total_cost", 0.0)
                avg_tokens = int(total_tok / total) if total > 0 else None
                avg_cost = round(total_cost_val / total, 6) if total > 0 else None

                cost_efficiency = round(win_rate / (avg_cost + 0.0001), 2) if avg_cost is not None else None
                roi_per_dollar = round(avg_profit / (avg_cost + 0.0001), 2) if (avg_cost is not None and avg_profit is not None) else None

                existing = await db.execute(
                    select(StrategyScore).where(
                        StrategyScore.prompt_text == prompt_text,
                        StrategyScore.symbol == symbol,
                        StrategyScore.direction == direction,
                        StrategyScore.source == source,
                    )
                )
                score = existing.scalar_one_or_none()

                if score:
                    score.total_trades = total
                    score.winning_trades = wins
                    score.total_pnl = pnl
                    score.win_rate = win_rate
                    score.avg_confidence = avg_conf
                    score.avg_profit = avg_profit
                    score.avg_loss = avg_loss
                    score.profit_factor = profit_factor
                    score.total_tokens = total_tok
                    score.avg_tokens = avg_tokens
                    score.total_cost = total_cost_val
                    score.avg_cost = avg_cost
                    score.cost_efficiency = cost_efficiency
                    score.roi_per_dollar = roi_per_dollar
                    score.first_used = first
                    score.last_used = last
                    score.updated_at = datetime.now(timezone.utc)
                else:
                    score = StrategyScore(
                        prompt_text=prompt_text,
                        symbol=symbol,
                        direction=direction,
                        source=source,
                        total_trades=total,
                        winning_trades=wins,
                        total_pnl=pnl,
                        win_rate=win_rate,
                        avg_confidence=avg_conf,
                        avg_profit=avg_profit,
                        avg_loss=avg_loss,
                        profit_factor=profit_factor,
                        total_tokens=total_tok,
                        avg_tokens=avg_tokens,
                        total_cost=total_cost_val,
                        avg_cost=avg_cost,
                        cost_efficiency=cost_efficiency,
                        roi_per_dollar=roi_per_dollar,
                        first_used=first,
                        last_used=last,
                    )
                    db.add(score)

                updated_count += 1

            await db.commit()
            if updated_count:
                logger.info(f"Updated {updated_count} strategy scores")

        except Exception as e:
            logger.warning(f"Strategy score update failed: {e}")


async def get_best_model_for_symbol(symbol: str, min_trades: int = MIN_TRADES_FOR_BEST):
    """Return {"provider", "model", "win_rate", "trades"} — the best-performing
    provider/model pair for this symbol, or None when no pair has enough data.

    Data source: autopilot_trades (has provider/model/profit directly). The
    design doc's model_usage x trade_records join is unusable — trade_records
    is empty (0 rows) on the live server.
    """
    if not symbol:
        return None
    base_symbol = symbol.split(".")[0]
    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(
                sql_text("""
                    SELECT provider, model,
                           COUNT(*) as trades,
                           AVG(CASE WHEN profit > 0 THEN 1.0 ELSE 0.0 END) as win_rate
                    FROM autopilot_trades
                    WHERE (symbol = :symbol OR symbol LIKE :symbol || '.%')
                      AND profit IS NOT NULL
                      AND provider IS NOT NULL
                      AND model IS NOT NULL
                    GROUP BY provider, model
                    HAVING COUNT(*) >= :min_trades
                    ORDER BY win_rate DESC, trades DESC
                    LIMIT 1
                """),
                {"symbol": base_symbol, "min_trades": min_trades},
            )
            row = result.fetchone()
            if not row:
                return None
            return {
                "provider": row.provider,
                "model": row.model,
                "win_rate": float(row.win_rate or 0) * 100.0,  # AVG() returns 0..1
                "trades": int(row.trades or 0),
            }
        except Exception as e:
            logger.warning(f"Model routing lookup failed: {e}")
            return None
