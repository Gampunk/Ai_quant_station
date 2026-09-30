"""
The broker's clock, and turning its times into real UTC.

MT5 stamps positions, deals and candles in the broker's server time, encoded as
if it were UTC. Everything in the backend works in real UTC, so the connector
client converts at the boundary, in one place, using the offset kept here.

The offset comes from the connector's /clock reading while prices are live,
which follows summer time by itself. Until a reading arrives, or when the
market is closed, the last good reading is kept. Before any reading this
process has seen, MT5_BROKER_UTC_OFFSET is used.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import settings

log = logging.getLogger("broker_clock")

REFRESH_SECONDS = 600
# A reading needs prices to have moved since the one before, so until the first
# offset is detected, read often.
FIRST_READING_SECONDS = 30
FORMAT = "%Y-%m-%d %H:%M:%S"


class BrokerClock:
    def __init__(self):
        self.detected: Optional[float] = None
        self.detected_at: Optional[float] = None
        self.last_checked: float = 0.0
        self._warned_mismatch_for: Optional[float] = None

    @property
    def offset_hours(self) -> float:
        return self.detected if self.detected is not None else float(settings.MT5_BROKER_UTC_OFFSET)

    @property
    def source(self) -> str:
        return "detected" if self.detected is not None else "MT5_BROKER_UTC_OFFSET"

    def due(self) -> bool:
        wait = REFRESH_SECONDS if self.detected is not None else FIRST_READING_SECONDS
        return time.time() - self.last_checked >= wait

    def observe(self, reading: dict) -> None:
        """Take a /clock reading. Only a reliable offset changes anything."""
        self.last_checked = time.time()
        offset = reading.get("offset_hours")
        if offset is None:
            return
        offset = float(offset)
        if self.detected is not None and offset != self.detected:
            log.warning("Broker clock moved from UTC%+g to UTC%+g, probably a summer time change",
                        self.detected, offset)
        elif self.detected is None:
            log.info("Broker clock detected: UTC%+g", offset)
        self.detected, self.detected_at = offset, time.time()
        configured = float(settings.MT5_BROKER_UTC_OFFSET)
        if offset != configured and self._warned_mismatch_for != offset:
            log.warning("MT5_BROKER_UTC_OFFSET is %+g but the broker's clock reads UTC%+g. "
                        "Using the detected value; update the setting for restarts at weekends.",
                        configured, offset)
            self._warned_mismatch_for = offset

    def status(self) -> dict:
        return {"offset_hours": self.offset_hours, "source": self.source,
                "detected_at": datetime.fromtimestamp(self.detected_at, timezone.utc).isoformat()
                if self.detected_at else None,
                "setting": float(settings.MT5_BROKER_UTC_OFFSET)}

    # ── Conversions ────────────────────────────────────────────────────────
    @property
    def _shift(self) -> int:
        return int(round(self.offset_hours * 3600))

    def epoch_to_utc(self, server_ts):
        return server_ts - self._shift if isinstance(server_ts, (int, float)) else server_ts

    def text_to_utc(self, server_text):
        """'YYYY-MM-DD HH:MM:SS' in server time to the same format in UTC."""
        if not server_text:
            return server_text
        try:
            parsed = datetime.strptime(server_text, FORMAT)
        except (TypeError, ValueError):
            return server_text
        return (parsed - timedelta(seconds=self._shift)).strftime(FORMAT)

    def iso_utc_to_server(self, utc_text: str) -> str:
        """An ISO time in UTC (naive means UTC) to naive server time, for MT5 range queries."""
        if not utc_text:
            return utc_text
        parsed = datetime.fromisoformat(utc_text)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return (parsed + timedelta(seconds=self._shift)).isoformat()


broker_clock = BrokerClock()
