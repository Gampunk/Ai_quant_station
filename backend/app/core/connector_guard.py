"""
Keeps connector traffic on local and private networks unless explicitly allowed.

Why this exists: the refactor runs locally while a live system runs elsewhere.
A copied .env file or a saved autopilot setting that points at the live
connector would let local work place real trades. By default only loopback
and private network addresses are accepted. Set ALLOW_REMOTE_CONNECTOR=true
to lift the block deliberately, for example during a planned production cutover.
"""
import ipaddress
import socket
from functools import lru_cache
from urllib.parse import urlparse

from .config import settings


class ConnectorAddressBlocked(ValueError):
    """Raised when a connector URL points outside local and private networks."""


@lru_cache(maxsize=64)
def _resolve(host: str) -> tuple[str, ...]:
    infos = socket.getaddrinfo(host, None)
    return tuple(sorted({info[4][0] for info in infos}))


def _is_local_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])
    return (ip.is_loopback or ip.is_private) and not ip.is_unspecified


def check_connector_url(url: str | None) -> None:
    """Raise ConnectorAddressBlocked unless url is local, private, or explicitly allowed.

    An empty url means no connector is configured and passes.
    """
    if not url or settings.ALLOW_REMOTE_CONNECTOR:
        return

    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConnectorAddressBlocked(
            f"Connector URL must start with http:// or https:// and include a host: {url!r}"
        )

    host = parsed.hostname
    try:
        addresses = (host,) if _is_ip_literal(host) else _resolve(host)
    except OSError as exc:
        raise ConnectorAddressBlocked(f"Cannot resolve connector host {host!r}: {exc}") from exc

    public = [a for a in addresses if not _is_local_ip(a)]
    if public:
        raise ConnectorAddressBlocked(
            f"Connector host {host!r} resolves to non-local address {public[0]}. "
            "Set ALLOW_REMOTE_CONNECTOR=true only if you intend to reach a remote connector."
        )


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%")[0])
        return True
    except ValueError:
        return False
