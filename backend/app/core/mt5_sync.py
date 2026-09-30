import logging
import asyncio
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import settings
from .historical_loader import LOCAL_CACHE, AVAILABLE_SYMBOLS, _read_parquet_cached
from .mt5_service import fetch_ohlc_range

logger = logging.getLogger(__name__)

def _upload_parquet_to_hf():
    """Upload current-year parquet files to HuggingFace (runs in thread pool)."""
    from .historical_loader import HF_REPO_ID
    from huggingface_hub import HfApi, login
    token = settings.HUGGINGFACE_API_KEY
    if not token:
        logger.info("[Sync] No HUGGINGFACE_API_KEY, skipping parquet upload")
        return
    current_year = datetime.now().year
    files = sorted(Path(LOCAL_CACHE).glob(f"*_{current_year}.parquet"))
    if not files:
        logger.info("[Sync] No parquet files to upload")
        return
    login(token=token)
    api = HfApi()
    for fpath in files:
        remote = f"data/{fpath.name}"
        try:
            api.upload_file(path_or_fileobj=str(fpath), path_in_repo=remote, repo_id=HF_REPO_ID, repo_type="dataset")
            logger.info(f"[Sync] Uploaded {fpath.name} to HF")
        except Exception as e:
            logger.warning(f"[Sync] HF upload failed for {fpath.name}: {e}")

async def sync_mt5_to_parquet():
    """
    The MT5 Auto-Sync Bridge:
    - Identifies gaps in 2026 parquet files.
    - Fetches missing 1-minute candles from local MT5.
    - Appends them to the archive.
    """
    # Clear RAM cache so the next backtest sees the new data
    _read_parquet_cached.cache_clear()
    
    # 0. Connection Gatekeeper: Check if MT5 is actually available before flooding logs
    from .mt5_service import init_mt5_connection
    if not await init_mt5_connection():
        logger.info("[Sync] Skipping sync: MT5/Connector is currently offline.")
        return

    logger.info("--- [Pillar 3] Starting MT5 Auto-Sync Sequence ---")
    
    current_year = datetime.now().year
    sync_count_total = 0
    
    for symbol in AVAILABLE_SYMBOLS:
        try:
            parquet_path = LOCAL_CACHE / f"{symbol}_{current_year}.parquet"
            last_timestamp = 0
            
            # 1. Determine the 'watermark' (last recorded candle)
            if parquet_path.exists():
                try:
                    # Only read the last row to save memory
                    df_check = pd.read_parquet(parquet_path, columns=["timestamp"])
                    if not df_check.empty:
                        last_timestamp = df_check["timestamp"].max()
                except Exception as e:
                    logger.warning(f"Could not read existing parquet for {symbol}: {e}")
            
            # 2. Define range
            # Fetch from last_timestamp + 60 seconds until now
            start_dt = datetime.fromtimestamp(last_timestamp + 60, tz=timezone.utc)
            end_dt = datetime.now(timezone.utc)
            
            if start_dt >= end_dt:
                logger.info(f"[Sync] {symbol} is already up to date.")
                continue
                
            # 3. Fetch data
            logger.info(f"[Sync] Fetching {symbol} from {start_dt} to {end_dt}...")
            raw_data = await fetch_ohlc_range(symbol, "1m", start_dt, end_dt)
            # Candle times arrive in UTC: the connector client converts broker time.
            
            if not raw_data:
                logger.info(f"[Sync] No new candles found for {symbol}.")
                continue
                
            # 4. Process and Format
            df_new = pd.DataFrame(raw_data)
            
            # Map MT5 columns to Archive columns
            # MT5: time, open, high, low, close, tick_volume
            # Archive: timestamp, open, high, low, close, volume
            rename_map = {
                "time": "timestamp",
                "tick_volume": "volume"
            }
            df_new = df_new.rename(columns=rename_map)
            
            # Ensure we only keep required columns
            cols = ["timestamp", "open", "high", "low", "close", "volume"]
            df_new = df_new[cols]
            
            # 5. Append and Save
            if parquet_path.exists():
                df_existing = pd.read_parquet(parquet_path)
                df_final = pd.concat([df_existing, df_new], ignore_index=True)
                # Remove overlaps if any
                df_final = df_final.drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
            else:
                df_final = df_new.sort_values("timestamp")
                
            df_final.to_parquet(parquet_path, index=False)
            logger.info(f"--- [Sync] SUCCESS: Appended {len(df_new)} rows to {symbol}_{current_year}.parquet ---")
            sync_count_total += len(df_new)
            
        except Exception as e:
            logger.error(f"[Sync] CRITICAL ERROR for {symbol}: {e}")

    if sync_count_total > 0:
        try:
            await asyncio.to_thread(_upload_parquet_to_hf)
        except Exception as e:
            logger.error(f"[Sync] Failed to upload parquet to HF: {e}")

# Global scheduler instance
scheduler = AsyncIOScheduler()

def start_sync_scheduler():
    if not scheduler.running:
        scheduler.add_job(sync_mt5_to_parquet, 'date', run_date=datetime.now(timezone.utc))
        scheduler.add_job(sync_mt5_to_parquet, 'interval', minutes=60)
        scheduler.start()
        logger.info("MT5 Auto-Sync Scheduler initialized (Hourly).")

def shutdown_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        logger.info("MT5 Auto-Sync Scheduler shut down.")
