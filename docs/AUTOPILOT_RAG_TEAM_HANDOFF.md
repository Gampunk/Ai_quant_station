# Autopilot Trade Lifecycle and RAG: Team Handoff

**Lifecycle baseline:** `31a49c6` — `Track autopilot order lifecycle and improve RAG telemetry`; scheduled-attempt gate tracking was added in `8a6e888`.
**Deployment status from the shared server output:** database migrations reached `b37c8e2a91d4`; the backend restarted and began Autopilot cycle `#11580`; the connector was online and returned HTTP 200 for a market-data request.
**Current verification boundary:** startup, migration, and market-data connectivity are confirmed. The provided logs do not yet show an Autopilot order being placed and then followed through fill and close, so end-to-end broker lifecycle tracking still needs a real order lifecycle in the logs/report.

## Executive summary

This release makes each new Autopilot analysis cycle easier to follow from prompt selection through AI analysis and, when an order is submitted, through MT5 order state, deal events, and final realized result. It also records what RAG retrieved for that cycle so the team can assess whether historical context was available and included.

This is an observability and decision-support improvement. RAG supplies historical evidence to the model, and the prompt selector uses regime and historical performance to weight prompt selection. Neither mechanism guarantees profitable decisions. We must review closed-trade outcomes and data coverage before changing strategy prompts.

## The full Autopilot cycle

### 1. Start and identify a cycle

At the start of each scheduled check, the backend creates a unique UUID `cycle_id` and a durable `AutopilotCycle` row, before cooldown, daily-loss, connector, and fresh-market-data gates. Early exits are stored as explicit outcomes such as `skipped_cooldown`, `daily_loss_limit`, `skipped_no_connector`, or `skipped_stale_market_data`. Checks that pass those gates keep the same cycle ID through prompt selection, analysis, execution, and outcome recording. The row records the user, cycle number, start time, and progressively adds the symbol, prompt, regime, market data details, AI provider/model, decision, execution, and outcome fields.

The UUID is the stable join key across cycle telemetry, RAG retrieval logs, execution attempts, Autopilot trade records, and broker order/deal events. The cycle number is useful for human-readable logs, but it is not as reliable as the UUID for joins.

### 2. Load market data and classify conditions

The backend loads candles from the configured MT5 connector and derives market context such as trend, volatility, directional bias, and ATR. It records the timeframe, candle count, market-data hash, and regime details on the cycle record.

If market data or MT5 initialization is unavailable, the cycle can finish with a corresponding outcome instead of silently appearing to be a completed trade decision.

### 3. Choose a prompt

The eligible prompt pool comes from configured default and personal prompts. The selector tags prompts by trading style, estimates fit with the current regime, and adds bounded historical-performance contributions where enough closed trades exist for that prompt in the same regime. It then samples using the resulting weights, rather than always choosing the top-scoring prompt.

The cycle stores the selected prompt number/text, a hash of the prompt text (to distinguish revisions), the market regime, the selection score/probability, and candidate/reason context. That lets the team later ask which prompt version was considered and why it was selected.

### 4. Build RAG context

For Autopilot, the RAG query is built from the selected strategy prompt and is scoped to the current user and symbol. The service:

1. Embeds the query text.
2. Searches recent assistant analysis memories for that user and symbol (including broker symbol suffix variants) that have stored embeddings.
3. Scores semantic similarity, with bounded adjustments for realized P&L linked to that analysis and user feedback when available.
4. Retrieves eligible strategy scoreboard rows for the symbol: better-performing strategies and sufficiently sampled underperformers.
5. Builds context labeling retrieved material as historical evidence, not instructions, and tells the model to judge it against current market conditions.
6. Writes a RAG telemetry row tied to the cycle, with hashes, counts, selected memory IDs/similarity/score, and context length.

When non-empty, that context is added to the AI analysis prompt. Retrieval is best-effort: if the RAG operation fails, the cycle logs a warning and continues without RAG context. An empty or absent context is therefore something to measure, not proof that the cycle failed.

**Important distinction:** the semantic memory search retrieves past assistant analyses. The prompt-performance scoreboard is a separate aggregate of closed trades grouped by prompt, symbol, direction, and source. Both can inform the prompt, but they are different evidence sources.

### 5. Ask AI and record the decision path

The model receives current market data, the selected strategy, Autopilot decision context, and any RAG context. Calls and retries/fallbacks are logged by cycle with provider, model, stage, outcome, token counts, cost/latency where available. The cycle records whether RAG context was included and how long it was.

The model may find no trade setup, generate an invalid/failed response, or return a setup. These are different outcomes and should not be counted as equivalent to a broker order. A setup can also be rejected by validation or by the broker.

### 6. Submit and identify an MT5 order

When a setup passes validation, the backend submits it through the configured connector. It persists an Autopilot trade record with prompt number/text, cycle UUID, symbol/direction/order type, requested parameters, provider/model and market context, plus the returned MT5 identifiers and status.

The cycle UUID is also shortened into the MT5 order comment as a correlation marker. The full UUID remains in the backend. The separate `mt5_order_ticket` field is important because an order ticket (especially for a pending order) is not necessarily the same as the position ticket created after a fill.

An `ORDER_SUBMITTED` event is written to `autopilot_order_events`. Event keys are unique so repeated polling does not duplicate the same event.

### 7. Track pending orders, fills, and broker deals

The sync/reconciliation path checks active and historical MT5 orders. It updates pending-order state, including filled, partially filled, cancelled, expired, or rejected where the connector reports those statuses. It then relates resulting positions/deals to the stored order ticket and, where available, the cycle marker in the broker comment.

Broker order-state and deal observations are appended to the event ledger, with order/deal/position identifiers, event type, broker time, entry type, reason code/text, volume, price, P&L, swap, and commission when supplied by MT5.

The order ticket, position ticket, and deal ticket represent different objects. Keeping them separate avoids treating a pending order as if it were already an open position.

### 8. Reconcile closure and realized result

When MT5 history shows close deals and the position is no longer open, the backend sums close-deal profit, swap, and commission; stores the exit price and close time; classifies the result and exit reason (for example, TP/SL when the broker supplies a reason); and records whether the reason came from broker data or a fallback classification. It updates both the Autopilot trade and its cycle record.

Partial close events are recorded, but the trade is not treated as finally closed while the position remains open. The periodic reconciler also helps capture trades closed outside the application, such as by broker-side SL/TP or manual action.

## What the database now records

- `autopilot_cycles`: one durable record per analysis, with selected prompt/version, regime, AI/RAG context metadata, decision and execution outcome, and final trade result when applicable.
- `autopilot_trades`: the Autopilot's trade record, including prompt/cycle linkage, order and position tickets, status, and closed-trade result fields.
- `autopilot_order_events`: deduplicated order/deal event ledger joined by cycle and prompt, with broker identifiers/reasons and event times.
- `rag_logs`: per-cycle retrieval telemetry, including user/symbol/source, hashes, selected memory metadata, result counts, and context size.
- Existing AI call and execution-attempt logs: provider/model call outcomes and rejected/failed execution attempts.

The deployed migration sequence reported by the server was:

```text
e4a7b9c2d1f3
  -> f4a91b7c2e60  Autopilot cycle telemetry and stable cycle IDs
  -> c81d2a6b7e40  User/cycle-scoped RAG retrieval metadata
  -> 9d72c4a1e6f0  MT5 order ticket and lifecycle fields
  -> b37c8e2a91d4  Order/deal event ledger and exit-reason fields
  -> a6d91c4e2b70  Broker quote, accepted protection levels, and execution diagnostics
```

## How we will analyze performance

The report command is run on the server because that is where the live database and logs are:

```bash
cd /opt/impulse_analyst/backend
source venv/bin/activate
python -m scripts.prompt_performance_report --days 90
```

It creates a timestamped folder under `backend/prompt_reports/`. The report includes:

- `prompt_summary.csv`: prompt-level selections, decisions, orders, closed results and performance.
- `prompt_regime_summary.csv`: prompt performance split by symbol and market regime.
- `decision_records.csv`: per-trade prompt/cycle identifiers, order tickets/status, close reason and P&L.
- `execution_attempts.csv`: failed/rejected execution attempts.
- `ai_calls.csv`: provider/model outcomes, stages, token use, cost and latency where available.
- `cycle_records.csv`: full durable analysis cycle, including selection context and RAG-included/context-size fields.
- `rag_retrievals.csv`: what RAG retrieved per cycle and how much context it built.
- `order_events.csv`: broker order/deal event timeline.
- `report_summary.json`: reporting window and coverage counts/notes.

For decisions about prompt quality, use **closed trades** and compare sample size, net P&L, win rate, profit factor, drawdown and regime. A prompt with one or two closed trades is weak evidence; use a larger sample and avoid treating win rate alone as sufficient. Check whether events and closes are missing before interpreting a low count as poor performance.

## What the latest server output confirms

From the output shared so far:

- The database was upgraded successfully through `b37c8e2a91d4`.
- `impulse-analyst` restarted and was active.
- The backend rebuilt 308 strategy-score rows and began cycle `#11580`.
- The backend fetched XAUUSD candles and selected Strategy #29 in a bullish regime.
- The MT5 connector connected to the OctaFX demo account and answered market-data requests with HTTP 200.

Those are good deployment and connectivity checks. The shown connector output was only a market-data request, and the shared cycle log did not show a placed order, fill, or close. Therefore the end-to-end order lifecycle feature has not yet been demonstrated by the evidence shared here.

## Next verification steps

1. Confirm the updated connector build from this release is installed and running on the MT5 host. The Windows EXE is distributed separately from the Git backend commit.
2. Let normal Autopilot activity run; use a demo environment for any deliberately controlled order check.
3. After an Autopilot order is submitted, generate a short-window report and verify the same cycle UUID/prompt appears in `cycle_records.csv`, `decision_records.csv`, and `order_events.csv`.
4. For a pending order, verify the order ticket/status first, then a fill or terminal state. For a filled position, wait for its eventual close and verify close-deal reason/P&L on the same cycle.
5. Check that the associated `rag_retrievals.csv` row has the same cycle UUID and that `cycle_records.csv` reports whether context was included. A retrieval with zero similar memories can be valid; it means no eligible embedded memory/scoreboard context was selected at that point.
6. Review report coverage counts before judging prompt performance. Use only the generated report files when sharing externally; they are intended to omit API keys and raw error messages.

## Caveats and interpretation

- The stable UUID linkage is for new cycles recorded after deployment. Older logs/trades may lack it and can only be joined approximately using legacy cycle numbers, tickets, or comments.
- Prompt selection is weighted sampling, so a high score raises selection probability but does not make a prompt deterministic.
- Strategy scores and RAG context depend on closed trades being reconciled with realized P&L. Open and pending orders do not yet provide a final prompt outcome.
- If MT5 history omits an order/deal, the connector is stale, or the broker strips comments, some correlations may be unavailable. Tickets and cycle IDs provide multiple ways to match, but reports must still expose unmatched coverage.
- One successful cycle or a running service does not establish improved trading performance. Measure after sufficient closed-trade volume and compare like-for-like regimes.

## Files to know

- `backend/app/api/autopilot.py` — cycle lifecycle, prompt selection, RAG injection, order placement, broker sync, and outcome updates.
- `backend/app/core/rag_service.py` — user-scoped semantic retrieval, performance/feedback weighting, context construction and RAG logs.
- `backend/app/core/strategy_scorer.py` — prompt performance aggregation and provider/model performance routing.
- `backend/app/models/ai_memory.py` and `backend/app/models/rag_log.py` — durable cycle/trade/event/RAG data models.
- `backend/scripts/prompt_performance_report.py` — report generation and join/coverage logic.
- `mt5_connector/connector.py` — MT5 connector endpoints/data used to observe active/historical orders and deals; deploy its built executable separately on the MT5 host.
