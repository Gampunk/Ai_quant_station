"""
Loads the real connector.py with the fake MetaTrader5 module in front of it.
"""
import os
import sys

import pytest

CONNECTOR_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_DIR = os.path.join(CONNECTOR_DIR, "testing", "fake_mt5")
sys.path.insert(0, FAKE_DIR)
sys.path.insert(1, CONNECTOR_DIR)

# connector.py prompts for a port and a token at import time unless these are set.
# Every test sets the token it needs on the module itself.
os.environ.setdefault("MT5_CONNECTOR_PORT", "5999")
os.environ.setdefault("MT5_API_TOKEN", "import-time-placeholder")

import MetaTrader5 as fake_mt5  # noqa: E402

assert getattr(fake_mt5, "IS_FAKE", False), "tests must never load the real MetaTrader5 package"

import connector  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

FIXED_NOW = 1_789_380_000  # 2026-09-14 10:00:00 UTC
TEST_TOKEN = "test-token-abc123"


@pytest.fixture
def mt5():
    fake_mt5._reset(now=FIXED_NOW)
    return fake_mt5


@pytest.fixture
def client(mt5, monkeypatch):
    """Connector with a demo account, initialized, demo guard on, token required."""
    monkeypatch.setattr(connector, "mt5_initialized", False)
    monkeypatch.setattr(connector, "REQUIRE_DEMO", True)
    monkeypatch.setattr(connector, "CONNECTOR_API_TOKEN", TEST_TOKEN)
    monkeypatch.setattr(connector, "ALLOW_NO_TOKEN", False)
    with TestClient(connector.app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as c:
        resp = c.post("/initialize")
        assert resp.status_code == 200, resp.text
        yield c


def buy(client, **overrides):
    body = {"symbol": "XAUUSD", "action": "BUY", "volume": 0.10}
    body.update(overrides)
    return client.post("/order", json=body)
