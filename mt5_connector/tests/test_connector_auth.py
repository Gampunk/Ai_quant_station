"""
Token check and docs page.

The token must be read from the Authorization header. An earlier version
declared it as a plain function argument, which FastAPI reads from the query
string, so the header every client sends was ignored and a configured token
rejected all real traffic.

Negative control: make verify_auth return True as its first line. Every test
here except the two "no token configured" cases must fail.
"""
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

import connector
from conftest import CONNECTOR_DIR, FAKE_DIR, TEST_TOKEN


@pytest.fixture
def tokened(mt5, monkeypatch):
    """Connector that requires a token. The client sends none by default."""
    monkeypatch.setattr(connector, "CONNECTOR_API_TOKEN", TEST_TOKEN)
    monkeypatch.setattr(connector, "ALLOW_NO_TOKEN", False)
    monkeypatch.setattr(connector, "mt5_initialized", True)
    with TestClient(connector.app) as c:
        yield c


def test_correct_token_in_header_is_accepted(tokened):
    resp = tokened.get("/health", headers={"Authorization": f"Bearer {TEST_TOKEN}"})
    assert resp.status_code == 200


def test_token_without_the_bearer_prefix_is_accepted(tokened):
    assert tokened.get("/health", headers={"Authorization": TEST_TOKEN}).status_code == 200


def test_missing_header_is_rejected(tokened):
    assert tokened.get("/health").status_code == 401


def test_wrong_token_is_rejected(tokened):
    assert tokened.get("/health", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_empty_bearer_is_rejected(tokened):
    assert tokened.get("/health", headers={"Authorization": "Bearer "}).status_code == 401


def test_token_in_the_query_string_does_not_authenticate(tokened):
    """The original bug. A token supplied as a query parameter must not work."""
    assert tokened.get("/health", params={"authorization": TEST_TOKEN}).status_code == 401
    assert tokened.get("/health", params={"authorization": f"Bearer {TEST_TOKEN}"}).status_code == 401


@pytest.mark.parametrize("method, path, body", [
    ("get", "/account", None),
    ("get", "/positions", None),
    ("get", "/history", None),
    ("get", "/symbols", None),
    ("get", "/symbol/XAUUSD", None),
    ("get", "/data/latest/XAUUSD", None),
    ("get", "/data/range/XAUUSD", None),
    ("post", "/initialize", None),
    ("post", "/shutdown", None),
    ("post", "/order", {"symbol": "XAUUSD", "action": "BUY", "volume": 0.1}),
    ("post", "/close", {"ticket": 1}),
    ("post", "/modify", {"ticket": 1, "sl": 1.0}),
    ("get", "/", None),
])
def test_every_endpoint_requires_the_token(tokened, method, path, body):
    resp = getattr(tokened, method)(path, json=body) if body else getattr(tokened, method)(path)
    assert resp.status_code == 401, f"{method.upper()} {path} answered {resp.status_code} with no token"


def test_no_token_configured_refuses_everything(mt5, monkeypatch):
    monkeypatch.setattr(connector, "CONNECTOR_API_TOKEN", "")
    monkeypatch.setattr(connector, "ALLOW_NO_TOKEN", False)
    monkeypatch.setattr(connector, "mt5_initialized", True)
    with TestClient(connector.app) as c:
        resp = c.get("/health")
    assert resp.status_code == 503
    assert "MT5_API_TOKEN" in resp.json()["detail"]


def test_no_token_can_be_allowed_explicitly(mt5, monkeypatch):
    monkeypatch.setattr(connector, "CONNECTOR_API_TOKEN", "")
    monkeypatch.setattr(connector, "ALLOW_NO_TOKEN", True)
    monkeypatch.setattr(connector, "mt5_initialized", True)
    with TestClient(connector.app) as c:
        assert c.get("/health").status_code == 200


# ── Docs page ────────────────────────────────────────────────────────────────
def _app_in_subprocess(extra_env, code):
    env = {k: v for k, v in os.environ.items() if not k.startswith("MT5_")}
    env.update({"MT5_CONNECTOR_PORT": "5999", "MT5_API_TOKEN": "import-time-placeholder",
                "PYTHONPATH": os.pathsep.join([FAKE_DIR, CONNECTOR_DIR])})
    env.update(extra_env)
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                         timeout=60, cwd=CONNECTOR_DIR)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


_PROBE = (
    "import connector\n"
    "from fastapi.testclient import TestClient\n"
    "c = TestClient(connector.app)\n"
    "print(c.get('/docs').status_code, c.get('/openapi.json').status_code, c.get('/redoc').status_code)\n"
)


def test_docs_page_is_off_by_default():
    assert _app_in_subprocess({}, _PROBE) == "404 404 404"


def test_docs_page_can_be_enabled():
    assert _app_in_subprocess({"MT5_ENABLE_DOCS": "true"}, _PROBE) == "200 200 200"
