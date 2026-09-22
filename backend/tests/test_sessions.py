"""
Logout, token checks, login throttling, account state, and sandbox isolation.

Negative controls:
- In get_current_user (app/core/security.py), return the role from the token
  instead of the database: `return {"username": payload["sub"], "id": user_id,
  "role": payload.get("role")}` right after the payload checks. The "takes effect
  immediately" tests must fail.
- In logout (app/api/auth.py), skip the refresh token by removing its line from
  `candidates`. The refresh-token logout tests must fail.
"""
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.api import auth as auth_api
from app.core import blacklist
from app.models.user import User
from tests.credentials import TEST_PASSWORD


async def _login_pair(client, username, password=TEST_PASSWORD):
    resp = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"], resp.json()["refresh_token"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def _user_id(db_session, username):
    return (await db_session.execute(select(User.id).where(User.username == username))).scalar_one()


# ── Logout ───────────────────────────────────────────────────────────────────
async def test_logout_revokes_both_tokens(client: AsyncClient):
    access, refresh = await _login_pair(client, "test_trader")
    assert (await client.get("/api/auth/me", headers=_bearer(access))).status_code == 200

    out = await client.post("/api/auth/logout", headers=_bearer(access), json={"refresh_token": refresh})
    assert out.status_code == 200 and out.json()["revoked"] == 2

    assert (await client.get("/api/auth/me", headers=_bearer(access))).status_code == 401
    again = await client.post("/api/auth/refresh", json={"refresh_token": refresh})
    assert again.status_code == 401, "refresh token still works after logout"


async def test_logout_works_with_only_the_refresh_token(client: AsyncClient):
    """An expired access token must not stop you from revoking the refresh token."""
    _, refresh = await _login_pair(client, "test_trader")
    out = await client.post("/api/auth/logout", json={"refresh_token": refresh})
    assert out.status_code == 200 and out.json()["revoked"] == 1
    assert (await client.post("/api/auth/refresh", json={"refresh_token": refresh})).status_code == 401


async def test_logout_with_nothing_valid_still_succeeds(client: AsyncClient):
    out = await client.post("/api/auth/logout", headers=_bearer("garbage"), json={"refresh_token": "junk"})
    assert out.status_code == 200 and out.json()["revoked"] == 0


async def test_logout_ignores_a_token_of_the_wrong_type(client: AsyncClient):
    access, refresh = await _login_pair(client, "test_trader")
    out = await client.post("/api/auth/logout", json={"refresh_token": access})
    assert out.json()["revoked"] == 0
    assert (await client.get("/api/auth/me", headers=_bearer(access))).status_code == 200


async def test_logging_back_in_immediately_gives_a_working_token(client: AsyncClient):
    """Found by rehearsing the manual checks: identical tokens within one second."""
    access, refresh = await _login_pair(client, "test_trader")
    await client.post("/api/auth/logout", headers=_bearer(access), json={"refresh_token": refresh})
    fresh, _ = await _login_pair(client, "test_trader")
    assert fresh != access
    assert (await client.get("/api/auth/me", headers=_bearer(fresh))).status_code == 200


async def test_logging_out_one_session_leaves_another(client: AsyncClient):
    a_access, a_refresh = await _login_pair(client, "test_trader")
    b_access, _ = await _login_pair(client, "test_trader")
    await client.post("/api/auth/logout", headers=_bearer(a_access), json={"refresh_token": a_refresh})
    assert (await client.get("/api/auth/me", headers=_bearer(b_access))).status_code == 200


async def test_refresh_straight_after_login_works(client: AsyncClient):
    _, refresh = await _login_pair(client, "test_trader")
    renewed = await client.post("/api/auth/refresh", json={"refresh_token": refresh})
    assert renewed.status_code == 200, renewed.text
    new_access = renewed.json()["access_token"]
    assert (await client.get("/api/auth/me", headers=_bearer(new_access))).status_code == 200
    again = await client.post("/api/auth/refresh", json={"refresh_token": renewed.json()["refresh_token"]})
    assert again.status_code == 200, "the refresh token issued by a refresh was already revoked"


# ── Token checks ─────────────────────────────────────────────────────────────
async def test_one_revocation_lookup_per_request(client: AsyncClient, monkeypatch):
    access, _ = await _login_pair(client, "test_trader")
    calls = []
    original = blacklist.is_token_blacklisted

    async def counting(token):
        calls.append(token)
        return await original(token)

    monkeypatch.setattr(blacklist, "is_token_blacklisted", counting)
    assert (await client.get("/api/auth/me", headers=_bearer(access))).status_code == 200
    assert len(calls) == 1, f"{len(calls)} revocation lookups for one request"


async def test_forged_token_costs_no_database_lookup(client: AsyncClient, monkeypatch):
    calls = []

    async def counting(token):
        calls.append(token)
        return False

    monkeypatch.setattr(blacklist, "is_token_blacklisted", counting)
    forged = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.bad-signature"
    assert (await client.get("/api/auth/me", headers=_bearer(forged))).status_code == 401
    assert calls == []


# ── Account state takes effect immediately ───────────────────────────────────
async def test_demotion_takes_effect_immediately(client: AsyncClient, auth_headers, db_session):
    access, _ = await _login_pair(client, "test_trader")
    order = {"symbol": "XAUUSD", "action": "BUY", "volume": 0.01}
    assert (await client.post("/api/trade/order", headers=_bearer(access), json=order)).status_code != 403

    uid = await _user_id(db_session, "test_trader")
    assert (await client.put(f"/api/auth/users/{uid}", headers=auth_headers, json={"role": "viewer"})).status_code == 200

    assert (await client.post("/api/trade/order", headers=_bearer(access), json=order)).status_code == 403


async def test_deactivation_takes_effect_immediately(client: AsyncClient, auth_headers, db_session):
    access, refresh = await _login_pair(client, "test_trader")
    uid = await _user_id(db_session, "test_trader")

    resp = await client.put(f"/api/auth/users/{uid}", headers=auth_headers, json={"is_active": False})
    assert resp.status_code == 200 and resp.json()["is_active"] is False

    assert (await client.get("/api/auth/me", headers=_bearer(access))).status_code == 401
    assert (await client.post("/api/auth/refresh", json={"refresh_token": refresh})).status_code == 401
    login = await client.post("/api/auth/login", json={"username": "test_trader", "password": TEST_PASSWORD})
    assert login.status_code == 403


async def test_reactivation_restores_access(client: AsyncClient, auth_headers, db_session):
    uid = await _user_id(db_session, "test_trader")
    await client.put(f"/api/auth/users/{uid}", headers=auth_headers, json={"is_active": False})
    await client.put(f"/api/auth/users/{uid}", headers=auth_headers, json={"is_active": True})
    await _login_pair(client, "test_trader")


async def test_deleted_user_token_stops_working(client: AsyncClient, auth_headers, db_session):
    access, _ = await _login_pair(client, "test_viewer")
    uid = await _user_id(db_session, "test_viewer")
    assert (await client.delete(f"/api/auth/users/{uid}", headers=auth_headers)).status_code == 200
    assert (await client.get("/api/auth/me", headers=_bearer(access))).status_code == 401


async def test_admin_account_cannot_be_demoted_or_disabled(client: AsyncClient, auth_headers, db_session):
    uid = await _user_id(db_session, "admin")
    for change in ({"role": "viewer"}, {"is_active": False}):
        assert (await client.put(f"/api/auth/users/{uid}", headers=auth_headers, json=change)).status_code == 400


@pytest.mark.parametrize("role", ["superadmin", "", "Admin"])
async def test_unknown_roles_are_rejected(client: AsyncClient, auth_headers, db_session, role):
    uid = await _user_id(db_session, "test_trader")
    assert (await client.put(f"/api/auth/users/{uid}", headers=auth_headers, json={"role": role})).status_code == 400
    create = await client.post("/api/auth/users", headers=auth_headers,
                               json={"username": "new_one", "name": "New", "password": "a-long-enough-pass", "role": role})
    assert create.status_code == 400


async def test_weak_passwords_are_rejected_everywhere(client: AsyncClient, auth_headers, trader_headers, db_session):
    uid = await _user_id(db_session, "test_trader")
    create = await client.post("/api/auth/users", headers=auth_headers,
                               json={"username": "weak", "name": "Weak", "password": "Usdt@2026", "role": "trader"})
    update = await client.put(f"/api/auth/users/{uid}", headers=auth_headers, json={"password": "short"})
    change = await client.put("/api/auth/password", headers=trader_headers,
                              json={"current_password": TEST_PASSWORD, "new_password": "admin@2026"})
    assert (create.status_code, update.status_code, change.status_code) == (400, 400, 400)


# ── Login throttling ─────────────────────────────────────────────────────────
async def test_account_locks_after_repeated_failures(client: AsyncClient):
    for _ in range(5):
        bad = await client.post("/api/auth/login", json={"username": "test_trader", "password": "wrong-password"})
        assert bad.status_code == 401
    locked = await client.post("/api/auth/login", json={"username": "test_trader", "password": TEST_PASSWORD})
    assert locked.status_code == 429, "correct password accepted during lockout"
    assert "failed attempts" in locked.json()["detail"]


async def test_lockout_is_per_account(client: AsyncClient):
    for _ in range(5):
        await client.post("/api/auth/login", json={"username": "test_trader", "password": "wrong-password"})
    await _login_pair(client, "test_viewer")


async def test_success_clears_earlier_failures(client: AsyncClient):
    for _ in range(4):
        await client.post("/api/auth/login", json={"username": "test_trader", "password": "wrong-password"})
    await _login_pair(client, "test_trader")
    for _ in range(4):
        await client.post("/api/auth/login", json={"username": "test_trader", "password": "wrong-password"})
    await _login_pair(client, "test_trader")


async def test_address_limit(client: AsyncClient):
    codes = [
        (await client.post("/api/auth/login", json={"username": f"nobody{i}", "password": "x"})).status_code
        for i in range(11)
    ]
    assert codes[:10] == [401] * 10 and codes[10] == 429


async def test_unknown_username_still_checks_a_password(client: AsyncClient, monkeypatch):
    """Keeps response time the same whether or not the username exists."""
    checked = []
    real = auth_api.verify_password
    monkeypatch.setattr(auth_api, "verify_password", lambda pw, h: checked.append(h) or real(pw, h))
    resp = await client.post("/api/auth/login", json={"username": "does_not_exist", "password": "anything"})
    assert resp.status_code == 401
    assert checked == [auth_api._DUMMY_HASH]


# ── Sandbox always runs in a separate process ────────────────────────────────
@pytest.mark.parametrize("user_id", [0, 7])
async def test_ai_code_never_runs_inside_the_server(monkeypatch, user_id):
    from app.api import execute

    def in_process(*_args, **_kwargs):
        raise AssertionError("AI code ran inside the server process")

    monkeypatch.setattr(execute, "_execute_sandbox_sync", in_process)
    result = await execute.run_python_code("print(6 * 7)", user_id=user_id)
    assert result.get("success") is True, result
    assert result["output"].strip() == "42"
