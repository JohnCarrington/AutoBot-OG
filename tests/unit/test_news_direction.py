"""Tests for strategies.news_direction (step 5a).

Pure module — no Finnhub I/O, no calendar fetch. Tests pin the truth
table for the surprise → pair-direction mapping. The INVERTED-release
sign-flip is the correctness-critical case: a wrong sign on a real
release is a live trade in the wrong direction.
"""
from __future__ import annotations

import pytest

from common import Direction
from risk.news_calendar.impact import DEVIATION_THRESHOLD
from strategies.news_direction import (
    INVERTED_RELEASES,
    currency_strength_from_surprise,
    direction_for_release,
    pair_direction_from_currency_strength,
)


# ---------------------------------------------------------------------------
# currency_strength_from_surprise — DIRECT releases
# ---------------------------------------------------------------------------


def test_direct_beat_returns_stronger() -> None:
    """CPI beat by 10% → currency STRONGER."""
    assert currency_strength_from_surprise("CPI y/y", 0.10) == "STRONGER"


def test_direct_miss_returns_weaker() -> None:
    """CPI miss by 10% → currency WEAKER."""
    assert currency_strength_from_surprise("CPI y/y", -0.10) == "WEAKER"


def test_in_line_returns_none() -> None:
    """Below the threshold → no actionable surprise."""
    assert currency_strength_from_surprise("CPI y/y", 0.02) is None
    assert currency_strength_from_surprise("CPI y/y", -0.02) is None


def test_exactly_at_threshold_returns_none() -> None:
    """Boundary: == threshold is in-line; only strictly > threshold trades."""
    assert (
        currency_strength_from_surprise("CPI y/y", DEVIATION_THRESHOLD) is None
    )
    assert (
        currency_strength_from_surprise("CPI y/y", -DEVIATION_THRESHOLD) is None
    )


def test_just_above_threshold_returns_signal() -> None:
    bump = DEVIATION_THRESHOLD + 1e-6
    assert currency_strength_from_surprise("CPI y/y", bump) == "STRONGER"
    assert currency_strength_from_surprise("CPI y/y", -bump) == "WEAKER"


# ---------------------------------------------------------------------------
# currency_strength_from_surprise — INVERTED releases (the sign-flip test)
# ---------------------------------------------------------------------------


def test_inverted_release_beat_flips_to_weaker() -> None:
    """Unemployment Rate beats forecast (HIGHER actual) → currency WEAKER.

    THE sign-flip test. Higher unemployment is bad news for the
    economy → the currency should weaken even though deviation > 0.
    A regression here is a live trade in the wrong direction.
    """
    assert currency_strength_from_surprise("Unemployment Rate", 0.10) == "WEAKER"


def test_inverted_release_miss_flips_to_stronger() -> None:
    """Unemployment Rate misses forecast (LOWER actual) → currency STRONGER."""
    assert (
        currency_strength_from_surprise("Unemployment Rate", -0.10) == "STRONGER"
    )


def test_inverted_release_in_line_still_returns_none() -> None:
    """Threshold gate applies BEFORE the inversion flip."""
    assert currency_strength_from_surprise("Unemployment Rate", 0.02) is None


@pytest.mark.parametrize("event_name", sorted(INVERTED_RELEASES))
def test_each_inverted_pattern_flips_sign(event_name: str) -> None:
    """Every pattern in INVERTED_RELEASES must actually flip the sign."""
    assert currency_strength_from_surprise(event_name, 0.10) == "WEAKER"
    assert currency_strength_from_surprise(event_name, -0.10) == "STRONGER"


@pytest.mark.parametrize("event_name", [
    "CPI y/y",
    "Non-Farm Payrolls",
    "GDP Growth Rate QoQ",
    "Retail Sales m/m",
    "ISM Manufacturing PMI",
    "Fed Interest Rate Decision",
    "Average Hourly Earnings m/m",
])
def test_non_inverted_release_stays_direct(event_name: str) -> None:
    """A normal release whose name doesn't match any inverted pattern."""
    assert currency_strength_from_surprise(event_name, 0.10) == "STRONGER"
    assert currency_strength_from_surprise(event_name, -0.10) == "WEAKER"


def test_substring_match_case_insensitive() -> None:
    """Match is lowercase substring, so casing variations all flip."""
    assert (
        currency_strength_from_surprise("UNEMPLOYMENT RATE", 0.10) == "WEAKER"
    )
    assert (
        currency_strength_from_surprise("Initial Jobless Claims", 0.10)
        == "WEAKER"
    )


def test_substring_match_catches_real_finnhub_titles() -> None:
    """Real Finnhub titles often add suffixes / qualifiers — substring
    matching must still catch them."""
    # Real Finnhub variants seen in practice.
    assert (
        currency_strength_from_surprise("Initial Jobless Claims", 0.10)
        == "WEAKER"
    )
    assert (
        currency_strength_from_surprise(
            "Continuing Jobless Claims", 0.10
        ) == "WEAKER"
    )


# ---------------------------------------------------------------------------
# pair_direction_from_currency_strength — base/quote × stronger/weaker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pair,currency,strength,expected", [
    # GBPUSD: base=GBP, quote=USD.
    ("GBPUSD", "GBP", "STRONGER", Direction.BULLISH),   # base up → pair up
    ("GBPUSD", "GBP", "WEAKER",   Direction.BEARISH),   # base down → pair down
    ("GBPUSD", "USD", "STRONGER", Direction.BEARISH),   # quote up → pair down
    ("GBPUSD", "USD", "WEAKER",   Direction.BULLISH),   # quote down → pair up
    # EURUSD: base=EUR, quote=USD.
    ("EURUSD", "EUR", "STRONGER", Direction.BULLISH),
    ("EURUSD", "USD", "STRONGER", Direction.BEARISH),
    # USDJPY: base=USD, quote=JPY.
    ("USDJPY", "USD", "STRONGER", Direction.BULLISH),
    ("USDJPY", "JPY", "STRONGER", Direction.BEARISH),
    ("USDJPY", "JPY", "WEAKER",   Direction.BULLISH),
    # USDCAD: base=USD, quote=CAD.
    ("USDCAD", "CAD", "STRONGER", Direction.BEARISH),
    ("USDCAD", "CAD", "WEAKER",   Direction.BULLISH),
    # GBPJPY: base=GBP, quote=JPY.
    ("GBPJPY", "GBP", "STRONGER", Direction.BULLISH),
    ("GBPJPY", "JPY", "STRONGER", Direction.BEARISH),
])
def test_pair_direction_truth_table(
    pair: str, currency: str, strength: str, expected: Direction,
) -> None:
    assert (
        pair_direction_from_currency_strength(pair, currency, strength)
        == expected
    )


def test_pair_direction_unknown_pair_returns_none() -> None:
    assert (
        pair_direction_from_currency_strength("XAUUSD", "USD", "STRONGER")
        is None
    )


def test_pair_direction_currency_not_in_pair_returns_none() -> None:
    """Defensive guard: EUR is not in GBPUSD → None."""
    assert (
        pair_direction_from_currency_strength("GBPUSD", "EUR", "STRONGER")
        is None
    )


def test_pair_direction_case_insensitive() -> None:
    """Pair and currency casing must not matter."""
    assert (
        pair_direction_from_currency_strength("gbpusd", "usd", "STRONGER")
        == Direction.BEARISH
    )


# ---------------------------------------------------------------------------
# direction_for_release — end-to-end convenience
# ---------------------------------------------------------------------------


def test_direction_for_release_direct_quote_beat() -> None:
    """USD CPI beat on GBPUSD → quote stronger → BEARISH."""
    assert (
        direction_for_release("GBPUSD", "USD", "CPI y/y", 0.10)
        == Direction.BEARISH
    )


def test_direction_for_release_direct_base_beat() -> None:
    """GBP CPI beat on GBPUSD → base stronger → BULLISH."""
    assert (
        direction_for_release("GBPUSD", "GBP", "CPI y/y", 0.10)
        == Direction.BULLISH
    )


def test_direction_for_release_direct_miss() -> None:
    """USD CPI miss on GBPUSD → quote weaker → BULLISH."""
    assert (
        direction_for_release("GBPUSD", "USD", "CPI y/y", -0.10)
        == Direction.BULLISH
    )


def test_direction_for_release_inverted_beat_flips_pair_direction() -> None:
    """US Unemployment Rate beat on GBPUSD: higher unemployment → USD
    WEAKER → GBPUSD BULLISH (NOT BEARISH).

    End-to-end version of the sign-flip test — proves the inversion
    propagates through to the pair-level Direction.
    """
    assert (
        direction_for_release("GBPUSD", "USD", "Unemployment Rate", 0.10)
        == Direction.BULLISH
    )


def test_direction_for_release_inverted_miss_flips_pair_direction() -> None:
    """US Unemployment Rate miss on GBPUSD: lower unemployment → USD
    STRONGER → GBPUSD BEARISH."""
    assert (
        direction_for_release("GBPUSD", "USD", "Unemployment Rate", -0.10)
        == Direction.BEARISH
    )


def test_direction_for_release_in_line_returns_none() -> None:
    assert direction_for_release("GBPUSD", "USD", "CPI y/y", 0.02) is None


def test_direction_for_release_currency_not_in_pair_returns_none() -> None:
    """EUR release on a GBPUSD candidate → None even if surprise is real."""
    assert direction_for_release("GBPUSD", "EUR", "CPI y/y", 0.10) is None


def test_direction_for_release_unknown_pair_returns_none() -> None:
    assert direction_for_release("XAUUSD", "USD", "CPI y/y", 0.10) is None
