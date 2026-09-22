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

**Status:** built, waiting for your verification

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
