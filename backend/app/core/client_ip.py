"""The address a request really came from.

Behind nginx every request arrives from nginx's own address, so the login limit
per address was shared by everyone. nginx passes the real address in
X-Forwarded-For. That header is believed only when the request comes from an
address listed in FORWARDED_ALLOW_IPS; from anywhere else a client could write
whatever it likes in it.
"""
import ipaddress

from starlette.requests import Request

from .config import settings


def _networks(spec: str) -> list:
    nets = []
    for part in spec.split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    return nets


def _is_trusted(address: str, trusted: list) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in net for net in trusted)


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    trusted = _networks(settings.FORWARDED_ALLOW_IPS)
    if not trusted or not _is_trusted(peer, trusted):
        return peer
    # Each proxy appends the address it received from, so read from the right and
    # take the first address that is not one of our own proxies. Anything further
    # left was written by the client and proves nothing.
    hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    for hop in reversed(hops):
        if not _is_trusted(hop, trusted):
            return hop
    return peer
