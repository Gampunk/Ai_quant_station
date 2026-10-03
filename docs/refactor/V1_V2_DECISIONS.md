# Version 1 (live) vs Version 2 (refactor): comparison and decisions

**Approved on 2026-10-03** by both developers. Technical detail per commit is in
`UPSTREAM_COMPARISON.md`.

**Version 1** is `Gautam-813/master`, the code running live: 24 commits since the branches split.
**Version 2** is `Gampunk/refactor/hardening`: steps 1 to 13.

**In one sentence:** Version 2 is much safer and more reliable; Version 1 has more features around AI
models, RAG and autopilot reporting. **Decision:** Version 2 is the base, and Version 1's features
are brought into it, each passing through Version 2's safety rules.

## 1. Security

| Area | Version 1 | Version 2 | Decision |
|---|---|---|---|
| Built-in accounts | Five accounts with passwords written in the code, in a public repository | None. The admin is created from a private setting; others with a tool or the Users page | **V2.** Change those passwords on the live site now |
| Login signing key | If missing, a random one is invented and every login breaks | The server refuses to start without a strong key | **V2** |
| Who can do what | Any logged-in account can trade and run AI-written code | Three roles: admin, trader, viewer | **V2** |
| Running code sent from the browser | An endpoint runs whatever code it receives | Endpoint removed. AI code runs only inside the app's own features, in a separate process | **V2** |
| Logout | Only the 15-minute token is cancelled; the 7-day token keeps working | Both cancelled. A password change also logs out every other device | **V2** |
| Login protection | Basic limit per address | Per address and per account, real address read correctly behind nginx | **V2** |
| Connector token | Refused when empty | Required, compared without leaking timing | **V2** |
| Connector address | Public internet address, plain HTTP, written in two scripts | Public addresses refused unless deliberately allowed | **V2**, plus a private network link (VPN) |
| Order size cap | None | Connector refuses anything over 1 lot | **V2** |
| Secrets in the Docker image | n/a | Checked: none get in | **V2** |
| HTTPS | No | No | **Both need it** before a public launch |

## 2. Trading safety

| Area | Version 1 | Version 2 | Decision |
|---|---|---|---|
| Path of an order | Two routes; the autopilot goes straight to the connector | One route: every order passes the risk gate, enforced by a test | **V2** |
| Risk limits | None | Per-trade risk, daily loss, margin level, open trades, pending distance; editable and versioned | **V2** |
| Position size | From the AI's suggestion | From account equity and stop distance | **V2** |
| Stop loss | Filled in at 0.2% of price when missing | Required | **Required, plus an optional setting (off by default) that fills a missing stop at an ATR multiple** |
| Emergency stop | None | Kill switch and "Close all trades" | **V2** |
| Two orders at once | Both can pass the same checks | Checked one at a time | **V2** |
| Daily loss baseline | n/a | Equity recorded at 00:00 UTC | **V2** |

## 3. Reliability

| Area | Version 1 | Version 2 | Decision |
|---|---|---|---|
| Autopilot loop crash on one error | Fixed | Fixed, and the page shows why it stopped | **V2** |
| Market open detection | Live price ticks | Fixed weekday hours | **V1** |
| Times | Broker local time stored as UTC | Real UTC; offset detected live | **V2** |
| Stop/target hit detection | `exit_reason` columns | Broker's deal reason, one helper | **V2's method writing into V1's columns** |
| Fill price | Real fill | Real fill and requested price (slippage) | **V2** |
| Hidden errors | A few fixed | All fixed; a test blocks new ones | **V2** |
| Price data sync | Reports success on failure | Reports failure; no 1970 download on 1 January | **V2** |
| Database migrations | Five new | Five new | **Join both** with one merge migration |

## 4. Monitoring and alerts

| Area | Version 1 | Version 2 | Decision |
|---|---|---|---|
| Channel | Telegram | Telegram | **Telegram only; remove email** |
| What is watched | Backend, connector, service, from outside, every 5 min | Connector, live prices, each autopilot, from inside, every minute | **Keep both** |
| Alert behaviour | Repeats every 5 min; no recovery message | One alert per problem, one per recovery | **V2's behaviour for both** |
| Reports | Telegram | Email | **V1** |

## 5. Version 1 features brought into Version 2

Live AI model lists with a 24-hour cache and payment-error skipping; RAG health report, retrieval
telemetry and broker-suffix matching (with the pandas 3 fixes); autopilot cycle telemetry, order
lifecycle record and joined cycle history; profit reconciler and automatic trade linking (using
V2's deal reason and UTC); prompt performance report.

## 6. Approved decisions

- [x] Version 2 as the base
- [x] Bring in all Version 1 features in section 5
- [x] Stop loss: required, plus optional ATR fill-in (instead of the fixed 0.2%)
- [x] Market-open detection by live ticks (from Version 1)
- [x] Telegram only; remove email
- [x] Keep both monitors, with one alert per problem and one per recovery
- [x] Change the published passwords and the connector token on the live site now (colleague, on the live servers)
- [x] HTTPS and a private connector link before the public launch
- [x] Side-by-side test with separate demo accounts and databases, then switch
