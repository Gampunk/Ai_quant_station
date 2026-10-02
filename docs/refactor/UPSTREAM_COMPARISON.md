# Upstream comparison, for the step 14 merge

Compared on 2026-10-02. Both branches start at `a92afc5`.

- **Upstream** (`Gautam-813/master`, the live system's code): 23 commits, 57 files, about 8,000 lines added. 1,700 of those lines are in `autopilot.py`.
- **Ours** (`refactor/hardening`): 51 commits, steps 1 to 12.

Nothing here has been merged. This is the input for step 14's decisions.

## 1. Fixed on both sides

Same problem, fixed separately. In step 14 one version is kept.

| Problem | Upstream | Ours | Suggested keep |
|---|---|---|---|
| Connector reported the quote, not the fill | `ee248c6`: connector returns `result.price` | Step 3 and 8: fill stored as `entry_price`, quote as `requested_price` | Ours. It keeps both prices, so slippage is measurable |
| Autopilot loop died on one error | `ee248c6`: cycle wrapped in try/except | Step 10: same, plus the page shows a dead loop as stopped | Ours, then take anything extra from theirs |
| One user's autopilot restart failure stopped the rest | `ee248c6`: per-user handling | Step 12: same | Either; identical idea |
| `/health` answered by the frontend | `7ada34b`: excluded from the catch-all | Step 11: same, and `/docs` too | Ours |
| Connector accepted requests with an empty token | `ee248c6`: `verify_auth` refuses an empty token | Step 3: token required, constant-time compare | Compare line by line in step 14 |
| Hidden errors in stats rebuilding | `ee248c6`: logging added in two places | Step 12: all 64 silent handlers, with a test | Ours |

## 2. Only upstream has it (features to bring in)

| Feature | Commits | Notes for the merge |
|---|---|---|
| Telegram delivery for reports and alerts | `beb6144` | Removes all email code. Our branch still has it. Telegram is their chosen channel |
| External health monitor every 5 minutes | `beb6144` | Catches a dead backend, which nothing in-process can. Alerts every 5 minutes while down, with no "recovered" message, and hardcodes the live connector's public address |
| Live tick detection instead of fixed market hours | `ee248c6` | Better than our time rules. Should feed our heartbeat and autopilot pause |
| Live model lists with a 24 h cache, 402/403 skipping | `09c1790`, `5b8961d`, `85a46c0`, `c760e0a` | No overlap with us |
| RAG health report, telemetry, broker-suffix matching | `807157e`, `49ec81f`, `df97e9b`, `31a49c6` | No overlap. Includes pandas 3 resample fixes we may also need |
| Autopilot cycle telemetry, order lifecycle ledger | `b160056`, `8a6e888`, `47345f5`, `31a49c6` | New tables and columns. Overlaps in meaning with our `risk_decisions` and `requested_price` |
| Profit reconciler, auto-link of trades | `ed6c092` | Needs review against our deal-reason classification (finding 10) |
| Prompt performance report | `cc2e1fa` | A script, no overlap |

## 3. Conflicts to decide in step 14

| Area | Upstream | Ours | Why it matters |
|---|---|---|---|
| Order route | Autopilot posts straight to the connector (`autopilot.py`), Terminal through `trade_service` | One risk gate, `submit_order`, the only caller of `place_order`, test-enforced | Their autopilot path must go through our gate, or the risk limits do not apply |
| Default stop and target | 0.2% stop and target filled in when missing, on all paths (`ed6c092`) | A stop loss is required; the autopilot is sized from equity and the stop | A fixed 0.2% stop changes position size and risk. One policy must win, probably as a risk setting |
| Exit reason | `exit_reason` and `exit_reason_source` columns | Broker deal reason through `close_result` | Same question, two answers |
| Times | Broker time as received | Real UTC everywhere, offset detected live | Their new telemetry would be stored in broker time |
| Database migrations | Five new migrations after `e4a7b9c2d1f3` | Four new migrations after `e4a7b9c2d1f3` | No column is added twice. One alembic merge revision joins them. The live database is on their chain |
| `autopilot.py` | 1,700 changed lines | Step 10 and 12 rework | The hardest file to merge. Best done function by function |

## 4. Security problems still on upstream

These are live on the current system.

| Problem | Our fix |
|---|---|
| Five accounts with passwords written in `main.py`, in a public repository | Step 4 |
| A missing `SECRET_KEY` falls back to a random one | Step 4 |
| No role checks: any account can trade and run AI-written code | Step 4 |
| An endpoint runs code sent by the client | Steps 5 and 7 |
| Logout revokes only the access token; the 7-day refresh token keeps working | Step 6 |
| No connector address guard, no lot cap, no risk gate | Steps 1, 9 |
| The live connector's public address is written in two scripts (`monitor.py`, `generate_master_report.py`), over plain HTTP | Step 14: private network |

## 5. Suggested merge approach for step 14

1. Start from our branch, since it carries the security and risk fixes.
2. Bring upstream's features in, area by area, each through our rules: every order through `submit_order`, times in UTC, no silent errors.
3. Join the migrations with one merge revision, tested against a copy of the live database's schema.
4. Agree the stop and target policy with your colleague before merging `ed6c092`.
5. Rotate every password and token the public history exposed.
