"""
Structural checks: one route to the broker.

These read the source rather than run it, so they fail the moment someone adds
a second route. Negative control: add `requests.post(url + "/order")` to any
backend module, or `import MetaTrader5`, and the matching test fails.
"""
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
GATEWAY = APP / "core" / "mt5_connector.py"


def _sources():
    return [(p, p.read_text()) for p in APP.rglob("*.py")]


def test_no_backend_module_uses_the_windows_only_package():
    offenders = [str(p.relative_to(APP)) for p, src in _sources()
                 if re.search(r"^\s*(import MetaTrader5|from MetaTrader5)", src, re.M)]
    assert offenders == []


def test_only_the_gateway_sends_trading_requests():
    """Route declarations like @router.post("/order") are incoming paths, not requests."""
    # Matches "/order" and f"{url}/order" alike: the old code built URLs that way.
    pattern = re.compile(r"""/(order|close|modify)["']""")
    offenders = []
    for path, src in _sources():
        if path == GATEWAY:
            continue
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith(("@", "#")):
                continue
            if pattern.search(line):
                offenders.append(f"{path.relative_to(APP)}: {stripped[:60]}")
    assert offenders == [], f"trading requests built outside the connector client: {offenders}"


def test_only_the_gateway_opens_http_connections_to_the_connector():
    offenders = []
    for p, src in _sources():
        if p == GATEWAY:
            continue
        if "MT5_CONNECTOR_URL" in src and ("httpx." in src or "requests." in src):
            offenders.append(str(p.relative_to(APP)))
    assert offenders == [], f"modules building their own connector requests: {offenders}"
