"""
The one way the backend talks to the MT5 connector.

Every order, close, modify and market data request goes through connector_client.
It reads the address and token from the server settings, checks the address is
local or private, sends the token, and turns connector failures into
ConnectorError with the connector's own message and status.

There is no second route. An earlier version also called the Windows-only
MetaTrader5 package directly, which could not work on a Linux server, and let
each user set their own connector address.
"""
from typing import Any, Dict, Optional

import httpx

from ..core.config import settings
from .connector_guard import check_connector_url


class ConnectorError(Exception):
    """The connector could not be reached, or answered with an error."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{status_code}: {detail}")


class MT5ConnectorClient:
    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None, timeout: float = 30.0):
        # None means "read the server settings at call time", so a settings change,
        # including in tests, takes effect without rebuilding the client.
        self._base_url = base_url
        self._token = token
        self.timeout = timeout

    @property
    def base_url(self) -> str:
        return (self._base_url if self._base_url is not None else settings.MT5_CONNECTOR_URL or "").strip()

    @base_url.setter
    def base_url(self, value: str) -> None:
        self._base_url = value

    @property
    def token(self) -> str:
        return self._token if self._token is not None else settings.MT5_API_TOKEN

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    async def request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        if not self.base_url:
            raise ConnectorError(503, "MT5_CONNECTOR_URL is not set in the server settings")
        check_connector_url(self.base_url)

        headers = kwargs.pop("headers", {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        url = self.base_url.rstrip("/") + "/" + path.lstrip("/")

        # A short-lived client per request: callers run on different event loops
        # (the app, schedulers, tests), and a shared client is bound to one of them.
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ConnectorError(503, f"MT5 connector unreachable: {exc}") from exc

        if response.status_code >= 400:
            try:
                detail = response.json().get("detail") or response.text
            except ValueError:
                detail = response.text
            raise ConnectorError(response.status_code, str(detail)[:500])
        return response.json()

    # ── Terminal and account ──────────────────────────────────────────────
    async def health(self) -> Dict[str, Any]:
        return await self.request("GET", "/health")

    async def initialize(self) -> Dict[str, Any]:
        return await self.request("POST", "/initialize")

    async def get_account(self) -> Dict[str, Any]:
        return await self.request("GET", "/account")

    # ── Market data ───────────────────────────────────────────────────────
    async def get_symbols(self) -> Dict[str, Any]:
        return await self.request("GET", "/symbols")

    async def get_symbol(self, symbol: str) -> Dict[str, Any]:
        return await self.request("GET", f"/symbol/{symbol}")

    async def get_latest_data(self, symbol: str, timeframe: str = "1h", count: int = 500) -> Dict[str, Any]:
        return await self.request("GET", f"/data/latest/{symbol}", params={"timeframe": timeframe, "count": count})

    async def get_range(self, symbol: str, timeframe: str, start: str, end: str) -> Dict[str, Any]:
        return await self.request("GET", f"/data/range/{symbol}",
                                  params={"timeframe": timeframe, "start": start, "end": end})

    # ── Positions and history ─────────────────────────────────────────────
    async def get_positions(self) -> Dict[str, Any]:
        return await self.request("GET", "/positions")

    async def get_history(self, hours: int = 0) -> Dict[str, Any]:
        return await self.request("GET", "/history", params={"hours": hours})

    # ── Trading. These are the only functions that change a position. ─────
    async def place_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        return await self.request("POST", "/order", json=order)

    async def close_position(self, ticket: int, volume: Optional[float] = None) -> Dict[str, Any]:
        return await self.request("POST", "/close", json={"ticket": ticket, "volume": volume})

    async def modify_position(self, ticket: int, sl: Optional[float] = None, tp: Optional[float] = None) -> Dict[str, Any]:
        return await self.request("POST", "/modify", json={"ticket": ticket, "sl": sl, "tp": tp})

    async def close(self) -> None:
        """Kept for callers that close the client at shutdown. Nothing to release."""


# The shared instance every module uses.
connector_client = MT5ConnectorClient()


async def shutdown_connector():
    await connector_client.close()
