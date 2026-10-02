"""
Step 12: errors are never dropped silently.

Every handler that catches Exception (or everything) must log, re-raise, or say
on its `except` line why dropping the error is right, with a
`# swallow-ok: <reason>` comment. Narrow handlers (ValueError and the like) are
free to fall back quietly: they catch only what they expect.

Code in app/ logs instead of printing, so production's JSON logs see it.

Negative controls:
- In app/core/mt5_sync.py, replace `failed.append(symbol)` and the
  `logger.exception` above it with `pass`. The handler test and the price sync
  tests must fail.
- Delete one `# swallow-ok:` comment. The handler test must fail and name the line.
"""
import ast
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"

_LOG_METHODS = {"exception", "error", "warning", "info", "debug", "critical"}
# The sandbox process has no logging set up; its stderr is logged by the parent
# (execute.run_python_code), so a printed traceback there is reported.
_SANDBOX_FUNCTIONS = {"_execute_sandbox_sync"}


def _is_broad(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(n, ast.Name) and n.id in ("Exception", "BaseException") for n in names)


def _reports(handler: ast.ExceptHandler, in_sandbox: bool) -> bool:
    for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in _LOG_METHODS:
                return True
            if isinstance(f, ast.Name) and f.id == "add_log":  # the autopilot log, which also logs
                return True
            if in_sandbox and isinstance(f, ast.Attribute) and f.attr == "print_exc":
                return True
    return False


def _handlers(tree):
    """Yield (handler, enclosing function name)."""
    def visit(node, func):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
            if isinstance(child, ast.ExceptHandler):
                yield child, func
            yield from visit(child, name)
    yield from visit(tree, None)


def _silent_handlers():
    found = []
    for path in sorted(APP_DIR.rglob("*.py")):
        source = path.read_text()
        lines = source.splitlines()
        for handler, func in _handlers(ast.parse(source)):
            if not _is_broad(handler):
                continue
            comment = lines[handler.lineno - 1].partition("# swallow-ok:")[2].strip()
            if len(comment) >= 10:
                continue
            if _reports(handler, func in _SANDBOX_FUNCTIONS):
                continue
            found.append(f"{path.relative_to(APP_DIR.parent)}:{handler.lineno}")
    return found


def test_no_broad_handler_drops_an_error_silently():
    silent = _silent_handlers()
    assert not silent, (
        "These handlers catch every error and neither log nor re-raise. Log it, or add "
        "`# swallow-ok: <reason>` on the except line:\n  " + "\n  ".join(silent)
    )


def test_app_code_logs_instead_of_printing():
    prints = []
    for path in sorted(APP_DIR.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
                prints.append(f"{path.relative_to(APP_DIR.parent)}:{node.lineno}")
    assert not prints, "Use a logger, not print():\n  " + "\n  ".join(prints)


# ── Price sync (finding 4) ──────────────────────────────────────────────────
import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from app.core import mt5_service, mt5_sync  # noqa: E402


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    """Two symbols, an empty archive, a connector that answers, and a record of every fetch."""
    fetches = []
    monkeypatch.setattr(mt5_sync, "LOCAL_CACHE", tmp_path)
    monkeypatch.setattr(mt5_sync, "AVAILABLE_SYMBOLS", ["XAUUSD", "EURUSD"])
    monkeypatch.setattr(mt5_sync, "_upload_parquet_to_hf", lambda: None)

    async def connected():
        return True
    monkeypatch.setattr(mt5_service, "init_mt5_connection", connected)

    async def fetch(symbol, timeframe, start, end):
        fetches.append((symbol, start))
        if symbol == "EURUSD" and getattr(fetch, "fail_eurusd", False):
            raise RuntimeError("connector said no")
        t = int(end.timestamp()) // 60 * 60
        return [{"time": t, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "tick_volume": 10}]
    monkeypatch.setattr(mt5_sync, "fetch_ohlc_range", fetch)
    return tmp_path, fetches, fetch


async def test_a_failed_symbol_makes_the_run_fail_but_the_others_still_sync(sync_env):
    folder, fetches, fetch = sync_env
    fetch.fail_eurusd = True
    with pytest.raises(mt5_sync.PriceSyncFailed, match="EURUSD"):
        await mt5_sync.sync_mt5_to_parquet()
    year = datetime.now(timezone.utc).year
    assert (folder / f"XAUUSD_{year}.parquet").exists(), "one symbol's failure stopped the others"


async def test_a_clean_run_raises_nothing(sync_env):
    await mt5_sync.sync_mt5_to_parquet()


async def test_a_new_year_starts_at_january_first_not_1970(sync_env):
    _, fetches, _ = sync_env
    await mt5_sync.sync_mt5_to_parquet()
    year = datetime.now(timezone.utc).year
    assert fetches and all(start == datetime(year, 1, 1, tzinfo=timezone.utc) for _, start in fetches), fetches


async def test_an_unreadable_file_fails_without_refetching_everything(sync_env):
    folder, fetches, _ = sync_env
    year = datetime.now(timezone.utc).year
    (folder / f"XAUUSD_{year}.parquet").write_bytes(b"not a parquet file")
    with pytest.raises(mt5_sync.PriceSyncFailed, match="XAUUSD"):
        await mt5_sync.sync_mt5_to_parquet()
    assert [s for s, _ in fetches] == ["EURUSD"], "the unreadable symbol was fetched from scratch"


async def test_an_existing_file_continues_from_its_last_candle(sync_env):
    folder, fetches, _ = sync_env
    year = datetime.now(timezone.utc).year
    last = int(datetime(year, 3, 1, tzinfo=timezone.utc).timestamp())
    pd.DataFrame([{"timestamp": last, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]).to_parquet(
        folder / f"XAUUSD_{year}.parquet", index=False)
    await mt5_sync.sync_mt5_to_parquet()
    assert dict(fetches)["XAUUSD"] == datetime.fromtimestamp(last + 60, tz=timezone.utc)


# ── The autopilot names why the AI step failed (finding 28) ────────────────
from app.api import autopilot  # noqa: E402
from app.core import providers  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.mt5_connector import connector_client  # noqa: E402
from app.models.ai_memory import AutopilotSettings  # noqa: E402
from tests.fake_connector import free_port, start_fake, stop_fake  # noqa: E402


@pytest.fixture(scope="module")
def fake_broker():
    proc, url = start_fake(free_port())
    yield url
    stop_fake(proc)


async def test_with_no_ai_key_the_log_says_so(fake_broker, monkeypatch, db_session):
    monkeypatch.setattr(settings, "MT5_CONNECTOR_URL", fake_broker)
    monkeypatch.setattr(settings, "MT5_API_TOKEN", "")
    await connector_client.initialize()

    async def no_key(*args, **kwargs):
        return None
    monkeypatch.setattr(providers, "resolve_api_key", no_key)

    db_session.add(AutopilotSettings(user_id=1, symbol="XAUUSD", provider="nvidia",
                                     model=providers.PROVIDERS["nvidia"]["models"][0]))
    await db_session.commit()
    autopilot._user_states.pop(1, None)

    await autopilot.run_autopilot_cycle(1)
    messages = [entry["message"] for entry in autopilot._get_state(1)["logs"]]
    failure = [m for m in messages if m.startswith("AI code generation failed after retries")]
    assert failure, messages[-5:]
    assert "no AI key is set" in failure[0], failure[0]


# ── Reports say when the broker's history is missing ───────────────────────
async def _history_fails(*args, **kwargs):
    raise RuntimeError("connector unreachable")


async def test_reports_flag_missing_history_instead_of_showing_nothing(fake_broker, monkeypatch, client, viewer_headers):
    monkeypatch.setattr(settings, "MT5_CONNECTOR_URL", fake_broker)
    monkeypatch.setattr(settings, "MT5_API_TOKEN", "")
    await connector_client.initialize()
    monkeypatch.setattr(connector_client, "get_history", _history_fails)
    resp = await client.get("/api/analytics/reports", headers=viewer_headers)
    assert resp.status_code == 200
    assert "connector unreachable" in (resp.json()["mt5_error"] or "")

    export = await client.get("/api/analytics/reports/export", headers=viewer_headers)
    assert "connector unreachable" in (export.json()["mt5_error"] or "")


async def test_reports_without_a_problem_carry_no_error(fake_broker, monkeypatch, client, viewer_headers):
    monkeypatch.setattr(settings, "MT5_CONNECTOR_URL", fake_broker)
    monkeypatch.setattr(settings, "MT5_API_TOKEN", "")
    await connector_client.initialize()
    resp = await client.get("/api/analytics/reports", headers=viewer_headers)
    assert resp.status_code == 200 and resp.json()["mt5_error"] is None


# ── A refresh that cannot revoke the old token is refused ──────────────────
async def test_refresh_fails_safely_when_the_old_token_cannot_be_revoked(client, monkeypatch):
    from app.api import auth as auth_api
    from tests.credentials import TEST_PASSWORD
    pair = (await client.post("/api/auth/login", json={"username": "test_trader", "password": TEST_PASSWORD})).json()

    async def broken(*args, **kwargs):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(auth_api, "blacklist_token", broken)

    resp = await client.post("/api/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert resp.status_code == 503, "a new pair was issued while the old refresh token stayed valid"
