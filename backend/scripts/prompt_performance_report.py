#!/usr/bin/env python3
"""Build a read-only, secret-free Autopilot prompt performance report.

Usage from the backend directory:
    python -m scripts.prompt_performance_report
    python -m scripts.prompt_performance_report --days 30
    python -m scripts.prompt_performance_report --user-id 7 --days 90

The default user is the only user whose AutopilotSettings row is enabled.
If that is not unambiguous, pass --user-id explicitly. Output files are
written to backend/prompt_reports/<UTC timestamp>/; nothing is uploaded.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import select, or_  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.models.ai_memory import (  # noqa: E402
    AiCallLog,
    AutopilotCycle,
    AutopilotExecutionAttempt,
    AutopilotLog,
    AutopilotSettings,
    AutopilotTrade,
    AutopilotOrderEvent,
)
from app.models.rag_log import RagLog  # noqa: E402


def _parse_default_prompts(path: Path) -> dict[int, str]:
    """Read both supported prompt_list.txt formats without importing the API."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    prompts: dict[int, str] = {}
    current_num: int | None = None
    current_lines: list[str] = []
    format_mode: str | None = None
    new_header = re.compile(r"^PROMPT\s*#(\d+):?\s*$", re.I)
    old_header = re.compile(r"^(\d+)\.\s*(.*)$")

    def save() -> None:
        if current_num is not None and current_lines:
            prompts[current_num] = " ".join(current_lines).strip()

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        match = new_header.match(line)
        if match:
            format_mode = "new"
            save()
            current_num = int(match.group(1))
            current_lines = []
            continue
        match = old_header.match(line)
        if match and format_mode != "new":
            format_mode = "old"
            save()
            current_num = int(match.group(1))
            current_lines = [match.group(2).strip()] if match.group(2).strip() else []
            continue
        if current_num is not None:
            current_lines.append(re.sub(r"\s+", " ", line))
    save()
    return prompts


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if value is None:
        return ""
    return value


def _write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in columns})


def _profit_metrics(trades: list[AutopilotTrade]) -> dict[str, Any]:
    # A P&L value alone is not proof of final closure (older sync code could
    # record the first partial close). Require a close timestamp and result.
    closed = [t for t in trades if t.profit is not None and t.closed_at is not None and t.result]
    pnls = [float(t.profit) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    # Drawdown from chronological cumulative realized P&L, reset at zero.
    equity = peak = max_drawdown = 0.0
    for trade in sorted(closed, key=lambda t: t.closed_at or t.executed_at or t.created_at):
        equity += float(trade.profit or 0.0)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    return {
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(pnls) - len(wins) - len(losses),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 2) if closed else None,
        "total_pnl": round(sum(pnls), 2),
        "avg_pnl_per_closed_trade": round(sum(pnls) / len(pnls), 2) if pnls else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else (None if not gross_win else "inf"),
        "max_realized_drawdown": round(max_drawdown, 2),
    }


async def _resolve_user_id(requested_user_id: int | None) -> int:
    if requested_user_id is not None:
        return requested_user_id
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AutopilotSettings.user_id).where(AutopilotSettings.enabled.is_(True))
        )
        enabled = sorted(set(result.scalars().all()))
    if len(enabled) != 1:
        raise RuntimeError(
            "Could not identify exactly one enabled Autopilot user. "
            f"Found {len(enabled)}; rerun with --user-id <ID>."
        )
    return int(enabled[0])


async def build_report(user_id: int, days: int | None, output_root: Path) -> Path:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days) if days else None
    default_prompts = _parse_default_prompts(REPO_DIR / "backend" / "prompt_list.txt")

    async with AsyncSessionLocal() as db:
        settings_row = (await db.execute(
            select(AutopilotSettings).where(AutopilotSettings.user_id == user_id)
        )).scalar_one_or_none()

        trade_query = select(AutopilotTrade).where(AutopilotTrade.user_id == user_id)
        log_query = select(AutopilotLog).where(AutopilotLog.user_id == user_id)
        attempt_query = select(AutopilotExecutionAttempt).where(AutopilotExecutionAttempt.user_id == user_id)
        call_query = select(AiCallLog).where(AiCallLog.user_id == user_id)
        cycle_query = select(AutopilotCycle).where(AutopilotCycle.user_id == user_id)
        order_event_query = select(AutopilotOrderEvent).where(AutopilotOrderEvent.user_id == user_id)
        if cutoff:
            trade_query = trade_query.where(or_(
                AutopilotTrade.executed_at >= cutoff,
                AutopilotTrade.closed_at >= cutoff,
            ))
            log_query = log_query.where(AutopilotLog.timestamp >= cutoff)
            attempt_query = attempt_query.where(AutopilotExecutionAttempt.created_at >= cutoff)
            call_query = call_query.where(AiCallLog.created_at >= cutoff)
            cycle_query = cycle_query.where(or_(
                AutopilotCycle.started_at >= cutoff,
                AutopilotCycle.trade_closed_at >= cutoff,
            ))
            order_event_query = order_event_query.where(or_(
                AutopilotOrderEvent.broker_time >= cutoff,
                AutopilotOrderEvent.observed_at >= cutoff,
            ))

        trades = list((await db.execute(trade_query.order_by(AutopilotTrade.executed_at))).scalars().all())
        logs = list((await db.execute(log_query.order_by(AutopilotLog.timestamp))).scalars().all())
        attempts = list((await db.execute(attempt_query.order_by(AutopilotExecutionAttempt.created_at))).scalars().all())
        calls = list((await db.execute(call_query.order_by(AiCallLog.created_at))).scalars().all())
        cycles = list((await db.execute(cycle_query.order_by(AutopilotCycle.started_at))).scalars().all())
        order_events = list((await db.execute(
            order_event_query.order_by(AutopilotOrderEvent.broker_time, AutopilotOrderEvent.observed_at)
        )).scalars().all())
        rag_query = select(RagLog).where(RagLog.user_id == user_id, RagLog.cycle_id.is_not(None))
        if cutoff:
            rag_query = rag_query.where(RagLog.created_at >= cutoff)
        rag_logs = list((await db.execute(rag_query.order_by(RagLog.created_at))).scalars().all())

    selected_by_prompt: Counter[int] = Counter()
    selected_cycles_by_prompt: dict[int, set[tuple[int | None, str]]] = defaultdict(set)
    selected_prompt_by_cycle: dict[int, set[int]] = defaultdict(set)
    prompt_by_cycle_id = {c.cycle_id: c.prompt_number for c in cycles if c.prompt_number is not None}
    started_cycles: set[tuple[int | None, str]] = set()
    for log in logs:
        cycle_key = (log.cycle_number, log.timestamp.isoformat() if log.timestamp else "")
        message = log.message or ""
        if "=== Starting Cycle #" in message:
            if not log.cycle_id:
                started_cycles.add(cycle_key)
        match = re.search(r"Using Strategy\s+#(\d+)|Using Strategy\s+Custom-(\d+)", message)
        if match:
            prompt_id = int(match.group(1)) if match.group(1) else -int(match.group(2))
            if not log.cycle_id:
                selected_by_prompt[prompt_id] += 1
                selected_cycles_by_prompt[prompt_id].add(cycle_key)
                if log.cycle_number is not None:
                    selected_prompt_by_cycle[int(log.cycle_number)].add(prompt_id)

    for cycle in cycles:
        started_cycles.add((cycle.cycle_number, cycle.started_at.isoformat() if cycle.started_at else cycle.cycle_id))
        if cycle.prompt_number is not None:
            selected_by_prompt[cycle.prompt_number] += 1
            selected_cycles_by_prompt[cycle.prompt_number].add((cycle.cycle_number, cycle.cycle_id))
            selected_prompt_by_cycle[cycle.cycle_number].add(cycle.prompt_number)

    grouped: dict[tuple[int, str, str], list[AutopilotTrade]] = defaultdict(list)
    for trade in trades:
        grouped[(int(trade.prompt_number), trade.symbol or "", trade.prompt_text or "")].append(trade)

    attempts_by_key: Counter[tuple[int, str]] = Counter()
    for attempt in attempts:
        # Prefer exact UUID attribution; legacy rows retain approximate cycle-number matching.
        if attempt.cycle_id and attempt.cycle_id in prompt_by_cycle_id:
            matched = [prompt_by_cycle_id[attempt.cycle_id]]
        else:
            matched = list(selected_prompt_by_cycle.get(int(attempt.cycle_number), set())) if attempt.cycle_number is not None else []
        if len(matched) == 1:
            attempts_by_key[(matched[0], attempt.symbol or "")] += 1

    decisions_by_key: dict[tuple[int, str], list[AutopilotTrade]] = defaultdict(list)
    for trade in trades:
        decisions_by_key[(int(trade.prompt_number), trade.symbol or "")].append(trade)

    current_selected = set(settings_row.selected_prompts or []) if settings_row else set()
    # Empty selection means all default prompts are eligible; selected custom prompt IDs
    # are encoded as custom_<id> in settings and negative prompt numbers in trade rows.
    prompt_keys: set[tuple[int, str]] = {(num, "") for num in default_prompts}
    prompt_keys.update((pn, symbol) for pn, symbol, _ in grouped)
    prompt_keys.update((pn, symbol) for pn, symbol in attempts_by_key)
    prompt_keys.update((pn, "") for pn in selected_by_prompt)
    prompt_keys.update((int(c.prompt_number), "") for c in calls if c.prompt_number is not None)

    summary_rows: list[dict[str, Any]] = []
    regime_groups: dict[tuple[int, str, str], list[AutopilotTrade]] = defaultdict(list)
    for (prompt_num, symbol, prompt_text), rows in grouped.items():
        regime_groups[(prompt_num, symbol, "")].extend(rows)
        for trade in rows:
            regime = trade.market_regime or "unknown"
            regime_groups[(prompt_num, symbol, regime)].append(trade)

    for prompt_num, symbol in sorted(prompt_keys):
        matching = [t for t in trades if int(t.prompt_number) == prompt_num and (not symbol or t.symbol == symbol)]
        current_text = default_prompts.get(prompt_num, "") if prompt_num > 0 else ""
        snapshots = sorted({t.prompt_text for t in matching if t.prompt_text})
        text = current_text or (Counter(t.prompt_text for t in matching if t.prompt_text).most_common(1)[0][0] if matching else "")
        selected_count = selected_by_prompt[prompt_num]
        decision_count = len(matching)
        no_setup_count = sum(1 for t in matching if (t.decision_type or "").upper() == "NO_SETUP")
        executed_count = sum(1 for t in matching if (t.execution_status or "").lower() == "executed")
        pending_order_count = sum(1 for t in matching if (t.execution_status or "").lower() == "pending")
        active_partial_count = sum(1 for t in matching if (t.order_status or "").lower() == "partially_filled_active")
        cancelled_order_count = sum(1 for t in matching if (t.order_status or "").lower() in ("cancelled", "canceled"))
        expired_order_count = sum(1 for t in matching if (t.order_status or "").lower() == "expired")
        prompt_attempts = attempts_by_key[(prompt_num, symbol)] if symbol else sum(v for (pn, _), v in attempts_by_key.items() if pn == prompt_num)
        metrics = _profit_metrics(matching)
        if prompt_num > 0:
            eligible = (not current_selected) or prompt_num in current_selected or str(prompt_num) in current_selected
            display = f"#{prompt_num}"
        else:
            custom_id = f"custom_{abs(prompt_num)}"
            eligible = (not current_selected) or custom_id in current_selected
            display = f"Custom-{abs(prompt_num)}"
        summary_rows.append({
            "prompt_id": display,
            "prompt_number": prompt_num,
            "symbol": symbol,
            "currently_in_selected_pool": eligible if settings_row else None,
            # Selection logs are not symbol-tagged, so report them only on the
            # all-symbol aggregate row instead of repeating the same count per symbol.
            "observed_selection_log_count": selected_count if not symbol else None,
            "decision_records": decision_count,
            "no_setup_decisions": no_setup_count,
            "executed_trades_recorded": executed_count,
            "pending_broker_orders": pending_order_count,
            "active_partial_fill_orders": active_partial_count,
            "cancelled_broker_orders": cancelled_order_count,
            "expired_broker_orders": expired_order_count,
            "execution_failure_attempts_matched": prompt_attempts,
            "prompt_text_current_or_observed": text,
            "observed_prompt_text_versions": len(snapshots),
            **metrics,
        })

    summary_rows.sort(key=lambda r: (r["prompt_number"], r["symbol"]))
    regime_rows = []
    for (prompt_num, symbol, regime), rows in sorted(regime_groups.items()):
        if not regime:
            continue
        regime_rows.append({
            "prompt_number": prompt_num,
            "prompt_id": f"#{prompt_num}" if prompt_num > 0 else f"Custom-{abs(prompt_num)}",
            "symbol": symbol,
            "market_regime": regime,
            **_profit_metrics(rows),
        })

    decision_rows = []
    for t in trades:
        decision_rows.append({
            "id": t.id,
            "cycle_id": t.cycle_id,
            "prompt_number": t.prompt_number,
            "prompt_id": f"#{t.prompt_number}" if t.prompt_number > 0 else f"Custom-{abs(t.prompt_number)}",
            "prompt_text": t.prompt_text,
            "symbol": t.symbol,
            "decision_type": t.decision_type,
            "execution_status": t.execution_status,
            "mt5_order_ticket": t.mt5_order_ticket,
            "mt5_position_ticket": t.mt5_ticket,
            "order_status": t.order_status,
            "order_completed_at": t.order_completed_at.isoformat() if t.order_completed_at else "",
            "direction": t.direction,
            "market_regime": t.market_regime,
            "decision_score": t.decision_score,
            "confidence": t.confidence,
            "entry_price": t.entry_price,
            "stop_loss": t.stop_loss,
            "take_profit": t.take_profit,
            "exit_price": t.exit_price,
            "profit": t.profit,
            "result": t.result,
            "exit_reason": t.exit_reason,
            "exit_reason_source": t.exit_reason_source,
            "reasoning": t.reasoning,
            "executed_at": t.executed_at.isoformat() if t.executed_at else "",
            "closed_at": t.closed_at.isoformat() if t.closed_at else "",
            "cycle_number": t.cycle_number,
            "provider": t.provider,
            "model": t.model,
        })

    attempt_rows = [{
        "id": a.id,
        "cycle_id": a.cycle_id,
        "cycle_number": a.cycle_number,
        "symbol": a.symbol,
        "direction": a.direction,
        "order_type": a.order_type,
        "outcome": a.outcome,
        "market_regime": a.market_regime,
        "provider": a.provider,
        "model": a.model,
        # Deliberately omit error_message: provider/broker exceptions can contain sensitive request details.
        "created_at": a.created_at.isoformat() if a.created_at else "",
    } for a in attempts]

    call_rows = [{
        "id": c.id,
        "cycle_id": c.cycle_id,
        "prompt_number": c.prompt_number,
        "cycle_number": c.cycle_number,
        "provider": c.provider,
        "model": c.model,
        "stage": c.stage,
        "outcome": c.outcome,
        "prompt_tokens": c.prompt_tokens,
        "completion_tokens": c.completion_tokens,
        "total_tokens": c.total_tokens,
        "cost": c.cost,
        "latency_ms": c.latency_ms,
        # Deliberately omit error_message for secret-safe sharing.
        "created_at": c.created_at.isoformat() if c.created_at else "",
    } for c in calls]

    cycle_rows = [{
        "cycle_id": c.cycle_id,
        "cycle_number": c.cycle_number,
        "started_at": c.started_at.isoformat() if c.started_at else "",
        "completed_at": c.completed_at.isoformat() if c.completed_at else "",
        "status": c.status,
        "outcome": c.outcome,
        "symbol": c.symbol,
        "prompt_number": c.prompt_number,
        "prompt_version": c.prompt_version,
        "analysis_prompt_hash": c.analysis_prompt_hash,
        "market_data_hash": c.market_data_hash,
        "decision_source": c.decision_source,
        "rag_context_included": c.rag_context_included,
        "rag_context_chars": c.rag_context_chars,
        "provider": c.provider,
        "model": c.model,
        "market_regime": c.market_regime,
        "market_trend": (c.regime_details or {}).get("trend"),
        "market_volatility": (c.regime_details or {}).get("volatility"),
        "directional_bias": (c.regime_details or {}).get("direction_bias"),
        "regime_confidence": (c.regime_details or {}).get("confidence"),
        "selected_prompt_score": (c.selection_context or {}).get("selected_score"),
        "selected_prompt_probability": (c.selection_context or {}).get("selected_probability"),
        "candidate_count": (c.selection_context or {}).get("candidate_count"),
        "selection_candidates": (c.selection_context or {}).get("candidates"),
        "market_timeframe": c.market_timeframe,
        "candles_loaded": c.candles_loaded,
        "atr_14": c.atr_14,
        "avg_atr_20": c.avg_atr_20,
        "setup_direction": (c.setup or {}).get("direction"),
        "entry_price": (c.setup or {}).get("entry_price"),
        "stop_loss": (c.setup or {}).get("stop_loss"),
        "take_profit": (c.setup or {}).get("take_profit"),
        "requested_lot_size": c.requested_lot_size,
        "final_lot_size": c.final_lot_size,
        "execution_status": c.execution_status,
        "mt5_order_ticket": c.mt5_order_ticket,
        "mt5_position_ticket": c.mt5_ticket,
        "order_status": c.order_status,
        "order_completed_at": c.order_completed_at.isoformat() if c.order_completed_at else "",
        "trade_result": c.trade_result,
        "exit_reason": c.exit_reason,
        "exit_reason_source": c.exit_reason_source,
        "realized_profit": c.realized_profit,
        "trade_closed_at": c.trade_closed_at.isoformat() if c.trade_closed_at else "",
        "duration_minutes": c.duration_minutes,
    } for c in cycles]

    rag_rows = [{
        "rag_log_id": row.id,
        "cycle_id": row.cycle_id,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "symbol": row.symbol,
        "source": row.source,
        "query_hash": row.query_hash,
        "context_hash": row.context_hash,
        "similar_count": row.similar_count,
        "top_count": row.top_count,
        "losers_count": row.losers_count,
        "context_chars": row.context_chars,
        "selected_memories": row.selected_memories,
    } for row in rag_logs]

    order_event_rows = [{
        "event_id": event.id,
        "event_key": event.event_key,
        "autopilot_trade_id": event.autopilot_trade_id,
        "cycle_id": event.cycle_id,
        "prompt_number": event.prompt_number,
        "symbol": event.symbol,
        "event_type": event.event_type,
        "status": event.status,
        "order_ticket": event.order_ticket,
        "deal_ticket": event.deal_ticket,
        "position_id": event.position_id,
        "entry_type": event.entry_type,
        "reason_code": event.reason_code,
        "reason": event.reason,
        "volume": event.volume,
        "price": event.price,
        "profit": event.profit,
        "swap": event.swap,
        "commission": event.commission,
        "broker_time": event.broker_time.isoformat() if event.broker_time else "",
        "observed_at": event.observed_at.isoformat() if event.observed_at else "",
        "comment": event.comment,
    } for event in order_events]

    selected_cycle_count = len({cycle for cycles in selected_cycles_by_prompt.values() for cycle in cycles})
    decision_cycle_count = len({t.cycle_id or (t.cycle_number, t.prompt_number) for t in trades if t.cycle_id or t.cycle_number is not None})
    matched_attempt_count = sum(attempts_by_key.values())
    report = {
        "generated_at_utc": now.isoformat(),
        "user_id": user_id,
        "period_days": days,
        "period_start_utc": cutoff.isoformat() if cutoff else "all time",
        "period_end_utc": now.isoformat(),
        "default_prompt_count_in_file": len(default_prompts),
        "currently_selected_prompt_ids": sorted(current_selected, key=str),
        "empty_selected_list_means_all_defaults_eligible": bool(settings_row is not None and not current_selected),
        "observed_cycles_started": len(started_cycles),
        "observed_cycles_started_in_persisted_logs": len(started_cycles),
        "cycles_with_durable_outcome_records": sum(1 for c in cycles if c.status == "completed" and c.outcome),
        "cycles_still_running_or_incomplete": sum(1 for c in cycles if c.status != "completed" or not c.outcome),
        "cycle_outcome_counts": dict(Counter(c.outcome or "missing_outcome" for c in cycles)),
        "closed_trade_cycles": sum(
            1 for c in cycles if c.realized_profit is not None and c.trade_closed_at is not None and c.trade_result
        ),
        "unverified_or_open_trade_rows": sum(
            1 for t in trades if t.execution_status == "executed"
            and (t.profit is None or t.closed_at is None or not t.result)
        ),
        "pending_broker_orders": sum(1 for t in trades if t.execution_status == "pending"),
        "orders_filled": sum(1 for t in trades if (t.order_status or "").lower() in ("filled", "partially_filled", "partially_filled_active")),
        "orders_with_active_partial_fill": sum(1 for t in trades if (t.order_status or "").lower() == "partially_filled_active"),
        "orders_cancelled": sum(1 for t in trades if (t.order_status or "").lower() in ("cancelled", "canceled")),
        "orders_expired": sum(1 for t in trades if (t.order_status or "").lower() == "expired"),
        "orders_rejected": sum(1 for t in trades if (t.order_status or "").lower() == "rejected"),
        "broker_order_event_rows": len(order_events),
        "close_deal_events_with_native_reason": sum(
            1 for event in order_events
            if event.entry_type in ("CLOSE", "INOUT", "OUT_BY") and event.reason not in (None, "UNKNOWN")
        ),
        "observed_prompt_selection_events": selected_cycle_count,
        "observed_prompt_selection_events_in_persisted_logs": selected_cycle_count,
        "cycles_with_autopilot_trade_decision_records": decision_cycle_count,
        "execution_attempt_rows": len(attempts),
        "execution_attempts_matched_to_prompt_by_cycle": matched_attempt_count,
        "ai_call_log_rows": len(calls),
        "coverage_note": (
            "New cycles have durable UUID-linked outcome rows. Pending broker orders have distinct order tickets and are reconciled against active and historical MT5 orders; actual fills and closes are matched through MT5 deals and positions. Log-only legacy cycles are still inferred from persisted messages. "
            "Legacy cycle-number joins remain approximate. New execution attempts are attributed by cycle UUID. "
            "Cycle records omit raw provider and broker error messages."
        ),
        "files": [
            "prompt_summary.csv",
            "prompt_regime_summary.csv",
            "decision_records.csv",
            "execution_attempts.csv",
            "ai_calls.csv",
            "cycle_records.csv",
            "rag_retrievals.csv",
            "order_events.csv",
        ],
    }

    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    output_dir = output_root / timestamp
    output_dir.mkdir(parents=True, exist_ok=False)
    _write_csv(output_dir / "prompt_summary.csv", summary_rows, [
        "prompt_id", "prompt_number", "symbol", "currently_in_selected_pool",
        "observed_selection_log_count", "decision_records", "no_setup_decisions",
        "executed_trades_recorded", "pending_broker_orders", "active_partial_fill_orders", "cancelled_broker_orders",
        "expired_broker_orders", "execution_failure_attempts_matched",
        "closed_trades", "wins", "losses", "breakeven", "win_rate_pct", "total_pnl",
        "avg_pnl_per_closed_trade", "avg_win", "avg_loss", "profit_factor",
        "max_realized_drawdown", "observed_prompt_text_versions", "prompt_text_current_or_observed",
    ])
    _write_csv(output_dir / "prompt_regime_summary.csv", regime_rows, [
        "prompt_number", "prompt_id", "symbol", "market_regime", "closed_trades",
        "wins", "losses", "breakeven", "win_rate_pct", "total_pnl",
        "avg_pnl_per_closed_trade", "avg_win", "avg_loss", "profit_factor", "max_realized_drawdown",
    ])
    _write_csv(output_dir / "decision_records.csv", decision_rows, [
        "id", "cycle_id", "prompt_number", "prompt_id", "prompt_text", "symbol", "decision_type",
        "execution_status", "direction", "market_regime", "decision_score", "confidence",
        "mt5_order_ticket", "mt5_position_ticket", "order_status", "order_completed_at",
        "entry_price", "stop_loss", "take_profit", "exit_price", "profit", "result",
        "exit_reason", "exit_reason_source",
        "reasoning", "executed_at", "closed_at", "cycle_number", "provider", "model",
    ])
    _write_csv(output_dir / "execution_attempts.csv", attempt_rows, [
        "id", "cycle_id", "cycle_number", "symbol", "direction", "order_type", "outcome",
        "market_regime", "provider", "model", "created_at",
    ])
    _write_csv(output_dir / "ai_calls.csv", call_rows, [
        "id", "cycle_id", "prompt_number", "cycle_number", "provider", "model", "stage", "outcome",
        "prompt_tokens", "completion_tokens", "total_tokens", "cost", "latency_ms", "created_at",
    ])
    _write_csv(output_dir / "cycle_records.csv", cycle_rows, [
        "cycle_id", "cycle_number", "started_at", "completed_at", "status", "outcome",
        "symbol", "prompt_number", "prompt_version", "provider", "model", "market_regime",
        "market_trend", "market_volatility", "directional_bias", "regime_confidence", "selected_prompt_score",
        "selected_prompt_probability", "candidate_count", "selection_candidates",
        "analysis_prompt_hash", "market_data_hash", "decision_source", "rag_context_included",
        "rag_context_chars", "market_timeframe", "candles_loaded", "atr_14", "avg_atr_20", "setup_direction",
        "entry_price", "stop_loss", "take_profit", "requested_lot_size", "final_lot_size",
        "execution_status", "trade_result", "realized_profit", "trade_closed_at", "duration_minutes",
        "exit_reason", "exit_reason_source",
        "mt5_order_ticket", "mt5_position_ticket", "order_status", "order_completed_at",
    ])
    _write_csv(output_dir / "rag_retrievals.csv", rag_rows, [
        "rag_log_id", "cycle_id", "created_at", "symbol", "source", "query_hash",
        "context_hash", "similar_count", "top_count", "losers_count", "context_chars",
        "selected_memories",
    ])
    _write_csv(output_dir / "order_events.csv", order_event_rows, [
        "event_id", "event_key", "autopilot_trade_id", "cycle_id", "prompt_number", "symbol",
        "event_type", "status", "order_ticket", "deal_ticket", "position_id", "entry_type",
        "reason_code", "reason", "volume", "price", "profit", "swap", "commission",
        "broker_time", "observed_at", "comment",
    ])
    (output_dir / "report_summary.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a secret-free Autopilot prompt performance report.")
    parser.add_argument("--user-id", type=int, default=None, help="Autopilot user ID; auto-detects exactly one enabled user if omitted")
    parser.add_argument("--days", type=int, default=90, help="Lookback period in days (default: 90); use 0 for all available history")
    parser.add_argument("--output-dir", type=Path, default=BACKEND_DIR / "prompt_reports", help="Output root directory")
    args = parser.parse_args()
    if args.days < 0:
        parser.error("--days must be >= 0")

    async def run() -> None:
        user_id = await _resolve_user_id(args.user_id)
        path = await build_report(user_id, args.days or None, args.output_dir)
        print(f"Prompt performance report created: {path}")
        print("Files: prompt_summary.csv, prompt_regime_summary.csv, decision_records.csv,")
        print("       execution_attempts.csv, ai_calls.csv, cycle_records.csv,")
        print("       rag_retrievals.csv, report_summary.json")
        print("Review and share only these generated files; they contain no API keys or raw error messages.")

    asyncio.run(run())


if __name__ == "__main__":
    main()
