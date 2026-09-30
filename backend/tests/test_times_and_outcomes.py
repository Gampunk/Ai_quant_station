"""
Broker time to UTC, how trades ended, and candle times in the AI sandbox. No broker.

Negative controls:
- in app/core/trade_outcome.py, go back to `if "sl" in comment` guessing:
  test_a_comment_mentioning_sl_is_not_a_stop fails
- in app/core/broker_clock.py, make epoch_to_utc return server_ts unchanged:
  the conversion tests fail
- in app/api/execute.py, drop unit='s': test_sandbox_candles_are_in_this_century fails
"""
import logging
import pytest

from app.api.execute import _execute_sandbox_sync
from app.core import broker_clock as clock_module
from app.core.broker_clock import BrokerClock
from app.core.config import settings
from app.core.trade_outcome import close_result


# ── How a trade ended ────────────────────────────────────────────────────────
@pytest.mark.parametrize("deal, expected", [
    ({"reason": "sl", "profit": -12.0}, "SL_HIT"),
    ({"reason": "sl", "profit": 4.0}, "SL_HIT"),      # a trailed stop can close in profit
    ({"reason": "tp", "profit": 20.0}, "TP_HIT"),
    ({"reason": "stop_out", "profit": -900.0}, "STOP_OUT"),
    ({"reason": "expert", "profit": 3.0}, "PROFIT"),
    ({"reason": "client", "profit": -3.0}, "LOSS"),
    (None, "OPEN"),
])
def test_the_brokers_reason_decides(deal, expected):
    assert close_result(deal) == expected


def test_a_comment_mentioning_sl_is_not_a_stop():
    """The old guess read any comment containing "sl" or "tp" as a stop or target hit."""
    for comment in ("[AUTOPILOT] slow trend", "closed: tp not reached", "Islamabad session"):
        assert close_result({"reason": "expert", "comment": comment, "profit": -1.0}) == "LOSS"
        assert close_result({"comment": comment, "profit": 1.0}) == "PROFIT"


def test_older_connectors_fall_back_to_mt5s_own_comment():
    assert close_result({"comment": "[sl 2650.10]", "profit": -5.0}) == "SL_HIT"
    assert close_result({"reason": "unknown", "comment": "[tp 2680.00]", "profit": 9.0}) == "TP_HIT"


# ── Broker clock ─────────────────────────────────────────────────────────────
@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(settings, "MT5_BROKER_UTC_OFFSET", 0)
    return BrokerClock()


def test_until_detected_the_setting_is_used(clock, monkeypatch):
    monkeypatch.setattr(settings, "MT5_BROKER_UTC_OFFSET", 2)
    assert clock.offset_hours == 2 and clock.source == "MT5_BROKER_UTC_OFFSET"


def test_a_detected_offset_wins_over_the_setting(clock, caplog):
    with caplog.at_level(logging.WARNING, logger="broker_clock"):
        clock.observe({"offset_hours": 3})
    assert clock.offset_hours == 3 and clock.source == "detected"
    assert "MT5_BROKER_UTC_OFFSET is +0" in caplog.text


def test_a_closed_market_keeps_the_last_good_reading(clock):
    clock.observe({"offset_hours": 3})
    clock.observe({"offset_hours": None, "live": False})
    assert clock.offset_hours == 3


def test_a_summer_time_change_is_followed_and_logged(clock, caplog):
    clock.observe({"offset_hours": 3})
    with caplog.at_level(logging.WARNING, logger="broker_clock"):
        clock.observe({"offset_hours": 2})
    assert clock.offset_hours == 2 and "summer time" in caplog.text


def test_reads_often_until_the_first_detection(clock, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(clock_module.time, "time", lambda: now[0])
    clock.observe({"offset_hours": None})
    now[0] += clock_module.FIRST_READING_SECONDS
    assert clock.due()
    clock.observe({"offset_hours": 3})
    now[0] += clock_module.FIRST_READING_SECONDS
    assert not clock.due()


def test_server_times_become_utc(clock):
    clock.observe({"offset_hours": 3})
    assert clock.text_to_utc("2026-09-30 13:00:00") == "2026-09-30 10:00:00"
    assert clock.text_to_utc("2026-10-01 01:30:00") == "2026-09-30 22:30:00"
    assert clock.epoch_to_utc(1_790_000_000 + 3 * 3600) == 1_790_000_000


def test_utc_queries_become_server_time(clock):
    clock.observe({"offset_hours": 3})
    assert clock.iso_utc_to_server("2026-09-30T10:00:00+00:00") == "2026-09-30T13:00:00"
    assert clock.iso_utc_to_server("2026-09-30T10:00:00") == "2026-09-30T13:00:00"


def test_half_hour_offsets(clock):
    clock.observe({"offset_hours": 5.5})
    assert clock.text_to_utc("2026-09-30 15:30:00") == "2026-09-30 10:00:00"


# ── Candle times in the AI sandbox ───────────────────────────────────────────
def test_sandbox_candles_are_in_this_century():
    """Connector candles carry unix seconds, which pandas read as nanoseconds: January 1970."""
    rows = [{"time": 1_790_000_000 + 900 * i, "open": 1, "high": 2, "low": 0.5, "close": 1.5} for i in range(30)]
    result = _execute_sandbox_sync("print(df.index[0].year, df.index[1] - df.index[0])", market_data=rows)
    assert result["success"], result
    assert result["output"].strip() == "2026 0 days 00:15:00"
