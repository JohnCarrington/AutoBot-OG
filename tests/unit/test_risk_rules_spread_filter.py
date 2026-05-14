"""Tests for risk.rules.spread_filter."""
from __future__ import annotations

import math

from risk.constants import SPREAD_ABS_CAP_PIPS, SPREAD_ATR_MULT
from risk.rules.spread_filter import check_spread_filter
from risk.types import MarketSnapshot


def test_allows_when_spread_below_both_caps() -> None:
    market = MarketSnapshot(current_spread_pips=1.0, atr_m5_pips=20.0)
    # abs_cap=3, atr_cap=6 → cap=3, 1 < 3 → allow.
    result = check_spread_filter(market)
    assert result.allow is True
    assert result.rule == "spread_filter"


def test_rejects_when_spread_exceeds_abs_cap() -> None:
    # ATR cap is 6, abs cap is 3, spread is 3.5 → exceeds abs cap.
    market = MarketSnapshot(current_spread_pips=3.5, atr_m5_pips=20.0)
    result = check_spread_filter(market)
    assert result.allow is False
    assert "abs_cap" in result.reason


def test_rejects_when_atr_cap_is_binding() -> None:
    # ATR very tight: atr_cap = 0.3 * 5 = 1.5p. spread 2.0p exceeds atr_cap
    # though it is below abs_cap=3p.
    market = MarketSnapshot(current_spread_pips=2.0, atr_m5_pips=5.0)
    result = check_spread_filter(market)
    assert result.allow is False
    assert "atr_cap" in result.reason


def test_allows_at_exact_cap_boundary() -> None:
    # Spread exactly at the min cap (abs in this case): boundary inclusive.
    market = MarketSnapshot(
        current_spread_pips=SPREAD_ABS_CAP_PIPS, atr_m5_pips=20.0
    )
    result = check_spread_filter(market)
    assert result.allow is True


def test_atr_cap_ignored_when_atr_zero() -> None:
    # ATR=0 -> divide-by-zero-like state; rule must not block on this alone.
    # Falls back to abs cap.
    market = MarketSnapshot(current_spread_pips=2.0, atr_m5_pips=0.0)
    result = check_spread_filter(market)
    assert result.allow is True  # 2 <= 3p abs cap


def test_atr_cap_ignored_when_atr_nan() -> None:
    market = MarketSnapshot(current_spread_pips=2.0, atr_m5_pips=float("nan"))
    result = check_spread_filter(market)
    assert result.allow is True


def test_atr_cap_ignored_when_atr_negative() -> None:
    market = MarketSnapshot(current_spread_pips=2.0, atr_m5_pips=-5.0)
    result = check_spread_filter(market)
    assert result.allow is True


def test_rejects_when_atr_nan_and_spread_exceeds_abs_cap() -> None:
    market = MarketSnapshot(
        current_spread_pips=SPREAD_ABS_CAP_PIPS + 0.5,
        atr_m5_pips=float("nan"),
    )
    result = check_spread_filter(market)
    assert result.allow is False
    assert "abs_cap" in result.reason


def test_reason_contains_numeric_detail() -> None:
    market = MarketSnapshot(current_spread_pips=10.0, atr_m5_pips=5.0)
    result = check_spread_filter(market)
    assert result.allow is False
    assert "10.00p" in result.reason
    # ATR cap = 0.3 * 5 = 1.50p, this should be the binding cap.
    assert "atr_cap" in result.reason


def test_atr_mult_constant_is_consumed() -> None:
    # If SPREAD_ATR_MULT changes, the binding cap should reflect it.
    # Here we just verify that the rule respects the constant by
    # constructing a borderline case at exactly the threshold.
    market = MarketSnapshot(
        current_spread_pips=SPREAD_ATR_MULT * 10.0,
        atr_m5_pips=10.0,
    )
    result = check_spread_filter(market)
    assert result.allow is True
