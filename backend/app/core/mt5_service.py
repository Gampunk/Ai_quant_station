"""
Candle fetching for background jobs and the AI sandbox, through the MT5 connector.

An earlier version used the connector only when MT5_USE_EXTERNAL_CONNECTOR was
also set, and otherwise fell into a Windows-only branch. On Linux that branch
crashed with "'NoneType' object has no attribute 'TIMEFRAME_M1'", which the
hourly price sync logged and then reported as success.
"""
import logging
from datetime import datetime
from typing import Any, Dict, List

from .mt5_connector import ConnectorError, connector_client

logger = logging.getLogger(__name__)


async def init_mt5_connection() -> bool:
    """True if a connector is configured and answering."""
    if not connector_client.configured:
        return False
    try:
        await connector_client.health()
        return True
    except ConnectorError as exc:
        logger.info("MT5 connector not available: %s", exc.detail)
        return False


async def fetch_ohlc_range(symbol: str, timeframe: str, start_dt: datetime, end_dt: datetime) -> List[Dict[str, Any]]:
    """Candles for a date range, or an empty list if the connector cannot supply them."""
    try:
        res = await connector_client.get_range(symbol, timeframe, start_dt.isoformat(), end_dt.isoformat())
        return res.get("data", [])
    except ConnectorError as exc:
        logger.error("Connector range fetch failed for %s: %s", symbol, exc.detail)
        return []


async def fetch_latest_candles(symbol: str, count: int = 5000, timeframe: str = "1m") -> List[Dict[str, Any]]:
    """The latest candles, or an empty list if the connector cannot supply them."""
    try:
        res = await connector_client.get_latest_data(symbol, timeframe, count)
        return res.get("data", [])
    except ConnectorError as exc:
        logger.error("Connector latest fetch failed for %s: %s", symbol, exc.detail)
        return []
