"""
Part 2: the backend measures the market, shortlists prompts by their labels, and
one AI call picks a prompt and gives the setup.

Negative controls:
- In core/market_signals.py session_at, shift every hour by +1. The session tests fail.
- In core/prompt_labels.py fit, drop the market (trend/range) points. The fit test fails.
- In autopilot._parse_brief_decision, accept any strategy_id. The off-shortlist test fails.
"""
import csv
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core import market_signals as ms
from app.core import prompt_labels as pl

BACKEND = Path(__file__).resolve().parents[1]


def _candles(n=300, start_price=2000.0, step=0.5, start=datetime(2026, 10, 6, tzinfo=timezone.utc)):
    """n 15-minute candles, a gentle up trend with small wiggles, ending at a known time."""
    out, price = [], start_price
    t0 = int(start.timestamp())
    for i in range(n):
        wiggle = 1.5 if i % 4 == 0 else -1.0
        o = price
        c = price + step + (wiggle if i % 7 else -2.0)
        out.append({"time": t0 + i * 900, "open": o, "high": max(o, c) + 1.0, "low": min(o, c) - 1.0,
                    "close": c, "volume": 100 + (i % 10)})
        price = c
    return out


REGIME = {"regime": "bullish_trend", "trend": "bullish", "volatility": "normal", "direction_bias": "BUY",
          "confidence": 70, "atr_ratio": 1.0}


# ── Signals ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("hour, session", [(0, "asia"), (6, "asia"), (7, "london"), (11, "london"),
                                           (12, "overlap"), (15, "overlap"), (16, "new_york"),
                                           (20, "new_york"), (21, "after_hours"), (23, "after_hours")])
def test_sessions_by_utc_hour(hour, session):
    assert ms.session_at(datetime(2026, 10, 7, hour, 30, tzinfo=timezone.utc)) == session


def test_volume_is_compared_with_the_day_before():
    df = ms._frame(_candles(120))
    df.loc[df.index[-1], "volume"] = 400
    assert ms.volume_signal(df)["label"] == "high"
    df.loc[df.index[-1], "volume"] = 40
    assert ms.volume_signal(df)["label"] == "low"


def test_swing_levels_are_local_extremes():
    candles = [{"time": i, "open": 1, "high": h, "low": h - 5, "close": 1}
               for i, h in enumerate([10, 11, 15, 11, 10, 12, 18, 12, 11, 10])]
    levels = ms.swing_levels(ms._frame(candles), count=2)
    assert levels["swing_highs"] == [18, 15]


def test_round_numbers_fit_the_price():
    assert ms.round_step(4321.5) == 10.0 and ms.round_step(1.0832) == 0.01


def test_day_levels_split_at_utc_midnight():
    candles = _candles(150)          # starts 6 Oct 00:00 UTC, 96 candles a day
    lv = ms.day_levels(ms._frame(candles))
    first_day = candles[:96]
    assert lv["prev_day_high"] == round(max(c["high"] for c in first_day), 5)


def test_the_brief_is_short_and_complete():
    s = ms.compute_signals(_candles(), REGIME, datetime(2026, 10, 9, 13, 0, tzinfo=timezone.utc))
    assert s["ok"] and s["session"] == "overlap" and len(s["recent_candles"]) == ms.RECENT_CANDLES
    text = ms.brief_text("XAUUSD", s)
    for part in ("Session overlap", "Regime bullish_trend", "Volume ", "Swing highs", f"Last {ms.RECENT_CANDLES} candles"):
        assert part in text
    assert len(text) < 1500, f"the market part of the brief is {len(text)} characters"
    raw = "\n".join(json.dumps(c) for c in _candles())
    assert len(text) < len(raw) / 5, "the brief should be far smaller than the raw candles it replaces"


def test_too_few_candles_is_reported_not_guessed():
    s = ms.compute_signals(_candles(10), REGIME, datetime.now(timezone.utc))
    assert s["ok"] is False and "10 candles" in s["reason"]


# ── Labels ──────────────────────────────────────────────────────────────────
def test_every_stored_label_is_valid_and_covers_all_prompts():
    raw = json.loads(pl.LABELS_FILE.read_text())["prompts"]
    numbers = set(re.findall(r"^PROMPT\s*#(\d+)", (BACKEND / "prompt_list.txt").read_text(), re.M))
    assert set(raw) == numbers
    assert not {n: pl.validate(v) for n, v in raw.items() if pl.validate(v)}


def test_invalid_labels_are_dropped_with_a_warning(tmp_path, caplog):
    path = tmp_path / "labels.json"
    path.write_text(json.dumps({"prompts": {
        "1": {"styles": ["range"], "market": "range", "sessions": ["any"], "volatility": ["low"],
              "timeframes": [], "direction": "both"},
        "2": {"styles": ["astrology"], "market": "range", "sessions": ["any"], "volatility": ["low"],
              "timeframes": [], "direction": "both"}}}))
    assert set(pl.load(path)) == {"1"}


def _signals(trend="bullish", session="london", vol="normal", bias="BUY", volume="normal"):
    return {"ok": True, "session": session,
            "regime": {"trend": trend, "direction_bias": bias},
            "volatility": {"label": vol}, "volume": {"label": volume}}


TREND = {"styles": ["trend_following"], "market": "trend", "sessions": ["any"], "volatility": ["any"],
         "timeframes": [], "direction": "both"}
RANGE = {**TREND, "styles": ["range"], "market": "range"}


def test_a_trend_prompt_fits_a_trend_and_a_range_prompt_fits_a_range():
    assert pl.fit(TREND, _signals("bullish"))[0] > pl.fit(RANGE, _signals("bullish"))[0]
    assert pl.fit(RANGE, _signals("range", bias="neutral"))[0] > pl.fit(TREND, _signals("range", bias="neutral"))[0]


def test_session_and_direction_count():
    london_only = {**TREND, "sessions": ["london"]}
    assert pl.fit(london_only, _signals(session="london"))[0] > pl.fit(london_only, _signals(session="asia"))[0]
    sell_only = {**TREND, "direction": "sell"}
    score, reasons = pl.fit(sell_only, _signals(bias="BUY"))
    assert any("against a BUY bias" in r for r in reasons)


def test_reviewed_spreadsheet_round_trip_and_refusal(tmp_path):
    good = tmp_path / "good.csv"
    bad = tmp_path / "bad.csv"
    header = ["number", "styles", "market", "sessions", "volatility", "timeframes", "direction", "reviewed", "prompt"]
    rows = [["1", "range;mean_reversion", "range", "london;overlap", "low;normal", "1h", "buy", "yes", "x"]]
    for path, data in ((good, rows), (bad, [rows[0][:2] + ["sideways"] + rows[0][3:]])):
        with open(path, "w", newline="") as f:
            csv.writer(f).writerows([header] + data)
    script = BACKEND / "scripts" / "draft_prompt_labels.py"
    env_target = tmp_path / "labels.json"
    code = (f"import sys; sys.argv=['x','import','{{}}']; import runpy; "
            f"from app.core import prompt_labels as p; from pathlib import Path; p.LABELS_FILE=Path('{env_target}'); "
            f"runpy.run_path('{script}', run_name='__main__')")
    ok = subprocess.run([sys.executable, "-c", code.format(good)], cwd=BACKEND, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    stored = json.loads(env_target.read_text())["prompts"]["1"]
    assert stored["sessions"] == ["london", "overlap"] and stored["reviewed"] is True
    refused = subprocess.run([sys.executable, "-c", code.format(bad)], cwd=BACKEND, capture_output=True, text=True)
    assert refused.returncode != 0 and "market must be one of" in refused.stderr


# ── The decision parser ─────────────────────────────────────────────────────
def test_a_good_trade_answer_is_accepted():
    from app.api.autopilot import _parse_brief_decision
    reply = 'Here: {"decision": "TRADE_SETUP", "strategy_id": "12", "direction": "BUY", "order_type": "market", ' \
            '"stop_loss": 1990, "take_profit": 2020, "confidence": 70, "reasoning": "support held"}'
    d = _parse_brief_decision(reply, ["5", "12", "40"], 2000.0)
    assert d["kind"] == "trade" and d["strategy_id"] == "12"
    assert d["setup"]["entry_price"] == 2000.0 and d["setup"]["stop_loss"] == 1990.0


def test_a_strategy_off_the_shortlist_is_refused():
    from app.api.autopilot import _parse_brief_decision
    reply = '{"decision": "TRADE_SETUP", "strategy_id": "99", "direction": "BUY", "stop_loss": 1990, "take_profit": 2020}'
    assert _parse_brief_decision(reply, ["5", "12"], 2000.0)["kind"] == "invalid"


def test_no_setup_and_garbage():
    from app.api.autopilot import _parse_brief_decision
    assert _parse_brief_decision('{"decision": "NO_SETUP", "strategy_id": "5", "reasoning": "no"}',
                                 ["5"], 1.0)["kind"] == "no_setup"
    assert _parse_brief_decision("I think you should buy", ["5"], 1.0)["kind"] == "invalid"
    assert _parse_brief_decision('{"decision": "TRADE_SETUP", "strategy_id": "5", "direction": "BUY"}',
                                 ["5"], 1.0)["kind"] == "invalid"


# ── Answers that do not follow the format exactly still work ────────────────
def _parse(reply, ids=("5", "12", "40"), price=2000.0):
    from app.api.autopilot import _parse_brief_decision
    return _parse_brief_decision(reply, list(ids), price)


def test_the_requested_line_format():
    d = _parse("DECISION: TRADE_SETUP\nSTRATEGY: 12\nDIRECTION: BUY\nORDER: market\nENTRY: 2000\n"
               "STOP: 1990.5\nTARGET: 2020\nCONFIDENCE: 70\nREASON: support held at 1991")
    assert d["kind"] == "trade" and d["strategy_id"] == "12"
    assert d["setup"]["stop_loss"] == 1990.5 and d["setup"]["take_profit"] == 2020.0
    assert d["setup"]["reasoning"] == "support held at 1991"


def test_markdown_and_extra_words_are_tolerated():
    d = _parse("Here is my answer:\n**DECISION:** TRADE_SETUP\n**Strategy:** #40\n- Direction: Short\n"
               "Stop: 2012.3 (above the swing high)\nTarget: 1975\nConfidence: 65%")
    assert d["kind"] == "trade" and d["strategy_id"] == "40" and d["setup"]["direction"] == "SELL"
    assert d["setup"]["stop_loss"] == 2012.3 and d["setup"]["confidence"] == 65


def test_a_json_answer_cut_off_midway_is_still_read():
    d = _parse('```json\n{"decision": "TRADE_SETUP", "strategy_id": "5", "direction": "BUY", '
               '"order_type": "market", "stop_loss": 1992.0, "take_profit": 2015.0, "reasoning": "breakout ab')
    assert d["kind"] == "trade" and d["strategy_id"] == "5" and d["setup"]["stop_loss"] == 1992.0


def test_plain_text_no_setup():
    d = _parse("After reviewing all three strategies, none of their conditions are met. NO SETUP.")
    assert d["kind"] == "no_setup" and "none of their conditions" in d["reasoning"]


def test_a_trade_without_a_stop_is_refused():
    d = _parse("DECISION: TRADE_SETUP\nSTRATEGY: 12\nDIRECTION: BUY\nTARGET: 2020")
    assert d["kind"] == "invalid" and "stop" in d["reason"]
