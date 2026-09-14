"""
Guard that keeps connector traffic on local and private networks.

Negative control: make check_connector_url return immediately, then run this
file. Every "blocked" and "rejected" test must fail, proving they test the guard.
"""
import socket

import pytest
from httpx import AsyncClient

from app.core import connector_guard
from app.core.config import settings
from app.core.connector_guard import ConnectorAddressBlocked, check_connector_url


@pytest.fixture(autouse=True)
def _guard_on(monkeypatch):
    """Force the default regardless of the developer's environment."""
    monkeypatch.setattr(settings, "ALLOW_REMOTE_CONNECTOR", False)
    connector_guard._resolve.cache_clear()
    yield
    connector_guard._resolve.cache_clear()


def _fake_dns(monkeypatch, mapping):
    def fake_getaddrinfo(host, *_args, **_kwargs):
        if host not in mapping:
            raise socket.gaierror(f"no such host {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (addr, 0)) for addr in mapping[host]]
    monkeypatch.setattr(connector_guard.socket, "getaddrinfo", fake_getaddrinfo)


@pytest.mark.parametrize("url", [
    "",
    None,
    "http://127.0.0.1:5001",
    "http://[::1]:5001",
    "http://10.0.0.5:5001",
    "http://172.20.0.1:5001",      # typical WSL to Windows host address
    "http://192.168.1.20:5001",
    "https://192.168.1.20",
])
def test_local_and_private_addresses_are_allowed(url):
    check_connector_url(url)


@pytest.mark.parametrize("url", [
    "http://8.8.8.8:5001",          # public address
    "http://0.0.0.0:5001",          # unspecified, never a real destination
    "192.168.1.20:5001",            # missing scheme
    "ftp://127.0.0.1:5001",         # wrong scheme
    "None/order",                   # what an unset URL becomes when a path is appended
])
def test_non_local_or_malformed_addresses_are_blocked(url):
    with pytest.raises(ConnectorAddressBlocked):
        check_connector_url(url)


def test_hostname_resolving_to_private_address_is_allowed(monkeypatch):
    _fake_dns(monkeypatch, {"windows-host": ["172.20.0.1"]})
    check_connector_url("http://windows-host:5001")


def test_hostname_resolving_to_public_address_is_blocked(monkeypatch):
    _fake_dns(monkeypatch, {"live-vps.example": ["8.8.4.4"]})
    with pytest.raises(ConnectorAddressBlocked, match="8.8.4.4"):
        check_connector_url("http://live-vps.example:5001")


def test_hostname_with_any_public_address_is_blocked(monkeypatch):
    _fake_dns(monkeypatch, {"mixed.example": ["192.168.1.5", "8.8.4.4"]})
    with pytest.raises(ConnectorAddressBlocked):
        check_connector_url("http://mixed.example:5001")


def test_unresolvable_hostname_is_blocked(monkeypatch):
    _fake_dns(monkeypatch, {})
    with pytest.raises(ConnectorAddressBlocked, match="Cannot resolve"):
        check_connector_url("http://nowhere.example:5001")


def test_explicit_override_allows_remote(monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_REMOTE_CONNECTOR", True)
    check_connector_url("http://8.8.8.8:5001")


async def test_autopilot_request_is_blocked_before_any_network_call(monkeypatch):
    from app.api import autopilot

    def no_network():
        raise AssertionError("the HTTP client was used, so the guard did not run first")
    monkeypatch.setattr(autopilot, "get_http_client", no_network)

    with pytest.raises(ConnectorAddressBlocked):
        await autopilot.async_request("POST", "http://8.8.8.8:5001/order", json={"symbol": "XAUUSD"})


async def test_saving_a_public_connector_url_is_rejected(client: AsyncClient, auth_headers: dict):
    resp = await client.post("/api/autopilot/settings", headers=auth_headers, json={
        "symbol": "XAUUSD", "provider": "nvidia", "model": "x",
        "mt5_connector_url": "http://8.8.8.8:5001",
    })
    assert resp.status_code == 400, resp.text
    assert "non-local" in resp.json()["detail"]


async def test_connecting_to_a_public_connector_url_is_rejected(client: AsyncClient, auth_headers: dict):
    resp = await client.post(
        "/api/autopilot/connect-mt5",
        headers=auth_headers,
        params={"connector_url": "http://8.8.8.8:5001"},
    )
    assert resp.status_code == 400, resp.text


async def test_saving_a_local_connector_url_still_works(client: AsyncClient, auth_headers: dict):
    resp = await client.post("/api/autopilot/settings", headers=auth_headers, json={
        "symbol": "XAUUSD", "provider": "nvidia", "model": "x",
        "mt5_connector_url": "http://127.0.0.1:5001",
    })
    assert resp.status_code == 200, resp.text


async def test_mt5_connector_client_blocks_public_base_url(monkeypatch):
    from app.core.mt5_connector import MT5ConnectorClient
    c = MT5ConnectorClient()
    c.base_url = "http://8.8.8.8:5001"
    try:
        with pytest.raises(ConnectorAddressBlocked):
            await c.health()
    finally:
        await c.close()
