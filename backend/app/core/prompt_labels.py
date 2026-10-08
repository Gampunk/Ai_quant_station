"""
Labels for the autopilot's strategy prompts, and how well a prompt fits the market.

Each prompt in prompt_list.txt gets explicit labels in prompt_labels.json: its
styles, the market it suits (trend or range), sessions, volatility, timeframes and
direction. The backend matches them against its own signals (core/market_signals)
to shortlist the prompts that fit the market right now.

The labels used to be guessed from keywords at runtime. They are now drafted
(scripts/draft_prompt_labels.py, by keyword and optionally by AI), reviewed by a
person in a spreadsheet, and stored. A prompt without stored labels, such as a
personal prompt added on the page, still gets keyword-guessed labels.
"""
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

log = logging.getLogger(__name__)

LABELS_FILE = Path(__file__).resolve().parents[2] / "prompt_labels.json"

VOCAB = {
    "styles": ("trend_following", "mean_reversion", "breakout", "range", "momentum", "scalping", "general"),
    "market": ("trend", "range", "any"),
    "sessions": ("asia", "london", "overlap", "new_york", "after_hours", "any"),
    "volatility": ("low", "normal", "high", "any"),
    "timeframes": ("1m", "5m", "15m", "30m", "1h", "4h", "1d"),
    "direction": ("buy", "sell", "both"),
}
LIST_FIELDS = ("styles", "sessions", "volatility", "timeframes")


def validate(labels: Dict[str, Any]) -> List[str]:
    """Problems with one prompt's labels; empty when they are fine."""
    problems = []
    for field in LIST_FIELDS:
        values = labels.get(field)
        if not isinstance(values, list) or not values and field != "timeframes":
            problems.append(f"{field} must be a non-empty list")
            continue
        bad = [v for v in values if v not in VOCAB[field]]
        if bad:
            problems.append(f"{field}: unknown {bad}")
    for field in ("market", "direction"):
        if labels.get(field) not in VOCAB[field]:
            problems.append(f"{field} must be one of {VOCAB[field]}")
    return problems


def load(path: Path = LABELS_FILE) -> Dict[str, Dict[str, Any]]:
    """Stored labels by prompt number (as a string). Invalid entries are dropped, with a warning."""
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.exception("Could not read %s; prompts fall back to keyword labels", path)
        return {}
    good = {}
    for key, labels in (raw.get("prompts") or {}).items():
        problems = validate(labels)
        if problems:
            log.warning("Prompt %s labels ignored: %s", key, "; ".join(problems))
            continue
        good[str(key)] = labels
    return good


_SESSION_WORDS = {
    "asia": ("asia", "asian", "tokyo", "sydney"),
    "london": ("london", "europe", "european", "frankfurt"),
    "new_york": ("new york", "ny session", "us session", "nyse", "american"),
    "overlap": ("overlap",),
}


def draft_by_keyword(text: str) -> Dict[str, Any]:
    """A first guess at a prompt's labels from its wording. Meant to be reviewed."""
    t = (text or "").lower()
    styles = set()
    for style, words in (
        ("breakout", ("breakout", "break out", "range break", "breaks above", "breaks below")),
        ("trend_following", ("trend", "ema", "moving average", "higher high", "lower low", "pullback")),
        ("mean_reversion", ("mean reversion", "overbought", "oversold", "rsi", "bollinger", "reversal", "divergence")),
        ("scalping", ("scalp", "m1", "m5", "1 minute", "5 minute")),
        ("momentum", ("momentum", "impulse", "strong candle", "volume spike")),
        ("range", ("range", "support", "resistance", "sideways", "consolidation")),
    ):
        if any(w in t for w in words):
            styles.add(style)
    styles = sorted(styles) or ["general"]

    trendish = {"trend_following", "momentum", "breakout"} & set(styles)
    rangish = {"mean_reversion", "range"} & set(styles)
    market = "trend" if trendish and not rangish else "range" if rangish and not trendish else "any"

    sessions = sorted(s for s, words in _SESSION_WORDS.items() if any(w in t for w in words)) or ["any"]

    if "breakout" in styles or "momentum" in styles:
        volatility = ["normal", "high"]
    elif market == "range":
        volatility = ["low", "normal"]
    else:
        volatility = ["any"]

    timeframes = []
    for label, pattern in (("1m", r"\b(?:m1|1m)\b"), ("5m", r"\b(?:m5|5m)\b"), ("15m", r"\b(?:m15|15m)\b"),
                           ("30m", r"\b(?:m30|30m)\b"), ("1h", r"\b(?:h1|1h|hourly)\b"),
                           ("4h", r"\b(?:h4|4h)\b"), ("1d", r"\b(?:d1|1d|daily)\b")):
        if re.search(pattern, t):
            timeframes.append(label)

    buy = any(w in t for w in ("bullish", "long", "buy"))
    sell = any(w in t for w in ("bearish", "short", "sell"))
    direction = "buy" if buy and not sell else "sell" if sell and not buy else "both"

    return {"styles": styles, "market": market, "sessions": sessions, "volatility": volatility,
            "timeframes": timeframes, "direction": direction, "reviewed": False}


def fit(labels: Dict[str, Any], signals: Dict[str, Any]) -> Tuple[float, List[str]]:
    """Points for how well a prompt's labels match the measured market, with the reasons."""
    if not signals.get("ok"):
        return 0.0, []
    score, reasons = 0.0, []
    regime = signals["regime"]
    trend = regime.get("trend")

    market = labels.get("market", "any")
    if market != "any":
        trending = trend in ("bullish", "bearish")
        if (market == "trend") == trending:
            score += 10
            reasons.append(f"made for a {market} market, and the market is {'trending' if trending else 'ranging'}")
        else:
            score -= 10
            reasons.append(f"made for a {market} market, but the market is {'trending' if trending else 'ranging'}")

    session = signals.get("session")
    sessions = labels.get("sessions") or ["any"]
    if "any" not in sessions:
        if session in sessions:
            score += 8
            reasons.append(f"suits the {session} session")
        else:
            score -= 8
            reasons.append(f"meant for {', '.join(sessions)}, not {session}")

    vol = signals["volatility"]["label"]
    vols = labels.get("volatility") or ["any"]
    if "any" not in vols and vol in VOCAB["volatility"]:
        if vol in vols:
            score += 6
            reasons.append(f"suits {vol} volatility")
        else:
            score -= 6
            reasons.append(f"wants {', '.join(vols)} volatility, now {vol}")

    bias = regime.get("direction_bias")
    direction = labels.get("direction", "both")
    if direction != "both" and bias in ("BUY", "SELL"):
        if direction.upper() == bias:
            score += 5
            reasons.append(f"{direction} strategy with a {bias} bias")
        else:
            score -= 10
            reasons.append(f"{direction} strategy against a {bias} bias")

    if signals["volume"]["label"] == "high" and {"breakout", "momentum"} & set(labels.get("styles") or []):
        score += 4
        reasons.append("high volume backs a breakout or momentum idea")
    return score, reasons
