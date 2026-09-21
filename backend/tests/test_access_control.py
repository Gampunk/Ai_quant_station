"""
Signing key, default accounts, permissions, per-user API keys.

Negative controls:
- Make require_role's checker return current_user as its first line in
  app/core/security.py. Every "viewer is refused" test must fail.
- In _signing_key(), replace the body with `return settings.SECRET_KEY or "x" * 40`.
  The "no key, no tokens" test must fail.
"""
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.core.config import admin_password_problem, secret_key_problem, settings
from app.core.database import AsyncSessionLocal
from app.core.providers import resolve_api_key
from app.core.security import create_access_token, decode_token
from app.models.user import User

BACKEND_DIR = Path(__file__).resolve().parents[1]
GOOD_KEY = "a" * 64


# ── Signing key ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("key, bad", [
    ("", True),
    ("short", True),
    ("x" * 31, True),
    ("change-this-to-a-long-random-string-in-production", True),
    ("x" * 32, False),
    (GOOD_KEY, False),
])
def test_signing_key_rules(key, bad):
    assert (secret_key_problem(key) is not None) is bad


def test_no_key_means_no_tokens(monkeypatch):
    """The original bug: with no key, a fresh random one was used on every call."""
    monkeypatch.setattr(settings, "SECRET_KEY", "")
    with pytest.raises(ValueError, match="SECRET_KEY is not set"):
        create_access_token({"sub": "someone"})


async def test_tokens_survive_a_round_trip(monkeypatch):
    monkeypatch.setattr(settings, "SECRET_KEY", GOOD_KEY)
    token = create_access_token({"sub": "someone", "user_id": 7, "role": "trader"})
    payload = await decode_token(token)
    assert payload["sub"] == "someone" and payload["user_id"] == 7


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("key", ["", "too-short"])
def test_server_refuses_to_start_without_a_strong_key(key, tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("SECRET_KEY", "MT5_CONNECTOR_URL")}
    env.update(SECRET_KEY=key, DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/s.db")
    proc = subprocess.run(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(_free_port())],
        cwd=BACKEND_DIR, env=env, capture_output=True, text=True, timeout=90,
    )
    assert proc.returncode != 0
    assert "SECRET_KEY" in proc.stderr and "Application startup failed" in proc.stderr


# ── Default accounts ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("password, bad", [
    ("", True), ("admin@2026", True), ("Usdt@2026", True), ("short-pass", True),
    ("a-long-enough-local-password", False),
])
def test_admin_password_rules(password, bad):
    assert (admin_password_problem(password) is not None) is bad


async def _usernames():
    async with AsyncSessionLocal() as db:
        return sorted(u.username for u in (await db.execute(select(User))).scalars())


async def _wipe_users():
    async with AsyncSessionLocal() as db:
        await db.execute(delete(User))
        await db.commit()


@pytest.mark.parametrize("password", ["", "Usdt@2026", "admin@2026", "short"])
async def test_no_account_is_created_without_a_strong_admin_password(password, monkeypatch):
    from app.main import create_default_users
    await _wipe_users()
    monkeypatch.setattr(settings, "DEFAULT_ADMIN_PASSWORD", password)
    await create_default_users()
    assert await _usernames() == []


async def test_only_the_admin_account_is_created(monkeypatch):
    from app.main import create_default_users
    await _wipe_users()
    monkeypatch.setattr(settings, "DEFAULT_ADMIN_PASSWORD", "a-long-enough-local-password")
    await create_default_users()
    assert await _usernames() == ["admin"]


def test_no_passwords_are_written_in_the_startup_code():
    source = (BACKEND_DIR / "app" / "main.py").read_text()
    for published in ("Usdt@2026", "admin@2026", "keval_viradiya", "sagar_barot", "meet_rao"):
        assert published not in source


# ── Permissions ──────────────────────────────────────────────────────────────
TRADING_ACTIONS = [
    ("post", "/api/trade/order", {"json": {"symbol": "XAUUSD", "action": "BUY", "volume": 0.01}}),
    ("post", "/api/trade/close", {"json": {"ticket": 1}}),
    ("post", "/api/trade/modify", {"json": {"ticket": 1, "sl": 1.0}}),
    ("post", "/api/autopilot/start", {}),
    ("post", "/api/autopilot/stop", {}),
    ("post", "/api/autopilot/settings", {"json": {"symbol": "XAUUSD", "provider": "nvidia", "model": "m"}}),
    ("post", "/api/autopilot/connect-mt5", {"params": {"connector_url": "http://127.0.0.1:1"}}),
    ("post", "/api/autopilot/prompts", {"json": {"content": "x"}}),
    ("put", "/api/autopilot/prompts/custom_1", {"json": {"content": "x"}}),
    ("delete", "/api/autopilot/prompts/custom_1", {}),
    ("post", "/api/ai/chat", {"json": {"messages": [{"role": "user", "content": "hi"}], "provider": "nvidia", "model": "m"}}),
    ("post", "/api/backtest/run", {"json": {"prompt_id": "1", "symbol": "XAUUSD"}}),
    ("post", "/api/historical-lab/run", {"json": {"mode": "analysis", "symbol": "XAUUSD", "start_date": "2024-01-01", "end_date": "2024-01-02"}}),
    ("post", "/api/historical-lab/chat", {"json": {"backtest_id": 1, "message": "hi"}}),
]


@pytest.mark.parametrize("method, path, kwargs", TRADING_ACTIONS, ids=[f"{m} {p}" for m, p, _ in TRADING_ACTIONS])
async def test_viewer_is_refused(client: AsyncClient, viewer_headers, method, path, kwargs):
    resp = await getattr(client, method)(path, headers=viewer_headers, **kwargs)
    assert resp.status_code == 403, f"viewer got {resp.status_code} on {method.upper()} {path}"


@pytest.mark.parametrize("method, path, kwargs", TRADING_ACTIONS, ids=[f"{m} {p}" for m, p, _ in TRADING_ACTIONS])
async def test_anonymous_is_refused(client: AsyncClient, method, path, kwargs):
    resp = await getattr(client, method)(path, **kwargs)
    assert resp.status_code in (401, 403)


# A subset with harmless side effects. The request may still fail for other
# reasons, such as no MT5 on Linux or no AI key, but never on permissions.
TRADER_SAFE = [TRADING_ACTIONS[0], TRADING_ACTIONS[5], TRADING_ACTIONS[7], TRADING_ACTIONS[10]]


@pytest.mark.parametrize("method, path, kwargs", TRADER_SAFE, ids=[f"{m} {p}" for m, p, _ in TRADER_SAFE])
async def test_trader_passes_the_permission_check(client: AsyncClient, trader_headers, method, path, kwargs):
    resp = await getattr(client, method)(path, headers=trader_headers, **kwargs)
    assert resp.status_code not in (401, 403), f"trader refused on {method.upper()} {path}: {resp.text[:200]}"


async def test_viewer_can_still_read(client: AsyncClient, viewer_headers):
    for path in ("/api/auth/me", "/api/autopilot/status", "/api/autopilot/prompts", "/api/autopilot/results"):
        resp = await client.get(path, headers=viewer_headers)
        assert resp.status_code == 200, f"viewer could not read {path}: {resp.status_code}"


async def test_shared_connector_token_no_longer_places_trades(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "MT5_API_TOKEN", "the-shared-token")
    resp = await client.post(
        "/api/trade/order", headers={"x-mt5-token": "the-shared-token"},
        json={"symbol": "XAUUSD", "action": "BUY", "volume": 0.01},
    )
    assert resp.status_code in (401, 403)


async def test_run_any_code_endpoint_is_gone(client: AsyncClient, auth_headers):
    resp = await client.post("/api/execute/code", headers=auth_headers, json={"code": "print(1)"})
    assert resp.status_code in (404, 405)


async def test_indicator_endpoint_still_works_for_viewers(client: AsyncClient, viewer_headers):
    candles = [{"open": 1, "high": 2, "low": 0.5, "close": 1 + i * 0.01} for i in range(40)]
    resp = await client.post("/api/execute/calculate-indicator", headers=viewer_headers,
                             json={"indicator": "SMA", "period": 5, "market_data": candles})
    assert resp.status_code == 200 and resp.json()["success"] is True


# ── Per-user API keys ────────────────────────────────────────────────────────
async def test_user_api_key_round_trip(client: AsyncClient, trader_headers, db_session):
    saved = await client.post("/api/ai/user-keys", headers=trader_headers,
                              json={"groq": "gsk_personal_key", "not_a_provider": "ignored"})
    assert saved.status_code == 200, saved.text

    listed = await client.get("/api/ai/user-keys", headers=trader_headers)
    assert listed.status_code == 200
    assert listed.json()["providers"] == {"groq": True}

    trader_id = (await db_session.execute(select(User.id).where(User.username == "test_trader"))).scalar_one()
    assert await resolve_api_key("groq", settings, trader_id, AsyncSessionLocal) == "gsk_personal_key"


async def test_rotated_key_falls_back_to_the_server_key(client: AsyncClient, trader_headers, db_session, monkeypatch):
    assert (await client.post("/api/ai/user-keys", headers=trader_headers, json={"groq": "gsk_personal_key"})).status_code == 200
    trader_id = (await db_session.execute(select(User.id).where(User.username == "test_trader"))).scalar_one()

    monkeypatch.setattr(settings, "GROQ_API_KEY", "server-groq-key")
    monkeypatch.setattr(settings, "SECRET_KEY", "b" * 64)  # stored key can no longer be decrypted
    assert await resolve_api_key("groq", settings, trader_id, AsyncSessionLocal) == "server-groq-key"
