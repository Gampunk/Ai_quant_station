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
