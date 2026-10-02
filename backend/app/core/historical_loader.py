"""
Historical Data Loader
Smart Parquet loading service that merges yearly files
into a single continuous DataFrame for analysis.
"""
import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from functools import lru_cache

import pandas as pd
import ta
from huggingface_hub import hf_hub_download

from app.core.config import settings

logger = logging.getLogger(__name__)

LOCAL_CACHE = Path(__file__).resolve().parent.parent.parent.parent / "data_archive" / "parquet_storage"
HF_REPO_ID = settings.HF_REPO_ID or "TheFinanceEngineer/impulse-market-data"
HF_TOKEN = settings.HUGGINGFACE_API_KEY

AVAILABLE_SYMBOLS = ["XAUUSD", "XAGUSD", "EURUSD", "USDJPY", "GBPUSD", "BTCUSD"]


def _get_parquet_path(symbol: str, year: int) -> Optional[Path]:
    """Get file from local cache first, then fall back to HuggingFace."""
    local_path = LOCAL_CACHE / f"{symbol}_{year}.parquet"
    if local_path.exists():
        return local_path

    # Download from HuggingFace as fallback
    try:
        logger.info(f"Downloading {symbol}_{year}.parquet from HuggingFace...")
        path = hf_hub_download(
            repo_id=HF_REPO_ID,
            filename=f"data/{symbol}/{symbol}_{year}.parquet",
            repo_type="dataset",
            token=HF_TOKEN,
            local_dir=str(LOCAL_CACHE)
        )
        return Path(path)
    except Exception as e:
        logger.warning(f"Could not load {symbol}_{year}: {e}")
        return None


@lru_cache(maxsize=32)
def _read_parquet_cached(path_str: str) -> pd.DataFrame:
    """Reads a parquet file and caches the result in memory.
    Uses path_str because Path objects might not be hashable in all python versions for lru_cache.
    maxsize=32 means we can hold about 32 years of data in RAM (~2.5GB).
    """
    logger.info(f"Cache miss: Loading {path_str} from disk...")
    return pd.read_parquet(path_str)


def load_data(
    symbol: str,
    start_date: str,
    end_date: str,
    timeframe: str = "1T"  # "1T" = 1-minute, "5T" = 5-minute, "1H" = 1-hour
) -> Optional[pd.DataFrame]:
    """
    Load and merge historical parquet data for a symbol over a date range.
    Automatically resamples to the requested timeframe.

    Returns a clean DataFrame with: timestamp, open, high, low, close, volume
    """
    if symbol not in AVAILABLE_SYMBOLS:
        return None
    start_dt = pd.to_datetime(start_date)
    end_dt = pd.to_datetime(end_date)
    start_year = start_dt.year
    end_year = end_dt.year

    dfs = []
    for year in range(start_year, end_year + 1):
        path = _get_parquet_path(symbol, year)
        if path:
            # Use cached reader
            df = _read_parquet_cached(str(path))
            dfs.append(df)

    if not dfs:
        logger.error(f"No data found for {symbol} between {start_date} and {end_date}")
        return None

    # Merge all years
    df = pd.concat(dfs, ignore_index=True)
    df = df.drop_duplicates(subset=["timestamp"], keep="last").sort_values("timestamp")

    # Convert timestamp → datetime index for filtering and resampling
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df = df.set_index("datetime")

    # Filter to exact date range
    df = df.loc[start_date:end_date]

    if df.empty:
        logger.error(f"No data in range {start_date} → {end_date} for {symbol}")
        return None

    # Resample to requested timeframe
    if timeframe != "1T":
        df = df.resample(timeframe).agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "timestamp": "first"
        }).dropna()

    df = df.reset_index()
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add a comprehensive set of technical indicators to the DataFrame.
    Uses the `ta` library (supports RSI, MACD, ATR, BB, EMA, SMA, etc.)
    """
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    # --- Trend Indicators ---
    df["ema_9"] = ta.trend.ema_indicator(close, window=9)
    df["ema_21"] = ta.trend.ema_indicator(close, window=21)
    df["ema_50"] = ta.trend.ema_indicator(close, window=50)
    df["ema_200"] = ta.trend.ema_indicator(close, window=200)
    df["sma_20"] = ta.trend.sma_indicator(close, window=20)
    df["sma_50"] = ta.trend.sma_indicator(close, window=50)

    macd = ta.trend.MACD(close)
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()

    # --- Momentum Indicators ---
    df["rsi_14"] = ta.momentum.rsi(close, window=14)
    stoch = ta.momentum.StochasticOscillator(high, low, close)
    df["stoch_k"] = stoch.stoch()
    df["stoch_d"] = stoch.stoch_signal()
    df["adx_14"] = ta.trend.adx(high, low, close, window=14)
    df["adx_pos_14"] = ta.trend.adx_pos(high, low, close, window=14)
    df["adx_neg_14"] = ta.trend.adx_neg(high, low, close, window=14)

    # --- Volatility Indicators ---
    bb = ta.volatility.BollingerBands(close, window=20)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_middle"] = bb.bollinger_mavg()
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_width"] = bb.bollinger_wband()

    df["atr_14"] = ta.volatility.average_true_range(high, low, close, window=14)

    # --- Volume Indicators ---
    df["obv"] = ta.volume.on_balance_volume(close, volume)

    return df


def get_available_years(symbol: str) -> list:
    """Return list of available years for a symbol.
    
    Checks local cache first, then falls back to a sensible default range
    (2006–2026) since HuggingFace is guaranteed to have the data.
    """
    if symbol not in AVAILABLE_SYMBOLS:
        return []
    years = set()
    # 1. Check local cache
    for f in LOCAL_CACHE.glob(f"{symbol}_*.parquet"):
        try:
            year = int(f.stem.split("_")[1])
            years.add(year)
        except (ValueError, IndexError):
            pass
    # 2. If local cache is empty, check HuggingFace repo listing
    if not years:
        try:
            from huggingface_hub import list_repo_refs
            # Try to fetch year-ranged files from the HF dataset repo
            import requests as _req
            api_url = f"https://huggingface.co/api/datasets/{HF_REPO_ID}/tree/main/data/{symbol}"
            resp = _req.get(api_url, timeout=10, headers={"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {})
            if resp.ok:
                for entry in resp.json():
                    if entry["type"] == "file":
                        parts = entry["path"].split("_")
                        if len(parts) == 2:
                            try:
                                years.add(int(parts[1].replace(".parquet", "")))
                            except ValueError:
                                pass
        except Exception:
            logger.warning("Could not list the years available for %s on Hugging Face", symbol, exc_info=True)
    # 3. Ultimate fallback: return years that are most commonly available
    if not years:
        years = set(range(2006, 2027))
    return sorted(years)
