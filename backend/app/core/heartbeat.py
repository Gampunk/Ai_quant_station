"""
The heartbeat: checks every minute that the system is alive, and alerts once when
something breaks and once when it recovers. Also records each day's starting
equity at 00:00 UTC.

Checks:
- connector: the connector answers and its MT5 terminal is connected.
  Alerts after 2 failed checks in a row, so one dropped request is not an alarm.
- prices: during market hours, prices are moving. Alerts after 10 quiet minutes,
  which rides over the normal quiet spells.
- autopilot:<user id>: a switched-on autopilot passed through its loop within
  twice its interval (plus two minutes), and its loop has not died.

A process cannot report its own death; a dead backend is caught from outside.
That outside watcher is upstream's monitor.py, adopted in the step 14 merge.
"""
import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .alerts import raise_alert
from .broker_clock import broker_clock
from .mt5_connector import ConnectorError, connector_client
from .risk import record_day_start

log = logging.getLogger("heartbeat")

# check -> (ok, message when failing, message on recovery, failures in a row before alerting)
Result = Tuple[Optional[bool], str, str, int]


class Heartbeat:
    def __init__(self):
        self.failures: Dict[str, int] = {}
        self.down: set[str] = set()

    async def _connector_and_prices(self) -> Dict[str, Result]:
        results: Dict[str, Result] = {}
        if not connector_client.configured:
            return results
        try:
            health = await connector_client.health()
            ok = bool(health.get("mt5_connected"))
            failing = "The MT5 connector answers, but its terminal is not connected"
        except ConnectorError as exc:
            ok, failing = False, f"The MT5 connector is unreachable: {exc.detail}"
        results["connector"] = (ok, failing, "The MT5 connector and terminal are back", 2)

        from ..api.autopilot import _is_market_open
        if ok and await _is_market_open():
            try:
                reading = await connector_client.clock()
                broker_clock.observe(reading)
                live: Optional[bool] = bool(reading.get("live"))
            except ConnectorError:
                live = None  # the connector check above reports this
            results["prices"] = (live, "No price movement for 10 minutes during market hours",
                                 "Prices are moving again", 10)
        return results

    async def _autopilots(self, now: datetime) -> Dict[str, Result]:
        from ..api.autopilot import _settings_row, _user_states
        results: Dict[str, Result] = {}
        for user_id, state in list(_user_states.items()):
            key = f"autopilot:{user_id}"
            if not state.get("enabled"):
                if key in self.down:
                    results[key] = (True, "", f"The autopilot of user #{user_id} is switched off", 1)
                continue
            task = state.get("task")
            if task is None or task.done():
                reason = state.get("stats", {}).get("stopped_reason") or "its loop is not running"
                results[key] = (False, f"The autopilot of user #{user_id} is switched on but stopped: {reason}",
                                f"The autopilot of user #{user_id} is running again", 1)
                continue
            settings_row = await _settings_row(user_id)
            interval = (settings_row.interval_seconds if settings_row and settings_row.interval_seconds else 300)
            limit = 2 * interval + 120
            beat = state.get("last_beat")
            quiet = (now - beat).total_seconds() if beat else 0
            results[key] = (quiet <= limit,
                            f"The autopilot of user #{user_id} has not cycled for {quiet / 60:.0f} minutes "
                            f"(interval {interval // 60} min)",
                            f"The autopilot of user #{user_id} is cycling again", 1)
        return results

    async def check_once(self, now: Optional[datetime] = None) -> list:
        """Run every check once and send the alerts that are due. Returns them."""
        now = now or datetime.now(timezone.utc)
        results = {**await self._connector_and_prices(), **await self._autopilots(now)}
        alerts = []
        for check, (ok, failing, recovered, needed) in results.items():
            if ok is None:
                continue
            if ok:
                self.failures[check] = 0
                if check in self.down:
                    self.down.discard(check)
                    alerts.append(await raise_alert(check, "up", recovered))
                continue
            self.failures[check] = self.failures.get(check, 0) + 1
            if self.failures[check] >= needed and check not in self.down:
                self.down.add(check)
                alerts.append(await raise_alert(check, "down", failing))
        return alerts


heartbeat = Heartbeat()
scheduler = AsyncIOScheduler(timezone="UTC")


async def _beat():
    try:
        await heartbeat.check_once()
    except Exception:
        log.exception("Heartbeat check failed")


def start_heartbeat():
    if scheduler.running:
        return
    scheduler.add_job(_beat, "interval", seconds=60, id="heartbeat", max_instances=1, coalesce=True)
    # A few seconds after midnight, so the broker's day has rolled over.
    scheduler.add_job(record_day_start, "cron", hour=0, minute=0, second=5, id="day_start",
                      max_instances=1, coalesce=True, misfire_grace_time=600)
    scheduler.start()
    log.info("Heartbeat started: checks every 60 s, starting equity at 00:00 UTC")


def shutdown_heartbeat():
    if scheduler.running:
        scheduler.shutdown(wait=False)
