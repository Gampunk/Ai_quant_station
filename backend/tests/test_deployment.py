"""
Step 11: password changes end sessions, the real client address behind a proxy,
production hides the API map, and every package version is pinned.

Negative controls:
- In get_current_user (app/core/security.py), delete the
  `issued_before_password_change` check. The "old access token" tests must fail.
- In set_password (app/core/security.py), stop increasing password_version.
  Every password-change test must fail.
- In client_ip (app/core/client_ip.py), return the first X-Forwarded-For entry
  without checking the peer. The "untrusted" and "spoofed" tests must fail.
- In backend/requirements.txt, change one `==` to `>=`. The pin test must fail.
"""
import itertools
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from starlette.requests import Request

from app.core import client_ip as client_ip_module
from app.core.client_ip import client_ip
from app.core.security import create_access_token, set_password
from app.models.user import User
from tests.credentials import TEST_PASSWORD

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent
NEW_PASSWORD = "a-brand-new-password-2026"


async def _login_pair(client, username, password=TEST_PASSWORD):
    resp = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"], resp.json()["refresh_token"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ── Changing a password ends every other session (finding 19) ───────────────
async def test_changing_your_password_ends_old_sessions(client: AsyncClient):
    old_access, old_refresh = await _login_pair(client, "test_trader")
    other_access, other_refresh = await _login_pair(client, "test_trader")  # another browser

    resp = await client.put("/api/auth/password", headers=_bearer(old_access),
                            json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})
    assert resp.status_code == 200, resp.text

    for access in (old_access, other_access):
        assert (await client.get("/api/auth/me", headers=_bearer(access))).status_code == 401, \
            "an old access token still works after a password change"
    for refresh in (old_refresh, other_refresh):
        assert (await client.post("/api/auth/refresh", json={"refresh_token": refresh})).status_code == 401, \
            "an old refresh token still works after a password change"


async def test_the_person_changing_it_gets_a_working_new_pair(client: AsyncClient):
    access, _ = await _login_pair(client, "test_trader")
    resp = await client.put("/api/auth/password", headers=_bearer(access),
                            json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})
    body = resp.json()
    assert (await client.get("/api/auth/me", headers=_bearer(body["access_token"]))).status_code == 200
    refreshed = await client.post("/api/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert refreshed.status_code == 200
    # And the new password logs in.
    await _login_pair(client, "test_trader", NEW_PASSWORD)


async def test_admin_resetting_a_password_ends_that_users_sessions(client: AsyncClient, auth_headers, db_session):
    access, refresh = await _login_pair(client, "test_viewer")
    user_id = (await db_session.execute(select(User.id).where(User.username == "test_viewer"))).scalar_one()

    resp = await client.put(f"/api/auth/users/{user_id}", headers=auth_headers, json={"password": NEW_PASSWORD})
    assert resp.status_code == 200, resp.text

    assert (await client.get("/api/auth/me", headers=_bearer(access))).status_code == 401
    assert (await client.post("/api/auth/refresh", json={"refresh_token": refresh})).status_code == 401


async def test_changing_a_name_or_role_does_not_end_sessions(client: AsyncClient, auth_headers, db_session):
    access, _ = await _login_pair(client, "test_viewer")
    user_id = (await db_session.execute(select(User.id).where(User.username == "test_viewer"))).scalar_one()
    await client.put(f"/api/auth/users/{user_id}", headers=auth_headers, json={"name": "Renamed"})
    assert (await client.get("/api/auth/me", headers=_bearer(access))).status_code == 200


async def test_tokens_from_before_this_step_still_work_until_a_change(client: AsyncClient, db_session):
    """Tokens issued before the upgrade carry no version, which counts as 0."""
    user = (await db_session.execute(select(User).where(User.username == "test_trader"))).scalar_one()
    legacy = create_access_token({"sub": user.username, "user_id": user.id, "role": user.role})
    assert (await client.get("/api/auth/me", headers=_bearer(legacy))).status_code == 200


def test_set_password_raises_the_version_every_time():
    user = User(username="u", name="u", hashed_password="x", password_version=0)
    set_password(user, NEW_PASSWORD)
    set_password(user, NEW_PASSWORD + "2")
    assert user.password_version == 2


# ── The real client address behind a proxy (finding 20) ─────────────────────
def _request(peer: str, forwarded: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return Request({"type": "http", "client": (peer, 1234), "headers": headers})


def test_without_a_trusted_proxy_the_header_is_ignored(monkeypatch):
    monkeypatch.setattr(client_ip_module.settings, "FORWARDED_ALLOW_IPS", "")
    assert client_ip(_request("203.0.113.5", "1.2.3.4")) == "203.0.113.5"


def test_an_untrusted_peer_cannot_choose_its_address(monkeypatch):
    monkeypatch.setattr(client_ip_module.settings, "FORWARDED_ALLOW_IPS", "172.30.57.10")
    assert client_ip(_request("203.0.113.5", "1.2.3.4")) == "203.0.113.5"


def test_the_trusted_proxy_passes_the_real_address(monkeypatch):
    monkeypatch.setattr(client_ip_module.settings, "FORWARDED_ALLOW_IPS", "172.30.57.10")
    assert client_ip(_request("172.30.57.10", "198.51.100.7")) == "198.51.100.7"


def test_a_spoofed_entry_before_the_real_one_is_ignored(monkeypatch):
    """nginx appends the address it saw. Anything to its left came from the client."""
    monkeypatch.setattr(client_ip_module.settings, "FORWARDED_ALLOW_IPS", "172.30.57.0/24")
    assert client_ip(_request("172.30.57.10", "9.9.9.9, 198.51.100.7")) == "198.51.100.7"


def test_a_malformed_proxy_list_is_refused():
    with pytest.raises(ValueError):
        client_ip_module._networks("172.30.57.10, not-an-address")


_usernames = itertools.count()


async def _fail_logins(client, forwarded, times):
    # A new username every time, so the per-account lockout never answers instead.
    codes = []
    for _ in range(times):
        r = await client.post("/api/auth/login", headers={"X-Forwarded-For": forwarded},
                              json={"username": f"nobody-{next(_usernames)}", "password": "wrong-password-123"})
        codes.append(r.status_code)
    return codes


async def test_behind_the_proxy_each_visitor_has_their_own_login_limit(client: AsyncClient, monkeypatch):
    # The test client connects from 127.0.0.1, standing in for nginx.
    monkeypatch.setattr(client_ip_module.settings, "FORWARDED_ALLOW_IPS", "127.0.0.1")
    first = await _fail_logins(client, "198.51.100.1", 11)
    assert first[-1] == 429, "the per-address limit never triggered"
    second = await _fail_logins(client, "198.51.100.2", 1)
    assert second == [401], "a second visitor was blocked by the first one's limit"


async def test_directly_the_header_cannot_dodge_the_login_limit(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(client_ip_module.settings, "FORWARDED_ALLOW_IPS", "")
    codes = [(await _fail_logins(client, f"198.51.100.{i}", 1))[0] for i in range(11)]
    assert codes[-1] == 429, "changing X-Forwarded-For escaped the limit without a proxy"


# ── Production hides the API map ────────────────────────────────────────────
def _docs_status(app_env: str) -> str:
    code = (
        "from starlette.testclient import TestClient; from app.main import app; c = TestClient(app); "
        "print(c.get('/docs').status_code, c.get('/openapi.json').status_code, c.get('/health').status_code)"
    )
    env = {**os.environ, "APP_ENV": app_env, "DATABASE_URL": "sqlite+aiosqlite:///:memory:"}
    out = subprocess.run([sys.executable, "-c", code], cwd=BACKEND_DIR, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout.strip().splitlines()[-1]


def test_production_hides_docs():
    assert _docs_status("production") == "404 404 200"


def test_development_shows_docs():
    assert _docs_status("development") == "200 200 200"


# ── Every package version is pinned (finding 13) ────────────────────────────
def _requirement_lines(path: Path):
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].rstrip(" \\").strip()
        if line and not line.startswith("-"):
            yield line


@pytest.mark.parametrize("name", ["requirements.txt", "requirements-dev.txt"])
def test_every_requirement_is_pinned_exactly(name):
    loose = [l for l in _requirement_lines(BACKEND_DIR / name) if not re.fullmatch(r"[A-Za-z0-9._\[\],-]+==[^=<>~!*\s]+", l)]
    assert not loose, f"{name} has unpinned lines: {loose}"


@pytest.mark.parametrize("name", ["requirements.txt", "requirements-dev.txt"])
def test_every_requirement_carries_a_hash(name):
    """Hashes make reading two indexes safe: a substituted package is refused."""
    text = (BACKEND_DIR / name).read_text()
    entries = re.findall(r"^[A-Za-z0-9._\[\],-]+==\S+(?: \\\n(?:\s+--hash=sha256:[0-9a-f]{64}(?: \\)?\n?)+)?", text, re.M)
    unhashed = [e.split()[0] for e in entries if "--hash=sha256:" not in e]
    assert entries and not unhashed, f"{name} has packages without a hash: {unhashed}"


def test_dev_tools_use_the_same_versions_as_production():
    def pins(name):
        return dict(re.findall(r"^([A-Za-z0-9._-]+)==(\S+)", (BACKEND_DIR / name).read_text(), re.M))
    prod, dev = pins("requirements.txt"), pins("requirements-dev.txt")
    differ = {k: (v, dev[k]) for k, v in prod.items() if k in dev and dev[k] != v}
    assert not differ and set(prod) <= set(dev), differ or set(prod) - set(dev)


def test_torch_is_the_cpu_build():
    text = (BACKEND_DIR / "requirements.txt").read_text()
    assert "download.pytorch.org/whl/cpu" in text
    assert re.search(r"^torch==\S+\+cpu\b", text, re.M)
    assert not re.search(r"^nvidia-", text, re.M), "GPU libraries would be installed"


# ── Deployment files ────────────────────────────────────────────────────────
def test_the_image_never_contains_secrets_or_local_state():
    ignored = (REPO_DIR / ".dockerignore").read_text().splitlines()
    for pattern in ("**/.env", "**/*.db", "**/*.db.*", "**/*.bak", "**/.venv/"):
        assert pattern in ignored, f".dockerignore misses {pattern}"


def test_the_container_runs_as_a_normal_user_on_the_port_it_exposes():
    dockerfile = (REPO_DIR / "Dockerfile").read_text()
    assert re.search(r"^USER (?!root)\S+", dockerfile, re.M)
    port = re.search(r"PORT=(\d+)", dockerfile).group(1)
    assert re.search(rf"^EXPOSE {port}$", dockerfile, re.M)
    assert "APP_ENV=production" in dockerfile


def test_only_nginx_is_reachable_from_outside():
    import yaml
    services = yaml.safe_load((REPO_DIR / "docker-compose.yml").read_text())["services"]
    assert "ports" not in services["postgres"] and "ports" not in services["backend"]
    assert services["nginx"]["ports"]
    assert ":?" in services["postgres"]["environment"]["POSTGRES_PASSWORD"], "Postgres must refuse a missing password"
    nginx_ip = services["nginx"]["networks"]["impulse_network"]["ipv4_address"]
    assert services["backend"]["environment"]["FORWARDED_ALLOW_IPS"] == nginx_ip


def test_nginx_config_has_one_http_block():
    conf = (REPO_DIR / "nginx.conf").read_text()
    assert len(re.findall(r"^http\s*\{", conf, re.M)) == 1
