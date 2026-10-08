"""
Market signals the backend measures itself, for the autopilot's market brief.

The AI used to receive 200-300 raw candles every cycle and write code to measure
the market. Now the backend measures it here, the same way every time, and the AI
gets a short brief: these signals plus the last 30 candles. Every value keeps the
numbers it came from ("basis"), and the whole result is stored with the cycle, so
any decision can be traced back to what the market looked like.

Everything here is a pure function of the candles and the time: no I/O, no clock.
Candle times are UTC epoch seconds (the connector client converts broker time).
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
import ta

# Trading sessions by UTC hour. Fixed hours: they ignore summer time, which moves
# London and New York by an hour for part of the year. Good enough to tell the
# sessions apart; documented so nobody mistakes them for exchange hours.
SESSIONS = (
    ("asia", 0, 7),
    ("london", 7, 12),
    ("overlap", 12, 16),     # London and New York both open
    ("new_york", 16, 21),
    ("after_hours", 21, 24),
)

RECENT_CANDLES = 30


def session_at(when: datetime) -> str:
    hour = when.astimezone(timezone.utc).hour
    for name, start, end in SESSIONS:
        if start <= hour < end:
            return name
    return "after_hours"


def _frame(candles: List[Dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(candles).copy()
    for col in ("time", "open", "high", "low", "close", "volume", "tick_volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "volume" not in df.columns and "tick_volume" in df.columns:
        df["volume"] = df["tick_volume"]
    return df.dropna(subset=["time", "open", "high", "low", "close"]).reset_index(drop=True)


def volume_signal(df: pd.DataFrame) -> Dict[str, Any]:
    """The last candle's tick volume against the median of the 96 before it (a day of 15-minute candles)."""
    if "volume" not in df.columns or df["volume"].isna().all() or len(df) < 20:
        return {"label": "unknown", "ratio": None, "basis": "no tick volume"}
    last = float(df["volume"].iloc[-1])
    base = df["volume"].iloc[-97:-1].median()
    if not base or pd.isna(base):
        return {"label": "unknown", "ratio": None, "basis": "no volume baseline"}
    ratio = last / float(base)
    label = "high" if ratio >= 1.5 else "low" if ratio <= 0.7 else "normal"
    return {"label": label, "ratio": round(ratio, 2),
            "basis": f"last {last:.0f} vs median {float(base):.0f} of previous {min(96, len(df) - 1)} candles"}


def swing_levels(df: pd.DataFrame, count: int = 3, wing: int = 2) -> Dict[str, List[float]]:
    """The most recent swing highs and lows: a candle higher (lower) than `wing` candles on each side."""
    highs, lows = [], []
    h, l = df["high"].tolist(), df["low"].tolist()
    for i in range(len(df) - wing - 1, wing - 1, -1):
        window_h = h[i - wing:i + wing + 1]
        window_l = l[i - wing:i + wing + 1]
        if len(highs) < count and h[i] == max(window_h):
            highs.append(round(h[i], 5))
        if len(lows) < count and l[i] == min(window_l):
            lows.append(round(l[i], 5))
        if len(highs) >= count and len(lows) >= count:
            break
    return {"swing_highs": highs, "swing_lows": lows}


def round_step(price: float) -> float:
    """The round-number step traders watch at this price: 10 for gold, 1 for mid prices, 0.01 for forex."""
    if price >= 1000:
        return 10.0
    if price >= 50:
        return 1.0
    if price >= 5:
        return 0.1
    return 0.01


def day_levels(df: pd.DataFrame) -> Dict[str, Optional[float]]:
    """Today's (UTC) high and low so far, and the previous UTC day's high and low."""
    days = pd.to_datetime(df["time"], unit="s", utc=True).dt.date
    today = days.iloc[-1]
    today_rows = df[days == today]
    before = df[days < today]
    out = {"today_high": round(float(today_rows["high"].max()), 5),
           "today_low": round(float(today_rows["low"].min()), 5),
           "prev_day_high": None, "prev_day_low": None}
    if len(before):
        prev_day = days[days < today].iloc[-1]
        prev_rows = df[days == prev_day]
        out["prev_day_high"] = round(float(prev_rows["high"].max()), 5)
        out["prev_day_low"] = round(float(prev_rows["low"].min()), 5)
    return out


def compute_signals(candles: List[Dict[str, Any]], regime: Dict[str, Any], now: datetime,
                    timeframe: str = "15m") -> Dict[str, Any]:
    """Everything the market brief says about the market, from `candles` and the regime classifier.

    `regime` is the output of the autopilot's regime classifier for the same candles,
    so trend, volatility and direction stay defined in one place.
    """
    df = _frame(candles)
    if len(df) < 30:
        return {"ok": False, "reason": f"only {len(df)} candles", "session": session_at(now)}

    close, high, low = df["close"], df["high"], df["low"]
    price = float(close.iloc[-1])
    atr_s = ta.volatility.average_true_range(high, low, close, window=14)
    atr = float(atr_s.iloc[-1])
    rsi = float(ta.momentum.rsi(close, window=14).iloc[-1])
    ema20 = float(ta.trend.ema_indicator(close, window=20).iloc[-1])
    ema50 = float(ta.trend.ema_indicator(close, window=50 if len(df) >= 50 else 30).iloc[-1])
    last4 = close.iloc[-5:].diff().dropna()
    up_moves = int((last4 > 0).sum())

    step = round_step(price)
    below = (price // step) * step
    levels = {**swing_levels(df), **day_levels(df),
              "round_below": round(below, 5), "round_above": round(below + step, 5)}

    recent = df.iloc[-RECENT_CANDLES:]
    return {
        "ok": True,
        "timeframe": timeframe,
        "as_of_utc": datetime.fromtimestamp(int(df["time"].iloc[-1]), timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "price": round(price, 5),
        "session": session_at(now),
        "regime": {k: regime.get(k) for k in ("regime", "trend", "volatility", "direction_bias", "confidence",
                                              "atr_ratio", "bb_width_pct", "ema_slope_pct", "adx")},
        "volatility": {"label": regime.get("volatility", "unknown"), "atr": round(atr, 5),
                       "atr_ratio": regime.get("atr_ratio"),
                       "basis": "ATR(14) against its 50-candle mean, and Bollinger width"},
        "volume": volume_signal(df),
        "momentum": {"rsi14": round(rsi, 1), "above_ema20": price > ema20, "above_ema50": price > ema50,
                     "ema20": round(ema20, 5), "ema50": round(ema50, 5),
                     "last_4_closes_up": up_moves},
        "levels": levels,
        "recent_candles": [
            [datetime.fromtimestamp(int(r.time), timezone.utc).strftime("%m-%d %H:%M"),
             round(float(r.open), 5), round(float(r.high), 5), round(float(r.low), 5), round(float(r.close), 5)]
            for r in recent.itertuples()
        ],
    }


def brief_text(symbol: str, s: Dict[str, Any]) -> str:
    """The signals as the short text the AI reads."""
    r, v, m, lv = s["regime"], s["volume"], s["momentum"], s["levels"]
    candles = "\n".join(f"{t}  O {o}  H {h}  L {l}  C {c}" for t, o, h, l, c in s["recent_candles"])
    return f"""MARKET BRIEF: {symbol}, {s['timeframe']} candles, as of {s['as_of_utc']} UTC
Price: {s['price']}
Session: {s['session']}
Regime: {r['regime']} (trend {r['trend']}, bias {r['direction_bias']}, confidence {r['confidence']}%)
Volatility: {s['volatility']['label']} (ATR14 {s['volatility']['atr']}, ratio to its average {r.get('atr_ratio')})
Volume: {v['label']} (ratio {v['ratio']}; {v['basis']})
Momentum: RSI14 {m['rsi14']}; price {'above' if m['above_ema20'] else 'below'} EMA20 {m['ema20']}, \
{'above' if m['above_ema50'] else 'below'} EMA50 {m['ema50']}; {m['last_4_closes_up']} of the last 4 closes up
Levels: today high {lv['today_high']} / low {lv['today_low']}; previous day high {lv['prev_day_high']} / low {lv['prev_day_low']}
Swing highs (recent first): {lv['swing_highs']}
Swing lows (recent first): {lv['swing_lows']}
Round numbers: {lv['round_below']} below, {lv['round_above']} above

Last {len(s['recent_candles'])} candles (UTC):
{candles}"""
