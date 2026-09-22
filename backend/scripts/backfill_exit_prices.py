"""
Backfill exit_price for all historical trades from MT5 history.
Run from backend/: python scripts/backfill_exit_prices.py
"""
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings
from app.core.connector_guard import check_connector_url
from app.core.database import AsyncSessionLocal
from app.models.ai_memory import AutopilotTrade

MT5_HISTORY_HOURS = 8760  # 1 year


async def backfill():
    mt5_url = settings.MT5_CONNECTOR_URL
    if not mt5_url:
        print("MT5_CONNECTOR_URL not set in .env")
        return
    check_connector_url(mt5_url)

    headers = {}
    if settings.MT5_API_TOKEN:
        headers["Authorization"] = f"Bearer {settings.MT5_API_TOKEN}"

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AutopilotTrade).where(
                AutopilotTrade.exit_price.is_(None),
                AutopilotTrade.mt5_ticket.isnot(None),
            )
        )
        trades = result.scalars().all()
        print(f"Found {len(trades)} trades with missing exit_price")

        if not trades:
            return

        # Fetch MT5 history
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{mt5_url.rstrip('/')}/history",
                params={"hours": MT5_HISTORY_HOURS},
                headers=headers,
            )
            if resp.status_code != 200:
                print(f"Failed to fetch MT5 history: {resp.status_code}")
                return
            deals = resp.json().get("deals", [])

        updated = 0
        for trade in trades:
            for deal in deals:
                is_match = (
                    deal.get("position_id") == trade.mt5_ticket
                    or deal.get("ticket") == trade.mt5_ticket
                )
                if is_match and deal.get("entry") == "CLOSE":
                    trade.exit_price = deal.get("price")
                    if not trade.closed_at and deal.get("time"):
                        try:
                            trade.closed_at = datetime.strptime(deal["time"], "%Y-%m-%d %H:%M:%S")
                        except ValueError:
                            pass
                    updated += 1
                    break

        if updated > 0:
            await db.commit()
            print(f"Updated {updated} trades with exit_price")
        else:
            print("No trades matched")


if __name__ == "__main__":
    asyncio.run(backfill())
