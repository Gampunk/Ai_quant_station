from fastapi import APIRouter, Depends, HTTPException
from typing import Optional
import re
import logging
import yfinance as yf
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)
from ..core.security import get_current_user

router = APIRouter(prefix="/data", tags=["Market Data"])

# Yahoo Finance symbol allowlist — blocks path-traversal, file://, etc.
_SYMBOL_RE = re.compile(r'^[A-Za-z0-9._^=-]+$')

_VALIDATED_SYMBOLS: set[str] | None = None


def _get_valid_symbols() -> set[str]:
    global _VALIDATED_SYMBOLS
    if _VALIDATED_SYMBOLS is not None:
        return _VALIDATED_SYMBOLS
    raw = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X",
               "AUDUSD=X", "USDCAD=X", "NZDUSD=X", "EURGBP=X", "EURJPY=X", "GBPJPY=X",
               "BTC-USD", "ETH-USD", "XRP-USD", "SOL-USD",
               "DOGE-USD", "ADA-USD", "AVAX-USD", "DOT-USD",
               "MATIC-USD", "LINK-USD", "UNI-USD", "ATOM-USD",
               "^GSPC", "^DJI", "^IXIC", "^RUT", "^VIX", "^FTSE", "^GDAXI", "^N225", "^HSI",
               "XAUUSD=X", "XAGUSD=X", "US30=X", "SPX500=X", "NAS100=X", "DAX40=X"]
    _VALIDATED_SYMBOLS = set(raw)
    return _VALIDATED_SYMBOLS


def _validate_symbol(symbol: str) -> str:
    """Reject unsafe symbols (path traversal, local files, SSRF attempts)."""
    if not _SYMBOL_RE.match(symbol):
        raise HTTPException(status_code=400, detail=f"Invalid symbol format: '{symbol}'")
    return symbol


def _get_available_symbols() -> set[str]:
    return _get_valid_symbols()


@router.get("/yahoo/symbols")
async def get_available_symbols(
    current_user: dict = Depends(get_current_user)
):
    """Get available symbols for selection (forex, crypto, indices)."""
    try:
        results = []
        for pair in ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X",
                      "AUDUSD=X", "USDCAD=X", "NZDUSD=X", "EURGBP=X", "EURJPY=X", "GBPJPY=X"]:
            results.append({"symbol": pair, "type": "forex", "name": pair.replace("=X", "")})
        for pair in ["BTC-USD", "ETH-USD", "XRP-USD", "SOL-USD",
                      "DOGE-USD", "ADA-USD", "AVAX-USD", "DOT-USD",
                      "MATIC-USD", "LINK-USD", "UNI-USD", "ATOM-USD"]:
            results.append({"symbol": pair, "type": "crypto", "name": pair.replace("-USD", "")})
        for idx in ["^GSPC", "^DJI", "^IXIC", "^RUT", "^VIX", "^FTSE", "^GDAXI", "^N225", "^HSI"]:
            results.append({"symbol": idx, "type": "index", "name": idx})
        return {"success": True, "symbols": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/yahoo/{symbol}")
async def get_yahoo_data(
    symbol: str,
    period: str = "1mo",
    interval: str = "1d",
    current_user: dict = Depends(get_current_user)
):
    """Fetch market data from Yahoo Finance."""
    if symbol in ("quote", "search", "symbols"):
        raise HTTPException(status_code=404, detail="Not found")
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=period, interval=interval)
        if hist.empty:
            raise HTTPException(status_code=404, detail=f"No data found for {symbol}")
        data = []
        for idx, row in hist.iterrows():
            data.append({
                "time": idx.strftime('%Y-%m-%d %H:%M:%S'),
                "open": float(row['Open']), "high": float(row['High']),
                "low": float(row['Low']), "close": float(row['Close']),
                "volume": int(row['Volume'])
            })
        return {"success": True, "source": "yahoo", "symbol": symbol,
                "period": period, "interval": interval, "count": len(data), "data": data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Yahoo Finance error: {str(e)}")


@router.get("/yahoo/quote/{symbol}")
async def get_yahoo_quote(
    symbol: str,
    current_user: dict = Depends(get_current_user)
):
    """Get current quote from Yahoo Finance."""
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        return {
            "success": True, "symbol": symbol,
            "price": info.get('currentPrice') or info.get('regularMarketPrice'),
            "bid": info.get('bid'), "ask": info.get('ask'),
            "volume": info.get('volume'), "market_cap": info.get('marketCap'),
            "name": info.get('shortName') or info.get('longName'),
            "change": info.get('regularMarketChange'),
            "change_percent": info.get('regularMarketChangePercent')
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Yahoo Finance error: {str(e)}")


@router.get("/yahoo/search/{query}")
async def search_yahoo(
    query: str,
    current_user: dict = Depends(get_current_user)
):
    """Search for symbols on Yahoo Finance."""
    try:
        ticker = yf.Ticker(query)
        info = ticker.info
        results = []
        if info.get('symbol'):
            results.append({
                "symbol": info['symbol'],
                "name": info.get('shortName') or info.get('longName') or query,
                "type": info.get('instrumentType', 'Unknown')
            })
        return {"success": True, "query": query, "results": results}
    except Exception:
        logger.warning("[Yahoo] Symbol search failed for %r", query, exc_info=True)
        return {"success": True, "query": query, "results": []}


@router.get("/yahoo/forex")
async def get_forex_pairs(
    current_user: dict = Depends(get_current_user)
):
    """Get common forex pairs."""
    pairs = [
        "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X",
        "AUDUSD=X", "USDCAD=X", "NZDUSD=X",
        "EURGBP=X", "EURJPY=X", "GBPJPY=X"
    ]
    
    results = []
    for pair in pairs:
        try:
            ticker = yf.Ticker(pair)
            info = ticker.info
            results.append({
                "symbol": pair,
                "price": info.get('currentPrice') or info.get('regularMarketPrice'),
                "change": info.get('regularMarketChange'),
                "change_percent": info.get('regularMarketChangePercent')
            })
        except Exception:
            logger.warning(f"Failed to fetch forex pair {pair}")
    
    return {
        "success": True,
        "pairs": results
    }


@router.get("/yahoo/crypto")
async def get_crypto_pairs(
    current_user: dict = Depends(get_current_user)
):
    """Get common crypto pairs."""
    pairs = [
        "BTC-USD", "ETH-USD", "XRP-USD", "SOL-USD",
        "DOGE-USD", "ADA-USD", "AVAX-USD", "DOT-USD"
    ]
    
    results = []
    for pair in pairs:
        try:
            ticker = yf.Ticker(pair)
            info = ticker.info
            results.append({
                "symbol": pair,
                "name": info.get('shortName', pair),
                "price": info.get('currentPrice') or info.get('regularMarketPrice'),
                "change": info.get('regularMarketChange'),
                "change_percent": info.get('regularMarketChangePercent')
            })
        except Exception:
            logger.warning(f"Failed to fetch crypto pair {pair}")
    
    return {
        "success": True,
        "crypto": results
    }