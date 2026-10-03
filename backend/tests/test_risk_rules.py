"""
Every risk rule, with plain numbers and no broker.

The account and symbol match the worked example: 5,000 equity, gold at 2000,
one lot moves 100 per 1.00 of price, so a 5.00 stop on 0.10 lots risks 50 (1%).

Negative controls, in app/core/risk.py:
- make evaluate() return Evaluation(True, volume=order.get("volume")) first: most tests here fail
- in the sizing, use `budget / ev.stop_distance` without per_unit: the sizing tests fail
- change `if loss_pct >= limit` to `if loss_pct > limit + 1`: test_daily_loss_at_the_limit_refuses fails
"""
import pytest

from app.core.risk import DEFAULTS, evaluate, validate_settings

GOLD = {"symbol": "XAUUSD", "ask": 2000.20, "bid": 2000.00, "digits": 2, "point": 0.01,
        "min_stop_distance": 0.10, "trade_tick_size": 0.01, "trade_tick_value": 1.0,
        "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01, "max_volume": 1.0}
ACCOUNT = {"login": 1, "equity": 5000.0, "margin": 0.0, "margin_level": 0.0}


def run(order, *, settings=None, account=None, positions=(), start_equity=5000.0, sized=False):
    s = {**DEFAULTS, **(settings or {})}
    return evaluate({"symbol": "XAUUSD", **order}, GOLD, {**ACCOUNT, **(account or {})},
                    list(positions), s, start_equity, sized)


def buy(sl_away=5.0, **extra):
    return {"action": "BUY", "sl": round(GOLD["ask"] - sl_away, 2), **extra}


# ── Autopilot sizing ────────────────────────────────────────────────────────
def test_one_percent_with_a_five_dollar_stop_is_a_tenth_of_a_lot():
    ev = run(buy(5.0), sized=True)
    assert ev.allowed, ev.message
    assert ev.volume == 0.10
    assert ev.risk_amount == 50.0 and ev.risk_pct == 1.0


def test_a_wider_stop_halves_the_size_and_keeps_the_risk():
    ev = run(buy(10.0), sized=True)
    assert ev.volume == 0.05 and ev.risk_amount == 50.0


def test_the_ais_lot_is_ignored():
    assert run(buy(5.0, volume=3.0), sized=True).volume == 0.10


def test_size_rounds_down_never_up():
    ev = run(buy(7.0), sized=True)   # 50 / 700 = 0.0714 lots
    assert ev.volume == 0.07 and ev.risk_pct <= 1.0


def test_too_small_a_budget_is_refused_not_rounded_up():
    ev = run(buy(60.0), sized=True)  # 50 / 6000 = 0.008 lots, below the 0.01 minimum
    assert not ev.allowed and ev.code == "risk_budget_too_small"


def test_size_never_exceeds_the_connector_cap():
    ev = run(buy(0.2), sized=True)   # 50 / 20 = 2.5 lots, the cap is 1.0
    assert ev.allowed and ev.volume == 1.0


def test_the_autopilot_always_needs_a_stop_even_when_stops_are_optional():
    ev = run({"action": "BUY"}, settings={"require_stop_loss": False}, sized=True)
    assert not ev.allowed and ev.code == "no_stop_loss"


# ── Manual orders keep their size, within the per-trade cap ─────────────────
def test_manual_size_is_kept():
    ev = run(buy(5.0, volume=0.15))
    assert ev.allowed and ev.volume == 0.15 and ev.risk_pct == 1.5


def test_exactly_two_percent_is_allowed():
    assert run(buy(5.0, volume=0.20)).allowed


def test_above_two_percent_is_refused():
    ev = run(buy(5.0, volume=0.21))
    assert not ev.allowed and ev.code == "trade_risk_too_high"
    assert "2.1" in ev.message


def test_a_typo_of_ten_lots_is_refused():
    ev = run(buy(5.0, volume=10))
    assert ev.code == "trade_risk_too_high"


def test_a_stop_is_required():
    ev = run({"action": "BUY", "volume": 0.1})
    assert not ev.allowed and ev.code == "no_stop_loss"


def test_without_the_stop_rule_an_order_without_a_stop_passes_unmeasured():
    ev = run({"action": "BUY", "volume": 0.1}, settings={"require_stop_loss": False})
    assert ev.allowed and ev.risk_amount is None


# ── Stop and target placement ──────────────────────────────────────────────
@pytest.mark.parametrize("order, code", [
    ({"action": "BUY", "volume": 0.1, "sl": 2001.0}, "bad_stop_loss"),    # above a buy
    ({"action": "BUY", "volume": 0.1, "sl": 2000.15}, "bad_stop_loss"),   # 0.05 away, minimum 0.10
    ({"action": "SELL", "volume": 0.1, "sl": 1999.0}, "bad_stop_loss"),   # below a sell
    ({"action": "BUY", "volume": 0.1, "sl": 1995.0, "tp": 1999.0}, "bad_take_profit"),
    ({"action": "SELL", "volume": 0.1, "sl": 2005.0, "tp": 2001.0}, "bad_take_profit"),
])
def test_misplaced_stops_are_refused(order, code):
    ev = run(order)
    assert not ev.allowed and ev.code == code


def test_a_sell_is_measured_from_the_bid():
    ev = run({"action": "SELL", "volume": 0.1, "sl": 2005.0})
    assert ev.allowed and ev.price == 2000.00 and ev.stop_distance == 5.0


# ── Account-wide limits ─────────────────────────────────────────────────────
def test_daily_loss_below_the_limit_allows():
    assert run(buy(5.0, volume=0.1), account={"equity": 4851.0}).allowed


def test_daily_loss_at_the_limit_refuses():
    ev = run(buy(5.0, volume=0.1), account={"equity": 4850.0})
    assert not ev.allowed and ev.code == "daily_loss_limit"


def test_daily_loss_limit_of_zero_is_off():
    assert run(buy(5.0, volume=0.1), account={"equity": 2500.0}, settings={"daily_loss_pct": 0}).allowed


def test_low_margin_level_refuses():
    ev = run(buy(5.0, volume=0.1), account={"margin": 100.0, "margin_level": 150.0})
    assert not ev.allowed and ev.code == "margin_level_low"


def test_no_margin_in_use_is_not_a_low_margin_level():
    assert run(buy(5.0, volume=0.1), account={"margin": 0.0, "margin_level": 0.0}).allowed


def test_no_limit_on_open_positions_by_default():
    assert run(buy(5.0, volume=0.1), positions=[{}] * 50).allowed


def test_open_position_limit_when_set():
    ev = run(buy(5.0, volume=0.1), positions=[{}] * 3, settings={"max_open_positions": 3})
    assert not ev.allowed and ev.code == "max_open_positions"


# ── Pending orders ──────────────────────────────────────────────────────────
def test_pending_order_near_the_market_passes():
    assert run({"action": "BUY_LIMIT", "volume": 0.1, "price": 1970.0, "sl": 1965.0}).allowed


def test_pending_order_far_from_the_market_is_refused():
    ev = run({"action": "BUY_LIMIT", "volume": 0.1, "price": 1900.0, "sl": 1895.0})
    assert not ev.allowed and ev.code == "pending_too_far"


def test_pending_stop_is_measured_from_the_pending_price():
    ev = run({"action": "BUY_LIMIT", "volume": 0.1, "price": 1970.0, "sl": 1965.0})
    assert ev.stop_distance == 5.0 and ev.risk_amount == 50.0


# ── Saving settings ─────────────────────────────────────────────────────────
def test_defaults_are_valid():
    assert validate_settings(dict(DEFAULTS)) == []


@pytest.mark.parametrize("change, fragment", [
    ({"autopilot_risk_pct": 3.0}, "cannot be above max_trade_risk_pct"),
    ({"max_trade_risk_pct": 50}, "between"),
    ({"max_open_positions": -1}, "between"),
    ({"daily_loss_pct": "3"}, "must be a number"),
    ({"require_stop_loss": "yes"}, "true or false"),
])
def test_bad_settings_are_rejected(change, fragment):
    errors = validate_settings({**DEFAULTS, **change})
    assert any(fragment in e for e in errors), errors


# ── Optional ATR default stop (replaces upstream's fixed 0.2%) ──────────────
def _run_with_atr(order, atr, mult, sized=False):
    s = {**DEFAULTS, "default_stop_atr_mult": mult}
    return evaluate({"symbol": "XAUUSD", **order}, {**GOLD, "atr": atr}, ACCOUNT, [], s, 5000.0, sized)


def test_without_the_setting_a_missing_stop_is_refused():
    ev = _run_with_atr({"action": "BUY"}, atr=4.0, mult=0)
    assert not ev.allowed and ev.code == "no_stop_loss"


def test_a_missing_stop_is_set_at_the_atr_multiple_and_sized_from_it():
    ev = _run_with_atr({"action": "BUY"}, atr=4.0, mult=1.25, sized=True)   # 5.00 below the ask
    assert ev.allowed, ev.message
    assert ev.stop_filled == round(GOLD["ask"] - 5.0, 2)
    assert ev.volume == 0.10 and ev.risk_pct == 1.0


def test_a_sell_gets_its_stop_above_the_bid():
    ev = _run_with_atr({"action": "SELL"}, atr=4.0, mult=1.25, sized=True)
    assert ev.stop_filled == round(GOLD["bid"] + 5.0, 2)


def test_a_given_stop_is_never_replaced():
    ev = _run_with_atr(buy(10.0), atr=4.0, mult=1.25, sized=True)
    assert ev.stop_filled is None and ev.volume == 0.05


def test_without_an_atr_reading_nothing_is_guessed():
    ev = _run_with_atr({"action": "BUY"}, atr=None, mult=1.25)
    assert not ev.allowed and ev.code == "no_stop_loss"


def test_the_filled_stop_respects_the_brokers_minimum_distance():
    ev = _run_with_atr({"action": "BUY"}, atr=0.01, mult=1.0, sized=True)
    assert ev.stop_filled == round(GOLD["ask"] - GOLD["min_stop_distance"], 2)


def test_the_setting_has_a_range():
    assert any("default_stop_atr_mult" in e for e in validate_settings({**DEFAULTS, "default_stop_atr_mult": 50}))
    assert not validate_settings({**DEFAULTS, "default_stop_atr_mult": 1.5})
