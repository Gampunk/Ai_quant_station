# Handoff: where the refactor stands

Read this first when resuming in a new environment (for example a cloud session).
Written 2026-10-02. Progress per step is in `PROGRESS.md`, problems in `FINDINGS.md`,
the colleague's branch in `UPSTREAM_COMPARISON.md`.

## How the user wants the work run

- Plan first. No edits until the user approves the plan for a step.
- Claude builds; the user runs every check themselves. Each step ends with checks,
  plus negative controls: break the guard on purpose and show its test fails.
- Run negative controls by copying the file to a scratch folder and restoring from
  that copy. Never `git checkout` a file to restore it: that once wiped uncommitted work.
- Don't edit code while `scripts/verify.sh` runs; pytest has already loaded the modules.
- One reviewable commit per part of a step, docs in a separate commit.
- Plain, short explanations. Security and robustness before any data or ML work.
- The live system (colleague's servers, Windows connector VPS, Linux backend) is
  never touched. No pushes to `upstream` (Gautam-813). Pushes go only to `origin`
  (Gampunk). Test orders use a demo account or the fake connector only.
- Never upgrade the user's local Docker; adapt files to the installed version.
- Alerts and reports use Telegram (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`), the
  colleague's chosen channel, so step 14 merges cleanly.
- Every risk limit stays user-editable and versioned. No default cap on open trades.

## State

- Steps 1 to 10: approved by the user.
- Steps 11, 12 and 13: built and committed. The user runs their checks together;
  the instructions are in PROGRESS.md. Not approved until those pass.
- Next: step 14, merging the best of both branches and replacing the live system.
  Plan it from `UPSTREAM_COMPARISON.md`; nothing is decided yet beyond what that
  file and the rules above say.

## Step 13 as approved (now built)

1. Faster backend tests (finding 18). Each test spends about 3 s in setup, mostly
   rebuilding tables and bcrypt-hashing three passwords. Hash once per session and
   empty tables instead of rebuilding. Report the real new duration.
2. GitHub Actions running the same checks as `scripts/verify.sh` on every push to
   the fork. A deliberately broken test must fail the run.
3. Kill switch. "Stop all trading" on the Risk Limits card: the risk gate refuses
   every new order, every autopilot stops and none restart at boot, a banner on every
   page. Closing positions and moving stops stay allowed. A separate "Close all
   positions" button with confirmation. Anyone who can trade switches it on; only an
   admin switches it off, with a reason. Every change recorded with who and why.
4. One order checked at a time (finding 24): a lock around check and send in
   `core/risk.py` `submit_order`, with the kill switch checked inside it.
5. Starting equity recorded at 00:00 UTC by a scheduled job (finding 25), falling
   back to the first check of the day; the card shows which was used.
6. Heartbeat alerts over Telegram, checked every minute: each running autopilot
   cycled within twice its interval, the connector and terminal answer, prices are
   live during market hours. One alert when a problem starts, one when it ends, also
   listed on the Risk Limits card. No outside "I'm alive" ping now: step 14 adopts
   and fixes the colleague's external `monitor.py`.

## Setting up a fresh environment

```bash
cd backend
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt   # run from backend/, reads uv.toml
cp .env.example .env    # set SECRET_KEY (32+ chars) and DEFAULT_ADMIN_PASSWORD (12+)
cd ../mt5_connector && uv venv --python 3.14 .venv && uv pip install --python .venv/bin/python -r requirements-test.txt
# (LOCAL_DEMO_SETUP.md covers the real MT5 terminal on Windows; not needed for tests)
cd ../frontend && npm install
./scripts/verify.sh     # from the repository root
```

Not in the repository, so missing in a fresh environment: `backend/.env`, the local
database, and `data_archive/` (the parquet archive; tests that need it are skipped).
