"""
Alerts: every one is stored (alerts table, shown on the Risk Limits card), logged,
and sent to Telegram when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set.

Telegram is the channel the upstream branch uses for reports, with the same
settings names, so the two meet cleanly when the branches are merged.
"""
import logging

import httpx

from .config import settings
from .database import AsyncSessionLocal
from ..models.risk import Alert

log = logging.getLogger("alerts")

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"


async def send_telegram(text: str) -> bool:
    """Send one message. False when Telegram is not set up or the send failed."""
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(TELEGRAM_URL.format(token=settings.TELEGRAM_BOT_TOKEN),
                                     json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": text})
        if resp.status_code != 200:
            log.warning("Telegram refused an alert: %s %s", resp.status_code, resp.text[:200])
            return False
        return True
    except httpx.HTTPError:
        log.warning("Could not reach Telegram to send an alert", exc_info=True)
        return False


async def raise_alert(check: str, state: str, message: str) -> Alert:
    """Record, log and send one alert. state is "down" when a check starts failing, "up" when it recovers."""
    (log.error if state == "down" else log.info)("[%s] %s: %s", check, state.upper(), message)
    icon = "🔴" if state == "down" else "🟢"
    # Named, so alerts from two instances running side by side are never confused.
    delivered = await send_telegram(f"{icon} {settings.INSTANCE_LABEL}: {message}")
    async with AsyncSessionLocal() as db:
        row = Alert(check=check, state=state, message=message, delivered=delivered)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row
