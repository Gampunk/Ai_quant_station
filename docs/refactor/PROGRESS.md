# Refactor progress

Branch `refactor/hardening`. One entry per step. A step is done only after you run its checks yourself.

## Step 1. Test setup and local safety guard

**Status:** verified by you on 2026-09-14. All four checks passed, including the negative control with 12 failures.

**Commits:** `ca1c1f1` test setup and baseline, `b8f1b0b` connector guard

**What changed**
- The test suite runs, and `scripts/verify.sh` checks everything in one command. See `BASELINE.md`.
- The server refuses to start when `MT5_CONNECTOR_URL` points outside local or private networks.
- The autopilot rejects saving or connecting to such an address with HTTP 400.
- Every autopilot connector request and the shared connector client check the address before sending.
- Override with `ALLOW_REMOTE_CONNECTOR=true`, only on purpose.

**Your checks**

1. Everything passes.
   ```bash
   ./scripts/verify.sh
   ```
   Expect four PASS lines. The backend shows 32 passed and 9 skipped.

2. The server refuses a public connector address.
   ```bash
   cd backend
   SECRET_KEY=k MT5_CONNECTOR_URL=http://8.8.8.8:5001 .venv/bin/python -m uvicorn app.main:app --port 8765
   ```
   Expect `ConnectorAddressBlocked` and `Application startup failed`.

3. The server accepts a local connector address.
   ```bash
   SECRET_KEY=k MT5_CONNECTOR_URL=http://127.0.0.1:5001 .venv/bin/python -m uvicorn app.main:app --port 8765
   ```
   Expect `Application startup complete`. Stop it with Ctrl+C.

**Negative control**

In `backend/app/core/connector_guard.py`, add `return` as the first line inside `check_connector_url`, then run the guard tests.

```bash
cd backend
SECRET_KEY=k .venv/bin/python -m pytest tests/test_connector_guard.py -q
```

Expect 12 failures. Restore the file afterwards.

```bash
git checkout -- app/core/connector_guard.py
```

## Step 2. Fake connector, Python 3.14, demo connection

**Status:** verified by you on 2026-09-18. All four checks passed, including a full round trip on the
OctaFX demo account: 0.01 lot XAUUSD opened with stop and target, stop modified, position closed,
both deals present in history. The run exposed finding 14.

**What changed**
- Connector dependencies pinned to versions with Windows builds for Python 3.14, and three unused packages removed.
- Connector listens on `127.0.0.1` by default. Set `MT5_CONNECTOR_HOST` to change it.
- Connector refuses order, close and modify with HTTP 403 unless MT5 reports a demo account. Only `MT5_REQUIRE_DEMO=false` turns this off.
- A fake MetaTrader5 terminal lets the real `connector.py` run in WSL with no broker.
- 28 connector tests run on Python 3.14. Two backend tests run the backend against the fake connector end to end.
- `scripts/demo_check.py` checks a real connector on your demo account.
- New problems found are listed in `FINDINGS.md`, numbers 6 to 13.

**Your checks**

1. Five PASS lines.
   ```bash
   ./scripts/verify.sh
   ```

2. Full trade round trip against the fake connector. Start it in one terminal:
   ```bash
   mt5_connector/.venv/bin/python mt5_connector/testing/run_fake_connector.py --port 5001
   ```
   In a second terminal:
   ```bash
   backend/.venv/bin/python scripts/demo_check.py --trade
   ```
   Expect `All stages passed`. Stop the fake connector with Ctrl+C.

3. Your real demo account. Follow `LOCAL_DEMO_SETUP.md`, then run `demo_check.py` read-only, then with `--trade`.

**Negative control**

In `mt5_connector/connector.py`, add `return` as the first line inside `require_demo_account()`, then run:

```bash
mt5_connector/.venv/bin/python -m pytest mt5_connector/tests -q
```

Expect 4 failures. Restore with `git checkout -- mt5_connector/connector.py`.

## Step 3. Connector security, and the fill price

**Status:** verified by you on 2026-09-20. Checks 1 to 3 passed, including both negative controls.
Check 4, the fill price against the real demo account, is deferred to a later session.

**What changed**
- The token is read from the `Authorization` header on all 14 endpoints, compared in constant time. It used to be declared as a plain argument, which FastAPI reads from the query string, so the header every client sends was ignored. That is why the live connector runs with no token at all.
- No token configured now refuses every request with 503. `MT5_ALLOW_NO_TOKEN=true` opts out, for an isolated instance such as the fake terminal.
- `/docs`, `/redoc` and `/openapi.json` are off unless `MT5_ENABLE_DOCS=true`. The docs page is an interactive order form.
- Order and close report the broker's fill price, plus `requested_price`. They used to report the quote seen beforehand. This is the connector half of finding 14.
- Two frontend tests were making real network calls and asserting nothing. They now use a fake transport and check real behaviour. Mirrored networking made unused ports hang instead of refusing, which is how this surfaced.

**Your checks**

1. Five PASS lines.
   ```bash
   ./scripts/verify.sh
   ```

2. Token required, and the old broken behaviour gone. Start the fake connector with a token:
   ```bash
   mt5_connector/.venv/bin/python mt5_connector/testing/run_fake_connector.py --port 5001 --token mytoken
   ```
   In a second terminal, each of these should print what the comment says:
   ```bash
   curl -s -o /dev/null -w "no token: %{http_code}\n" http://127.0.0.1:5001/health
   curl -s -o /dev/null -w "query string: %{http_code}\n" "http://127.0.0.1:5001/health?authorization=mytoken"
   curl -s -o /dev/null -w "wrong token: %{http_code}\n" -H "Authorization: Bearer nope" http://127.0.0.1:5001/health
   curl -s -o /dev/null -w "correct token: %{http_code}\n" -H "Authorization: Bearer mytoken" http://127.0.0.1:5001/health
   curl -s -o /dev/null -w "docs page: %{http_code}\n" http://127.0.0.1:5001/docs
   ```
   Expect 401, 401, 401, 200, 404.

3. Filled price against your demo account. Follow `LOCAL_DEMO_SETUP.md`, which now includes the token step, then:
   ```bash
   backend/.venv/bin/python scripts/demo_check.py --trade
   ```
   The order line shows `filled` and `quoted`. The filled price must match the OPEN deal printed at the end.

**Negative controls**

Auth: add `return True` as the first line of `verify_auth` in `mt5_connector/connector.py`. Expect 18 failures.

Fill price: change both `filled_price = result.price if ...` lines to `filled_price = price`. Expect 3 failures.

```bash
mt5_connector/.venv/bin/python -m pytest mt5_connector/tests -q
git checkout -- mt5_connector/connector.py
```

## Step 4. Signing key, accounts, permissions

**Status:** verified by you on 2026-09-21. Checks 1 to 4 passed. The negative controls in check 5 were skipped
by you and run by Claude before handover: 14 failures with the permission check off, 1 with the random-key bug back.

**What changed**
- The server refuses to start unless `SECRET_KEY` is at least 32 characters and not the example placeholder. The old code fell back to a new random key on every call.
- On a fresh database, only `admin` is created, only from a strong `DEFAULT_ADMIN_PASSWORD`. The four accounts with passwords written in the code are gone, including from `setup_postgres.py`.
- `create_admin.py` adds a person or resets a password, and prompts for it.
- Trading, autopilot changes, AI chat, prompt backtests and historical lab runs need the admin or trader role. Viewers can still read.
- Trade endpoints no longer accept the shared connector token, so every trade belongs to a logged-in person.
- The endpoint that ran any Python sent to it is deleted. Nothing in the app used it.
- Saving your own AI provider key works. It used to crash.
- `backend/.env` was generated for you with a random signing key and admin password. It is not committed.

**Before checking:** your old local database was created with the old accounts. Delete it so a fresh one is made:

```bash
rm -f ~/dev/Ai_quant_station/backend/finance_engine.db
```

**Your checks.** All from `~/dev/Ai_quant_station/backend`.

1. Five PASS lines. The backend now takes about six minutes.
   ```bash
   ../scripts/verify.sh
   ```

2. No strong key, no server. Each should end with `Application startup failed`:
   ```bash
   SECRET_KEY= .venv/bin/python -m uvicorn app.main:app --port 8765
   SECRET_KEY=short .venv/bin/python -m uvicorn app.main:app --port 8765
   ```

3. Only admin exists, the old password is dead, the new one works. Start the server and leave it running:
   ```bash
   .venv/bin/python -m uvicorn app.main:app --port 8765
   ```
   Look for `Admin account created from DEFAULT_ADMIN_PASSWORD`. In a second terminal:
   ```bash
   cd ~/dev/Ai_quant_station/backend
   ADMIN_PW=$(grep ^DEFAULT_ADMIN_PASSWORD .env | cut -d= -f2-)
   curl -s -o /dev/null -w "old published password: %{http_code}\n" -X POST localhost:8765/api/auth/login -H "content-type: application/json" -d '{"username":"admin","password":"admin@2026"}'
   curl -s -o /dev/null -w "your admin password: %{http_code}\n" -X POST localhost:8765/api/auth/login -H "content-type: application/json" -d "{\"username\":\"admin\",\"password\":\"$ADMIN_PW\"}"
   curl -s -o /dev/null -w "old guest account: %{http_code}\n" -X POST localhost:8765/api/auth/login -H "content-type: application/json" -d '{"username":"guest","password":"Usdt@2026"}'
   ```
   Expect 401, 200, 401.

4. A viewer cannot trade. Stop the server with Ctrl+C, then create a viewer. It asks for a password of at least 12 characters:
   ```bash
   .venv/bin/python create_admin.py --username viewer_test --name "Viewer Test" --role viewer
   ```
   Start the server again, then in the second terminal, using the password you just chose:
   ```bash
   TOKEN=$(curl -s -X POST localhost:8765/api/auth/login -H "content-type: application/json" -d '{"username":"viewer_test","password":"THE_PASSWORD_YOU_CHOSE"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
   curl -s -w "\ntrade as viewer: %{http_code}\n" -X POST localhost:8765/api/trade/order -H "Authorization: Bearer $TOKEN" -H "content-type: application/json" -d '{"symbol":"XAUUSD","action":"BUY","volume":0.01}'
   curl -s -o /dev/null -w "read status as viewer: %{http_code}\n" localhost:8765/api/autopilot/status -H "Authorization: Bearer $TOKEN"
   ```
   Expect 403 with a message naming the roles, then 200.

**Negative controls.** Restore with `git checkout -- <file>` after each.

- In `app/core/security.py`, add `return current_user` as the first line inside `checker`. Run `.venv/bin/python -m pytest tests/test_access_control.py -q -k viewer_is_refused`. Expect 14 failures.
- In `app/core/security.py`, replace the two lines inside `_signing_key` with `return settings.SECRET_KEY or "x" * 40`. Run `.venv/bin/python -m pytest tests/test_access_control.py -q -k no_key_means_no_tokens`. Expect 1 failure.

## Steps 5 and 6. Sandbox isolation, logout, login limits, account state

**Status:** verified by you on 2026-09-22. Checks 1 to 6 passed on a live server: logout revoked both tokens,
disabling cut access immediately, and the lockout held against the correct password.
Negative controls were run by Claude before handover.

**What changed**
- AI-written code always runs in a separate process. The in-process mode is gone, not just unused.
- Logout revokes both tokens. The seven-day refresh token used to survive logout and could mint new logins. The frontend now calls logout.
- Every token has a unique ID. Rehearsing these checks showed two logins in the same second produced identical tokens, so revoking one revoked both, and a refresh right after login returned an already-revoked token.
- One revocation lookup per request instead of two, and forged tokens cost no database lookup at all.
- Role and active status are read from the database on every request. Demoting, disabling or deleting someone takes effect on their next request.
- Disabled accounts cannot log in. Disabling used to be accepted and silently ignored.
- Roles are validated, and the admin account cannot be demoted or disabled.
- Every account follows the same password rule.
- Logins are limited to 10 a minute per address, and 5 failures lock an account for 15 minutes. Unknown usernames take as long to reject as wrong passwords.

**Your checks** use the `viewer_test` account you made in step 4. Run the verify script, then start the server in one terminal:

```bash
cd ~/dev/Ai_quant_station/backend
.venv/bin/python -m uvicorn app.main:app --port 8765
```

Paste the check blocks from the step 6 message into a second terminal. If you ever see `Too many login attempts`, wait one minute. Restarting the server clears an account lockout.

**Negative controls**
- In `get_current_user`, return the role from the token instead of reading the database. Expect 3 failures from `-k "immediately or deleted_user"`.
- In `logout`, drop the refresh token from `candidates`. Expect 2 failures from `-k logout`.
- Make `_username_locked` return False. Expect 1 failure from `-k lock`.
- Remove `"jti"` from both token functions. Expect 3 failures from `-k "back_in or one_session or straight_after"`.

## Step 7. Cleanup

**Status:** verified by you on 2026-09-22. Checks 1 to 6 passed.

**What changed**
- 37 leftover files deleted, about 28,000 lines: the `_junk/` folder, two AI session transcripts totalling 390 KB, scratch scripts at the root and in `backend/`, error and output dumps, a loose SQL file already covered by a migration, and committed test artifacts. Test artifacts are now gitignored.
- Five dead modules deleted: the bar-by-bar backtest engine, market storage, and the memory service with its two models.
- `backfill_exit_prices.py` moved to `backend/scripts/`, and it now checks the connector address is local.
- README, HOW_TO_RUN, QUICK_START, MT5_CONNECTOR and PROJECT_NOTES rewritten to match the code. AGENTS.md corrected in about fifteen places.
- The browser tests read the admin password from `E2E_ADMIN_PASSWORD` instead of hardcoding the published one.
- Kept, as you did not say otherwise: both `backtest-expert` folders.

**Your checks**, from the repository root.

1. Five PASS lines, and the backend count is unchanged at 120 passed, 9 skipped.
   ```bash
   ./scripts/verify.sh
   ```

2. The two cleanup commits only delete, move, or touch `.gitignore`. Every line should start with `D`, `R` or `M .gitignore`:
   ```bash
   git show --name-status --format= e94f8d6 8fa3dfe
   ```

3. The published passwords appear only in the rejection list and the tests that check it:
   ```bash
   git grep -n "Usdt@2026\|admin@2026"
   ```
   Expect matches only in `backend/app/core/config.py`, `backend/tests/` and `docs/refactor/`.

4. The README's start command works. Start the backend, then in a second terminal check it:
   ```bash
   cd backend && .venv/bin/python run.py
   ```
   ```bash
   curl localhost:8002/health
   ```
   Expect `{"status":"healthy"}`. Stop the backend with Ctrl+C.

5. The moved backfill script refuses a public connector:
   ```bash
   cd backend && MT5_CONNECTOR_URL=http://8.8.8.8:5001 .venv/bin/python scripts/backfill_exit_prices.py
   ```
   Expect `ConnectorAddressBlocked`.

6. Read the new README and MT5_CONNECTOR.md. They should match what you have seen the system do.

## Step 8. One route to the broker

**Status:** approved by you on 2026-09-28. Checks 1 to 5 passed, including the database refusal, stamp and upgrade,
and slippage recorded on the Terminal trade (quoted 2668.01, filled 2668.03; exit quoted 2667.81, filled 2667.79).
In check 6 the placeholder password was used, so only `no login: 401` was shown on the live server; the viewer
refusal is covered by `test_only_traders_can_initialize_the_terminal` in check 1. Check 7, the real demo account,
is deferred.

**Commits:** `14d8cf6` migrations, `1b67794` one connector route, `52f924e` connector data fixes, `8c8198b` slippage recording

**What changed**
- Database migrations run. They never did: startup ran them from the wrong folder and ignored the error, and they could not build a fresh database anyway. Startup now creates, upgrades or adopts the database, then refuses to start if a table or column the code needs is missing.
- Every order, close, modify and market data request goes through one client, `backend/app/core/mt5_connector.py`. The backend no longer uses the Windows-only MetaTrader5 package, so manual trading on the Terminal page now works on Linux. Tests fail if a second route to the broker is added.
- The connector address and token are set once, in the server's `.env`. The per-user connector address and the Settings page's dead MT5 options are gone. Settings now has a Check Connection button.
- Market data needs a personal login. The shared connector token no longer reads it. Starting the terminal needs the trader or admin role.
- Price sync goes through the connector instead of crashing on Linux.
- `/mt5/health` reports the terminal as connected. It read a field the connector never sends.
- Positions report their stop loss, so the close and modify audits now record it too.
- Every connector time is broker server time, formatted the same way on any machine. Positions used to show the Windows machine's local time.
- The connector no longer lets a caller choose which terminal program to start.
- Trades record the quoted price next to the filled price: `requested_price`, and `requested_exit_price` for Terminal closes. Pending orders record none.
- Removed settings: `MT5_USE_EXTERNAL_CONNECTOR`, `MT5_SERVER_PORT` and the backend's `MT5_TERMINAL_PATH`. The connector's own `MT5_TERMINAL_PATH` stays.

**Your checks.** Run them from the repository root unless a check says otherwise.

1. Five PASS lines: backend 146 passed and 9 skipped, connector 59 passed, frontend 109 passed.
   ```bash
   ./scripts/verify.sh
   ```

2. No second route to the broker. Both should print nothing. The patterns match real imports and settings only, not the test that guards against them or comments explaining the history:
   ```bash
   git grep -nE "^\s*(import|from) MetaTrader5" backend/app
   git grep -n "MT5_USE_EXTERNAL_CONNECTOR\|MT5_SERVER_PORT" backend/app/core/config.py backend/.env.example frontend/src
   ```

3. Your local database. It was made before migrations worked, so it has no recorded version. Start the backend:
   ```bash
   cd backend && .venv/bin/python -m uvicorn app.main:app --port 8765
   ```
   Expect startup to fail with `This database was made by older code`, naming three missing columns: `autopilot_trades.requested_price`, `trade_records.requested_exit_price` and `trade_records.requested_price`. This is the guard working. Make a backup, then record the database's last migration and start again:
   ```bash
   cp finance_engine.db finance_engine.db.bak
   .venv/bin/alembic stamp e4a7b9c2d1f3
   .venv/bin/python -m uvicorn app.main:app --port 8765
   ```
   Expect `Upgrading database from e4a7b9c2d1f3 to f1c3a5e7b9d2` and then `Database schema upgraded`. Stop it with Ctrl+C, start it again, and expect `Database schema up to date`. Stop it again.

4. The fake connector, including the new stop loss, open time and fill checks. Start it in one terminal:
   ```bash
   mt5_connector/.venv/bin/python mt5_connector/testing/run_fake_connector.py --port 5001 --token mytoken
   ```
   In a second terminal:
   ```bash
   backend/.venv/bin/python scripts/demo_check.py --token mytoken --trade
   ```
   Expect `All stages passed`, including `position reports its stop loss`, `reported fill matches the OPEN deal` and `position open time matches the OPEN deal`.

5. Manual trading through the backend, and slippage in the database. Leave the fake connector running. In the second terminal, start the backend pointed at it:
   ```bash
   cd backend
   MT5_CONNECTOR_URL=http://127.0.0.1:5001 MT5_API_TOKEN=mytoken .venv/bin/python -m uvicorn app.main:app --port 8765
   ```
   In a third terminal:
   ```bash
   cd ~/dev/Ai_quant_station/backend
   B=localhost:8765; ADMIN_PW=$(grep ^DEFAULT_ADMIN_PASSWORD .env | cut -d= -f2-)
   TOKEN=$(curl -s -X POST $B/api/auth/login -H "content-type: application/json" -d "{\"username\":\"admin\",\"password\":\"$ADMIN_PW\"}" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
   curl -s -X POST $B/api/mt5/initialize -H "Authorization: Bearer $TOKEN" -o /dev/null -w "initialize: %{http_code}\n"
   T=$(curl -s -X POST $B/api/trade/order -H "Authorization: Bearer $TOKEN" -H "content-type: application/json" -d '{"symbol":"XAUUSD","action":"BUY","volume":0.01}' | python3 -c "import sys,json; print(json.load(sys.stdin)['ticket'])")
   curl -s -X POST $B/api/trade/close -H "Authorization: Bearer $TOKEN" -H "content-type: application/json" -d "{\"ticket\":$T}"; echo
   python3 -c "
   import sqlite3; r = sqlite3.connect('finance_engine.db').execute('select requested_price, entry_price, requested_exit_price, exit_price from trade_records where mt5_ticket=$T').fetchone()
   print('entry: quoted %s, filled %s | exit: quoted %s, filled %s' % r)"
   ```
   Expect `initialize: 200`, then a line where each quoted price differs from its filled price by a couple of cents. The fake terminal slips every fill by 2 points.

6. A viewer cannot start the terminal. Still in the third terminal, using your `viewer_test` password:
   ```bash
   VT=$(curl -s -X POST $B/api/auth/login -H "content-type: application/json" -d '{"username":"viewer_test","password":"THE_PASSWORD_YOU_CHOSE"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
   curl -s -o /dev/null -w "viewer initialize: %{http_code}\n" -X POST $B/api/mt5/initialize -H "Authorization: Bearer $VT"
   curl -s -o /dev/null -w "viewer positions: %{http_code}\n" $B/api/mt5/positions -H "Authorization: Bearer $VT"
   curl -s -o /dev/null -w "no login: %{http_code}\n" $B/api/mt5/positions
   ```
   Expect 403, 200, 401. Stop the backend and the fake connector.

7. Your real demo account. This includes the fill price check deferred from step 3. Follow `LOCAL_DEMO_SETUP.md`, then:
   ```bash
   backend/.venv/bin/python scripts/demo_check.py --trade
   ```
   Expect `All stages passed`. Note the times printed: they are OctaFX server time, not UTC. That is finding 22.

**Negative controls.** Claude ran these before handover. Each failed exactly where stated. Restore with `git checkout -- <file>` afterwards, and commit any work of your own first.

- In `mt5_connector/connector.py`, format `open_time` with `datetime.fromtimestamp(pos.time)` again: `test_times_do_not_depend_on_the_machine_time_zone` fails.
- Remove the `"sl"` line from `/positions`: 2 failures.
- Put back `terminal_path_input` on `/initialize`: `test_initialize_ignores_a_caller_supplied_terminal_path` fails.
- In `backend/app/services/trade_service.py`, remove `requested_price=` or `rec.requested_exit_price =`: `test_terminal_order_close_and_modify` fails. Recording a quote for pending orders fails `test_pending_order_records_no_requested_price`.
- In `backend/app/api/autopilot.py`, drop `requested_price` from `execute_trade`'s result: `test_autopilot_records_the_filled_price_not_the_quote` fails.
- In `backend/app/core/schema.py`, replace `command.upgrade(cfg, "head")` with `pass`: `test_database_migrations_behind_is_upgraded` fails.

## Step 9. Risk engine

**Status:** approved by you on 2026-09-29. All eight checks passed, including the Settings page card.
Checks 5 and 6 were rerun after a password reset and a fresh login: the first attempt's 401s were an expired
15-minute login, and the autopilot correctly sized 0.03 lots under the 0.3% limit still in force.

**Commits:** `eb109ad` connector limits, `ba3f39d` risk gate, `1cb0fed` Settings card, `505ddf1` docs

**What changed**
- Every new order passes one gate, `backend/app/core/risk.py`, before it reaches the broker: the Terminal page, the AI Analyst's Execute Trade button and the autopilot. A test fails if any code sends an order around it. Closing is never blocked. Changing a stop is refused only if it removes the stop.
- The gate refuses an order, naming the rule, when:
  - it has no stop loss
  - its stop or target is on the wrong side of the price, or closer than the broker allows
  - it would lose more than the per-trade limit, 2% of equity, if the stop is hit
  - the account is down 3% or more since the start of the UTC day
  - the margin level is below 200%
  - a pending order's price is more than 2% from the market
  - the open-trade limit is reached. This is off by default (0 = no limit), as you asked
- The autopilot sizes every order from equity and the stop: 1% of equity lost if the stop is hit. The AI's lot is ignored, and kept in the record. If even the smallest lot would risk too much, the order is refused, not rounded up.
- All the limits are settings you change on the Settings page or through `/api/risk/settings`. Only an admin can change them, and every change needs a reason. Changes are never overwritten: each one is a new version with who and why. Every order decision records the version it used, the numbers, and the context (prompt, market regime, AI's lot).
- The Settings page's new Risk Limits card shows today's standing, recent changes and recently refused orders.
- Connector: it refuses any order above `MT5_MAX_VOLUME` (default 1 lot), a second lock the website cannot change. It refuses volume above the broker's maximum, and refuses misplaced stops instead of moving them. `/symbol` now sends what sizing needs, including the minimum stop distance (finding 7).
- New findings 24 to 26.

**Your checks.** Terminal 1 runs the fake connector, terminal 2 the backend, terminal 3 the commands.

1. Five PASS lines: backend 197 passed and 9 skipped (about 15 minutes), connector 68 passed, frontend 109 passed.
   ```bash
   cd ~/dev/Ai_quant_station && ./scripts/verify.sh
   ```

2. Your database gets the three new tables. Terminal 1:
   ```bash
   cd ~/dev/Ai_quant_station
   mt5_connector/.venv/bin/python mt5_connector/testing/run_fake_connector.py --port 5001 --token mytoken
   ```
   Terminal 2:
   ```bash
   cd ~/dev/Ai_quant_station/backend
   MT5_CONNECTOR_URL=http://127.0.0.1:5001 MT5_API_TOKEN=mytoken .venv/bin/python -m uvicorn app.main:app --port 8002
   ```
   Expect `Upgrading database from f1c3a5e7b9d2 to a7d2c4e6f8b1`, then `Database schema upgraded`.

3. The gate. Terminal 3:
   ```bash
   cd ~/dev/Ai_quant_station/backend
   B=localhost:8002; J="content-type: application/json"; ADMIN_PW=$(grep ^DEFAULT_ADMIN_PASSWORD .env | cut -d= -f2-)
   H="Authorization: Bearer $(curl -s -X POST $B/api/auth/login -H "$J" -d "{\"username\":\"admin\",\"password\":\"$ADMIN_PW\"}" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")"
   curl -s -X POST $B/api/mt5/initialize -H "$H" -o /dev/null -w "initialize: %{http_code}\n"
   SL=$(curl -s $B/api/mt5/symbol/XAUUSD -H "$H" | python3 -c "import sys,json; print(round(json.load(sys.stdin)['ask'] - 10, 2))")
   echo "no stop:";   curl -s -X POST $B/api/trade/order -H "$H" -H "$J" -d '{"symbol":"XAUUSD","action":"BUY","volume":0.05}'; echo
   echo "10 lots:";   curl -s -X POST $B/api/trade/order -H "$H" -H "$J" -d "{\"symbol\":\"XAUUSD\",\"action\":\"BUY\",\"volume\":10,\"sl\":$SL}"; echo
   echo "0.05 lots:"; curl -s -X POST $B/api/trade/order -H "$H" -H "$J" -d "{\"symbol\":\"XAUUSD\",\"action\":\"BUY\",\"volume\":0.05,\"sl\":$SL}"; echo
   ```
   Expect a refusal naming the missing stop loss, a refusal saying 10 lots risks about 100% against a 2% limit, then a filled order with `"risk_pct"` about 0.5.
   A login lasts 15 minutes. If a later command prints `401`, run the `H=...` line again to log in afresh.

4. Change a limit, and the next order follows it. Still in terminal 3:
   ```bash
   curl -s -X PUT $B/api/risk/settings -H "$H" -H "$J" -d '{"reason":"my first test","max_trade_risk_pct":0.3,"autopilot_risk_pct":0.3}' -o /dev/null -w "change: %{http_code}\n"
   curl -s -X POST $B/api/trade/order -H "$H" -H "$J" -d "{\"symbol\":\"XAUUSD\",\"action\":\"BUY\",\"volume\":0.05,\"sl\":$SL}"; echo
   curl -s $B/api/risk/settings/history -H "$H" | python3 -c "import sys,json; [print('version', v['id'], 'cap', v['max_trade_risk_pct'], 'by', v['changed_by_name'], '-', v['reason']) for v in json.load(sys.stdin)['versions']]"
   curl -s "$B/api/risk/decisions?limit=5" -H "$H" | python3 -c "import sys,json; [print(d['outcome'], d['reason_code'], d['volume'], 'lots, version', d['settings_id']) for d in json.load(sys.stdin)['decisions']]"
   ```
   Expect `change: 200`, a refusal against the 0.3% limit, two versions with your reason, and the decisions: the newest refused under the new version, the older ones under version 1.

5. A viewer cannot change limits. Replace `YOUR_VIEWER_PASSWORD` with the real `viewer_test` password:
   ```bash
   VH="Authorization: Bearer $(curl -s -X POST $B/api/auth/login -H "$J" -d '{"username":"viewer_test","password":"YOUR_VIEWER_PASSWORD"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")"
   curl -s -o /dev/null -w "viewer reads: %{http_code}\n" $B/api/risk/settings -H "$VH"
   curl -s -o /dev/null -w "viewer changes: %{http_code}\n" -X PUT $B/api/risk/settings -H "$VH" -H "$J" -d '{"reason":"x","daily_loss_pct":50}'
   ```
   Expect 200, then 403. A Python `KeyError: 'access_token'` means the password was wrong.
   Forgotten the password? Set a new one. The server can keep running. From `backend/`, type the new password twice at the prompt, at least 12 characters:
   ```bash
   .venv/bin/python create_admin.py --username viewer_test --reset
   ```
   After 5 wrong attempts the account is locked for 15 minutes. Restarting the backend clears that.

6. The autopilot sizes its own orders. Put the limits back, then let it place one order with a stop 8 away while the AI "asks" for 0.50 lots:
   ```bash
   curl -s -X PUT $B/api/risk/settings -H "$H" -H "$J" -d '{"reason":"back to defaults","max_trade_risk_pct":2,"autopilot_risk_pct":1}' -o /dev/null -w "reset: %{http_code}\n"
   MT5_CONNECTOR_URL=http://127.0.0.1:5001 MT5_API_TOKEN=mytoken .venv/bin/python -c "
   import asyncio
   from app.api import autopilot
   from app.core.mt5_connector import connector_client
   async def main():
       q = await connector_client.get_symbol('XAUUSD'); a = await connector_client.get_account()
       r = await autopilot.execute_trade(1, 'XAUUSD', 'BUY', 0.50, sl=round(q['ask'] - 8, 2))
       print('equity', a['equity'], '| AI asked for 0.50 lots | sent', r.get('volume'), '|', r.get('error', 'ok'))
       if r.get('ticket'): await connector_client.close_position(r['ticket'])
   asyncio.run(main())" 2>&1 | tail -1
   ```
   Expect about `sent 0.12`: 1% of about 10,000 is 100, and a stop 8 away on 1 lot of gold costs 800, so 0.125, rounded down.

7. The connector's own cap, sent straight to it:
   ```bash
   curl -s -X POST localhost:5001/order -H "Authorization: Bearer mytoken" -H "$J" -d '{"symbol":"XAUUSD","action":"BUY","volume":1.01}'; echo
   ```
   Expect a refusal naming `MT5_MAX_VOLUME`.

8. The Settings page. Terminal 3:
   ```bash
   cd ~/dev/Ai_quant_station/frontend && npm run dev
   ```
   Open http://localhost:5173, log in as admin, and go to Settings. The Risk Limits card should show your limits, today's equity, the changes you made with their reasons, and the orders refused above. Change a value, save without a reason (refused), then with one (saved). Stop everything with Ctrl+C.

**Negative controls.** Claude ran these before handover. Each failed where stated.

- In `backend/app/services/trade_service.py`, send with `connector_client.place_order(payload)` instead of `submit_order(...)`: 3 failures in `test_risk_gate.py`, including `test_only_the_risk_gate_places_orders`.
- In `backend/app/api/risk.py`, make `update_settings` depend on `get_current_user`: `test_only_an_admin_changes_the_limits` fails.
- In `backend/app/core/risk.py`, make `evaluate` return `Evaluation(True, volume=order.get("volume"))` first: 22 of the 35 rule tests fail.
- Size with `budget / ev.stop_distance`, leaving out the value per lot: 4 sizing tests fail.
- Loosen the daily limit to `loss_pct > limit + 1`: `test_daily_loss_at_the_limit_refuses` fails.
- In `mt5_connector/connector.py`, switch off the volume cap: 1 failure. Switch off `check_stops`: 5 failures.

## Step 10. Autopilot fixes

**Status:** approved by you on 2026-10-02. All six checks passed. In check 6 the AI step failed because no AI key is set, as expected, and the loop carried on to the next cycle.

**Commits:** `b0fe844` connector clock and close reasons, `b6c0941` autopilot and UTC, `b58a64c` Autopilot page, and the docs commit after them

**What changed**
- The autopilot loop survives errors. A failure in one cycle is logged in full and the next cycle still runs. It used to end the loop silently while the page said running. If the loop ever does end unexpectedly, the page shows it stopped, with the reason.
- The autopilot's own daily loss brake pauses it for the rest of the UTC day, logs that once, and resumes by itself at 00:00 UTC. It used to switch the autopilot off for good. It can now be switched off on the Autopilot page. The account-wide 3% limit in the risk gate applies either way.
- The autopilot's daily trade count and profit are read from the database every cycle, profit by the day each trade closed. They used to be memory counters that mixed open and close days, and missed trades found by the start-up back-sync.
- Real UTC everywhere in the backend (finding 22). The connector client converts every broker time in one place: trade history, open trades, candles, and range queries. The price sync's separate conversion is gone, so nothing is converted twice. The offset is detected from the connector's new `/clock` while prices are live, so it follows summer time. `MT5_BROKER_UTC_OFFSET` is only the fallback. `/api/mt5/health` shows which is in use.
- The AI's code sees real candle times, not January 1970 (finding 9).
- Stop and target hits come from the broker's own deal reason, in one helper used by all seven places that used to guess from the comment (finding 10). The broker force-closing a trade for margin now shows as `STOP_OUT`.
- The unused open-trade check is deleted. The risk gate's open-trade setting covers it.
- Records made before this step keep their old times, as you chose.
- The History page and every API now show UTC times, a few hours earlier than the broker's own clock.

**Your checks.** T1 runs the fake broker, set 3 hours ahead of UTC like a real broker. T2 runs the backend, T3 the commands.

1. Five PASS lines: backend 225 passed and 9 skipped (about 16 minutes), connector 72, frontend 109.
   ```bash
   cd ~/dev/Ai_quant_station && ./scripts/verify.sh
   ```

2. The database update. T1:
   ```bash
   cd ~/dev/Ai_quant_station
   mt5_connector/.venv/bin/python mt5_connector/testing/run_fake_connector.py --port 5001 --token mytoken --server-offset 3
   ```
   Expect `server_clock=UTC+3` in its first line. T2:
   ```bash
   cd ~/dev/Ai_quant_station/backend
   MT5_CONNECTOR_URL=http://127.0.0.1:5001 MT5_API_TOKEN=mytoken .venv/bin/python -m uvicorn app.main:app --port 8002
   ```
   Expect `Upgrading database from a7d2c4e6f8b1 to c5e8f2a4d6b9`.

3. The broker's clock is detected. T3:
   ```bash
   cd ~/dev/Ai_quant_station/backend
   B=localhost:8002; J="content-type: application/json"; ADMIN_PW=$(grep ^DEFAULT_ADMIN_PASSWORD .env | cut -d= -f2-)
   H="Authorization: Bearer $(curl -s -X POST $B/api/auth/login -H "$J" -d "{\"username\":\"admin\",\"password\":\"$ADMIN_PW\"}" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")"
   curl -s -X POST $B/api/mt5/initialize -H "$H" -o /dev/null -w "initialize: %{http_code}\n"
   curl -s $B/api/mt5/health -H "$H"; echo
   sleep 35
   curl -s $B/api/mt5/health -H "$H"; echo
   ```
   Expect the first `broker_clock` to say `"source":"MT5_BROKER_UTC_OFFSET"` with offset 0. The first reading cannot tell yet, because prices must move between two readings. The second should say `"offset_hours":3.0,"source":"detected"`. T2's log shows a warning that `MT5_BROKER_UTC_OFFSET is +0` but the broker reads UTC+3.

4. Times arrive in UTC. Place a trade, then compare the broker's own time with what the backend reports:
   ```bash
   SL=$(curl -s $B/api/mt5/symbol/XAUUSD -H "$H" | python3 -c "import sys,json; print(round(json.load(sys.stdin)['ask'] - 10, 2))")
   T=$(curl -s -X POST $B/api/trade/order -H "$H" -H "$J" -d "{\"symbol\":\"XAUUSD\",\"action\":\"BUY\",\"volume\":0.01,\"sl\":$SL}" | python3 -c "import sys,json; print(json.load(sys.stdin)['ticket'])")
   echo "UTC now:         $(date -u '+%Y-%m-%d %H:%M:%S')"
   echo "broker's clock:  $(curl -s localhost:5001/positions -H 'Authorization: Bearer mytoken' | python3 -c "import sys,json; print(json.load(sys.stdin)['positions'][-1]['open_time'])")"
   echo "backend reports: $(curl -s $B/api/mt5/positions -H "$H" | python3 -c "import sys,json; print(json.load(sys.stdin)['positions'][-1]['open_time'])")"
   curl -s -X POST $B/api/trade/close -H "$H" -H "$J" -d "{\"ticket\":$T}" -o /dev/null
   curl -s "$B/api/mt5/history?hours=1" -H "$H" | python3 -c "import sys,json; [print('closing deal:  ', d['time'], 'reason', d['reason']) for d in json.load(sys.stdin)['deals'] if d['position_id']==$T and d['entry']=='CLOSE']"
   ```
   Expect the broker's clock 3 hours ahead of UTC now, and the backend's time and the closing deal matching UTC now. The reason reads `expert`, which is how the broker marks a close sent by software.

5. Candles arrive in UTC:
   ```bash
   curl -s -X POST $B/api/mt5/data/latest -H "$H" -H "$J" -d '{"symbol":"XAUUSD","timeframe":"15m","count":2}' | python3 -c "
   import sys, json, datetime
   t = json.load(sys.stdin)['data'][-1]['time']
   print('latest 15m candle opened at', datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%H:%M'), 'UTC')"
   date -u '+now it is %H:%M UTC'
   ```
   Expect the candle within the last 15 minutes, not 3 hours ahead.

6. The brake switch on the Autopilot page. T3, once the checks above are done:
   ```bash
   cd ~/dev/Ai_quant_station/frontend && npm run dev
   ```
   Open http://localhost:5173 as admin and go to Autopilot. Next to Max Daily Loss there is a tick box, "Autopilot daily loss brake ($)", with an explanation underneath. Untick it: the amount greys out. Start the autopilot and stop it again, which saves the settings, then reload the page. The box should stay unticked. Tick it again, start and stop, and reload once more.

**Negative controls.** Claude ran these before handover. Each failed where stated.

- In `backend/app/api/autopilot.py`, narrow the loop's `except Exception` to `except ZeroDivisionError`: `test_an_error_does_not_end_the_loop` fails.
- In `_daily_totals`, count profit by `executed_at` instead of `closed_at`: 2 failures.
- In `_loop_iteration`, set `state["running"] = False` when the brake is hit, as the old code did: `test_the_brake_pauses_and_resumes_the_next_day` fails.
- In `backend/app/core/mt5_connector.py`, stop converting deal times: 2 failures.
- In `backend/app/core/trade_outcome.py`, guess from `"sl" in comment` again: `test_a_comment_mentioning_sl_is_not_a_stop` fails.
- In `backend/app/core/broker_clock.py`, make `epoch_to_utc` return its input: 2 failures.
- In `backend/app/api/execute.py`, drop `unit='s'`: `test_sandbox_candles_are_in_this_century` fails.
- In `mt5_connector/connector.py`: trusting a price that has not moved, dropping the 5-minute tolerance, or dropping the deal reason each fail 1 test.

## Step 11. Deployment fixes

**Status:** built. Checks 1 to 6 still to do.

**Commits:** `3cb6788` password changes and proxy address, `1634776` Docker, nginx and pinned dependencies, and the docs commit after them

**What changed**
- Changing a password ends every other session (finding 19): your own change, an admin's reset, or `create_admin.py --reset`. Each token carries the password version it was issued with, and older versions are refused. Whoever changes their own password gets a new login back on the Settings page and stays logged in. A new `users.password_version` column, with its migration.
- Behind nginx each visitor has their own login limit (finding 20). The backend reads `X-Forwarded-For` only when the request comes from an address in `FORWARDED_ALLOW_IPS`. Sent from anywhere else, the header is ignored, so nobody can dodge the limit by writing a new address in it. A malformed list stops the server at startup.
- Every package version is exact and hashed (finding 13). You edit `requirements.in`; `requirements.txt` is generated from it with your current versions, so nothing was upgraded. `requirements-dev.txt` is generated the same way, at the same versions. torch comes as the CPU build. `backend/uv.toml` lets uv read both indexes, which is safe because of the hashes.
- Docker: one port (8000 inside), one worker, a normal user rather than root, `APP_ENV=production`, and a health check. In production, `/docs`, `/redoc` and `/openapi.json` return 404, and logs are JSON.
- `.dockerignore` only matched the repository root, so `backend/.env`, the local database and its backup went into the image (finding 30). Now none of them do.
- With the frontend built, `/health` and `/docs` were answered by the web page (finding 31). Fixed.
- `nginx.conf` had two `http` blocks and could not start. It is rewritten and switched on in compose. Only nginx has a port on your machine (8080). Postgres and the backend are reachable only inside Docker.
- Postgres refuses to start without a `POSTGRES_PASSWORD` you choose. The default `postgres` password is gone.
- The systemd file runs as an `impulse` account on 127.0.0.1, behind nginx.
- `render.yaml` is deleted. It was never used, and it generated a connector token that could never match.

**Your checks.** T1 runs the fake broker, T2 the local backend, T3 the commands.

1. Five PASS lines: backend 250 passed and 9 skipped (about 19 minutes), connector 72, frontend 109.
   ```bash
   cd ~/dev/Ai_quant_station && ./scripts/verify.sh
   ```

2. The local database update. T1 and T2 as in step 10:
   ```bash
   cd ~/dev/Ai_quant_station
   mt5_connector/.venv/bin/python mt5_connector/testing/run_fake_connector.py --port 5001 --token mytoken --server-offset 3
   ```
   ```bash
   cd ~/dev/Ai_quant_station/backend
   MT5_CONNECTOR_URL=http://127.0.0.1:5001 MT5_API_TOKEN=mytoken .venv/bin/python -m uvicorn app.main:app --port 8002
   ```
   Expect `Upgrading database from c5e8f2a4d6b9 to b3d6f9a2c8e1`.

3. A password change ends old sessions. T3 creates a throwaway account, changes its password, and deletes it again:
   ```bash
   cd ~/dev/Ai_quant_station/backend
   B=localhost:8002; J="content-type: application/json"; ADMIN_PW=$(grep ^DEFAULT_ADMIN_PASSWORD .env | cut -d= -f2-)
   tok() { python3 -c "import sys,json; print(json.load(sys.stdin)['$1'])"; }
   H="Authorization: Bearer $(curl -s -X POST $B/api/auth/login -H "$J" -d "{\"username\":\"admin\",\"password\":\"$ADMIN_PW\"}" | tok access_token)"
   ID=$(curl -s -X POST $B/api/auth/users -H "$H" -H "$J" -d '{"username":"step11","name":"Step 11","password":"first-password-2026","role":"viewer"}' | tok id)
   P=$(curl -s -X POST $B/api/auth/login -H "$J" -d '{"username":"step11","password":"first-password-2026"}')
   OLD_A=$(echo "$P" | tok access_token); OLD_R=$(echo "$P" | tok refresh_token)
   N=$(curl -s -X PUT $B/api/auth/password -H "Authorization: Bearer $OLD_A" -H "$J" -d '{"current_password":"first-password-2026","new_password":"second-password-2026"}')
   echo "old access:  $(curl -s -o /dev/null -w '%{http_code}' $B/api/auth/me -H "Authorization: Bearer $OLD_A")"
   echo "old refresh: $(curl -s -o /dev/null -w '%{http_code}' -X POST $B/api/auth/refresh -H "$J" -d "{\"refresh_token\":\"$OLD_R\"}")"
   echo "new access:  $(curl -s -o /dev/null -w '%{http_code}' $B/api/auth/me -H "Authorization: Bearer $(echo "$N" | tok access_token)")"
   curl -s -X DELETE $B/api/auth/users/$ID -H "$H" -o /dev/null -w "deleted: %{http_code}\n"
   curl -s -o /dev/null -w "local /docs: %{http_code}\n" $B/docs
   ```
   Expect old access 401, old refresh 401, new access 200, deleted 200, and local `/docs` 200 (development still shows it).

4. Docker from scratch. Stop T2 first (Ctrl+C); T1 can keep running. In T3, from the repository root:
   ```bash
   cd ~/dev/Ai_quant_station
   echo "POSTGRES_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_hex(16))')" > .env
   docker compose up -d --build
   docker compose ps --format '{{.Name}}  {{.Status}}  {{.Ports}}'
   for p in /health /docs /openapi.json /; do echo "$p $(curl -s -o /dev/null -w '%{http_code} %{content_type}' localhost:8080$p)"; done
   docker compose exec -T postgres psql -U impulse -d impulse_analyst -tc "select version_num from alembic_version"
   docker compose logs backend | grep -E "Admin account|startup complete"
   ```
   The first build downloads about 1 GB and takes several minutes. Expect three containers, `healthy`, with a host port (`0.0.0.0:8080`) only on `impulse_nginx`. Then `/health 200 application/json`, `/docs 404`, `/openapi.json 404`, `/ 200 text/html`, the version `b3d6f9a2c8e1`, and JSON log lines for the admin account and startup. Then open http://localhost:8080 and log in as admin with your usual password. This is a new, empty Postgres database, so the Dashboard's MT5 figures may show an error: the container cannot use your local connector token unless it is the same as in `backend/.env`.

5. Each visitor has their own login limit, and nobody can fake one:
   ```bash
   docker compose restart backend && until [ "$(docker inspect -f '{{.State.Health.Status}}' impulse_backend)" = healthy ]; do sleep 3; done
   NET=$(docker network ls --format '{{.Name}}' | grep impulse_network)
   visit() { docker run --rm --network $NET --ip $1 --entrypoint sh ai_quant_station-backend -c "for i in \$(seq $2); do curl -s -o /dev/null -w '%{http_code} ' http://nginx/api/auth/login -H 'content-type: application/json' -d \"{\\\"username\\\":\\\"nobody\$i-$1\\\",\\\"password\\\":\\\"wrong-password-1\\\"}\"; done"; echo; }
   echo -n "visitor A, 11 tries: "; visit 172.30.57.101 11
   echo -n "visitor B, 1 try:    "; visit 172.30.57.102 1
   echo -n "you, 11 made-up addresses: "; for i in $(seq 11); do curl -s -o /dev/null -w '%{http_code} ' localhost:8080/api/auth/login -H "X-Forwarded-For: 203.0.113.$i" -H 'content-type: application/json' -d "{\"username\":\"fake$i\",\"password\":\"wrong-password-1\"}"; done; echo
   ```
   Expect visitor A: ten 401s, then 429. Visitor B: 401, so A's limit did not block B. You: ten 401s, then 429, because the made-up `X-Forwarded-For` addresses were ignored.

6. Refusals, and what is inside the image:
   ```bash
   POSTGRES_PASSWORD= docker compose config > /dev/null
   docker run --rm --entrypoint sh ai_quant_station-backend -c 'whoami; find / \( -name .env -o -name "*.db" -o -name "*.bak" -o -name .venv \) -not -path "/proc/*" 2>/dev/null; echo end'
   docker compose down -v
   ```
   Expect `required variable POSTGRES_PASSWORD is missing a value`, then `app` and `end` with nothing between them. `down -v` removes the containers and the test database. The root `.env` stays; delete it if you like.

**Negative controls.** Claude ran these before handover. Each failed where stated.

- In `get_current_user`, delete the password version check: 2 failures (old access tokens work again).
- In `set_password`, stop raising `password_version`: 3 failures.
- In `client_ip`, believe the first `X-Forwarded-For` entry from anyone: 4 failures, including the login limit test.
- In `requirements.txt`, change one `==` to `>=`: the pin test fails. Remove one package's hashes: the hash test fails.
- In Docker, the two-container visitor test and the made-up address test were run against the real stack, with the results in check 5.
