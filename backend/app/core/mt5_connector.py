"""
MT5 Connector Client
Connects to external MT5 Connector service instead of direct MT5
"""
import httpx
from typing import Optional, Dict, Any
from ..core.config import settings


class MT5ConnectorClient:
    """Client for connecting to external MT5 Connector service."""
    
    def __init__(self):
        self.base_url = settings.MT5_CONNECTOR_URL
        self.timeout = 30.0
        self._client = httpx.AsyncClient(timeout=self.timeout)
    
    async def close(self):
        await self._client.aclose()
    
    async def _request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        """Make HTTP request to connector with auth token."""
        base = self.base_url.rstrip("/")
        path = "/" + endpoint.lstrip("/")
        url = f"{base}{path}"
        
        headers = kwargs.pop("headers", {})
        if settings.MT5_API_TOKEN:
            headers["Authorization"] = f"Bearer {settings.MT5_API_TOKEN}"
        
        response = await self._client.request(method, url, headers=headers, **kwargs)
        response.raise_for_status()
        return response.json()
    
    async def health(self) -> Dict[str, Any]:
        """Check connector health."""
        return await self._request("GET", "/health")
    
    async def initialize(self, terminal_path: Optional[str] = None) -> Dict[str, Any]:
        """Initialize MT5 connection."""
        return await self._request("POST", "/initialize", json={"terminal_path": terminal_path})
    
    async def shutdown(self) -> Dict[str, Any]:
        """Shutdown MT5 connection."""
        return await self._request("POST", "/shutdown")
    
    async def get_account(self) -> Dict[str, Any]:
        """Get account info."""
        return await self._request("GET", "/account")
    
    async def get_symbols(self) -> Dict[str, Any]:
        """Get all symbols."""
        return await self._request("GET", "/symbols")
    
    async def get_symbol(self, symbol: str) -> Dict[str, Any]:
        """Get specific symbol."""
        return await self._request("GET", f"/symbol/{symbol}")
    
    async def get_positions(self) -> Dict[str, Any]:
        """Get open positions."""
        return await self._request("GET", "/positions")

    async def get_orders(self) -> Dict[str, Any]:
        """Get currently active/pending orders."""
        return await self._request("GET", "/orders")
    
    async def get_history(self, hours: int = 0) -> Dict[str, Any]:
        """Get trade history."""
        return await self._request("GET", f"/history?hours={hours}")

    async def get_order_history(self, hours: int = 0) -> Dict[str, Any]:
        """Get historical order states, including cancelled and expired orders."""
        return await self._request("GET", f"/history/orders?hours={hours}")
    
    async def place_order(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
        """Place an order."""
        return await self._request("POST", "/order", json=order_data)
    
    async def close_position(self, ticket: int, volume: Optional[float] = None) -> Dict[str, Any]:
        """Close a position."""
        return await self._request("POST", "/close", json={"ticket": ticket, "volume": volume})
    
    async def modify_position(self, ticket: int, sl: Optional[float] = None, tp: Optional[float] = None) -> Dict[str, Any]:
        """Modify position SL/TP."""
        return await self._request("POST", "/modify", json={"ticket": ticket, "sl": sl, "tp": tp})
    
    async def get_latest_data(self, symbol: str, timeframe: str = "1h", count: int = 500) -> Dict[str, Any]:
        """Get latest OHLC data."""
        return await self._request("GET", f"/data/latest/{symbol}?timeframe={timeframe}&count={count}")


# Global client instance
connector_client = MT5ConnectorClient()

async def shutdown_connector():
    await connector_client.close()
