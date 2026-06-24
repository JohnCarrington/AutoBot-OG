"""Tests for strategies.news (step 5b — real detect_news).

Exercises the full pipeline: published HIGH-impact release → deviation
math → news_direction mapping → structure anchor → Signal.

Cache priming via ``_inject_events_for_tests`` so the tests are
hermetic — no Finnhub HTTP call, no time-of-day dependence.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from common import Direction
from day_type import DayType
from risk.news_calendar.calendar import (
    _inject_events_for_tests,
    _reset_cache_for_tests,
)
from strategies.news import detect_news
from structure_engine import StructureLevel, StructureState


_NOW = datetime(2026, 6, 25, 12, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Cache fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_cache():
    _reset_cache_for_tests()
    yield
    _reset_cache_for_tests()


def _inject(events: list[dict]) -> None:
    _inject_events_for_tests(events)


def _event(
    *,
    country: str,
    event_name: str,
    time_str: str = "2026-06-25 12:30:00",
    actual: float | None = None,
    estimate: float | None = None,
    impact: str = "high",
    prev: float | None = None,
) -> dict:
    return {
        "country": country,
        "event": event_name,
        "time": time_str,
        "actual": actual,
        "estimate": estimate,
        "impact": impact,
        "prev": prev,
    }


# ---------------------------------------------------------------------------
# Structure / DataFrame helpers
# ---------------------------------------------------------------------------


def _level(*, side: str, price: float) -> StructureLevel:
    return StructureLevel(
        pair="GBPUSD",
        level_type=("SUPPORT" if side == "LOW" else "RESISTANCE"),
        price=price,
        zone_low=price - 0.0004,
        zone_high=price + 0.0004,
        timeframe="H1",
        score=7.0,
        touch_count=2,
        last_touched_ts=None,
        source="swing_h1",
        debug={},
    )


def _state(
    *,
    htf_bias: str = "BULLISH",
    nearest_support_price: float | None = 1.30000,
    nearest_resistance_price: float | None = 1.30200,
) -> StructureState:
    return StructureState(
        pair="GBPUSD",
        timestamp=_NOW.isoformat(),
        is_valid=True,
        htf_bias=htf_bias,  # type: ignore[arg-type]
        local_bias=htf_bias,  # type: ignore[arg-type]
        nearest_support=(
            _level(side="LOW", price=nearest_support_price)
            if nearest_support_price is not None else None
        ),
        nearest_resistance=(
            _level(side="HIGH", price=nearest_resistance_price)
            if nearest_resistance_price is not None else None
        ),
        liquidity_above=None,
        liquidity_below=None,
        current_reaction="NONE",
        acceptance_state="NONE",
        structure_mode="TREND_CONTINUATION",
        confidence=0.7,
        reason="test",
        levels=[],
        debug={},
    )


def _df_m5() -> pd.DataFrame:
    idx = pd.DatetimeIndex(
        [_NOW - timedelta(minutes=10),
         _NOW - timedelta(minutes=5),
         _NOW],
        tz="UTC",
    )
    return pd.DataFrame(
        [
            {"open": 1.30050, "high": 1.30060, "low": 1.30040,
             "close": 1.30050, "atr_14": 0.0020},
            {"open": 1.30050, "high": 1.30060, "low": 1.30040,
             "close": 1.30050, "atr_14": 0.0020},
            {"open": 1.30050, "high": 1.30060, "low": 1.30040,
             "close": 1.30050, "atr_14": 0.0020},
        ],
        index=idx,
    )


def _df_h1(macd_hist: float = 0.10) -> pd.DataFrame:
    return pd.DataFrame([{"macd_hist_12_26_9": macd_hist}])


# ---------------------------------------------------------------------------
# Happy paths — DIRECT and INVERTED
# ---------------------------------------------------------------------------


def test_direct_us_beat_emits_bearish_signal() -> None:
    """USD CPI beats forecast by 10% → USD STRONGER → GBPUSD BEARISH.

    Anchor for BEARISH is nearest_support.zone_high; SL above it by
    max(floor, mult × ATR). The signal carries strategy_name="news".
    """
    _inject([_event(
        country="US", event_name="CPI YoY",
        actual=3.30, estimate=3.00,  # +10% surprise
    )])
    sig = detect_news(
        _df_m5(), _df_h1(macd_hist=-0.10),
        DayType.BIG_NEWS_DAY,
        _state(htf_bias="BULLISH"),  # data is COUNTER-trend (USD strong → pair down)
        "GBPUSD",
        _NOW,
    )
    assert sig is not None
    assert sig.strategy_name == "news"
    assert sig.direction == Direction.BEARISH
    assert sig.day_type == DayType.BIG_NEWS_DAY
    assert sig.suggested_tp_price is None
    # SL above anchor (BEARISH) — anchor is nearest_support.zone_high
    # = 1.30000 + 0.0004 = 1.30040.
    assert sig.suggested_sl_price > sig.suggested_entry_price
    # Debug payload captures the surprise so ops can trace it.
    assert sig.debug["release_currency"] == "USD"
    assert sig.debug["release_event"] == "CPI YoY"
    assert sig.debug["release_deviation"] == pytest.approx(0.10)
    assert sig.debug["data_direction"] == "BEARISH"


def test_direct_us_miss_emits_bullish_signal() -> None:
    """USD CPI misses by 10% → USD WEAKER → GBPUSD BULLISH."""
    _inject([_event(
        country="US", event_name="CPI YoY",
        actual=2.70, estimate=3.00,  # -10% surprise
    )])
    sig = detect_news(
        _df_m5(), _df_h1(macd_hist=0.10),
        DayType.BIG_NEWS_DAY,
        _state(htf_bias="BULLISH"),
        "GBPUSD",
        _NOW,
    )
    assert sig is not None
    assert sig.direction == Direction.BULLISH
    # SL below anchor (BULLISH) — anchor is nearest_resistance.zone_low.
    assert sig.suggested_sl_price < sig.suggested_entry_price


def test_inverted_us_unemployment_beat_flips_to_bullish() -> None:
    """US Unemployment Rate beats forecast (HIGHER) → USD WEAKER →
    GBPUSD BULLISH.

    The sign-flip test in the live detector. The 5a direction module
    handles the inversion; this confirms it propagates to the emitted
    Signal's Direction.
    """
    _inject([_event(
        country="US", event_name="Unemployment Rate",
        actual=4.40, estimate=4.00,  # +10% — higher unemployment
    )])
    sig = detect_news(
        _df_m5(), _df_h1(macd_hist=0.10),
        DayType.BIG_NEWS_DAY,
        _state(htf_bias="BEARISH"),  # data is COUNTER-htf
        "GBPUSD",
        _NOW,
    )
    assert sig is not None
    assert sig.strategy_name == "news"
    assert sig.direction == Direction.BULLISH, (
        "INVERTED release: higher unemployment → USD weaker → GBPUSD up"
    )
    assert sig.debug["release_event"] == "Unemployment Rate"


def test_gbp_beat_routes_to_bullish() -> None:
    """GBP CPI beats → GBP STRONGER → GBPUSD BULLISH (base strength)."""
    _inject([_event(
        country="GB", event_name="CPI YoY",
        actual=3.30, estimate=3.00,
    )])
    sig = detect_news(
        _df_m5(), _df_h1(macd_hist=0.10),
        DayType.BIG_NEWS_DAY,
        _state(htf_bias="BULLISH"),
        "GBPUSD",
        _NOW,
    )
    assert sig is not None
    assert sig.direction == Direction.BULLISH
    assert sig.debug["release_currency"] == "GBP"


# ---------------------------------------------------------------------------
# Returns None branches
# ---------------------------------------------------------------------------


def test_in_line_surprise_returns_none() -> None:
    """Below DEVIATION_THRESHOLD → no signal."""
    _inject([_event(
        country="US", event_name="CPI YoY",
        actual=3.05, estimate=3.00,  # +1.67% — below 5% threshold
    )])
    sig = detect_news(
        _df_m5(), _df_h1(), DayType.BIG_NEWS_DAY,
        _state(), "GBPUSD", _NOW,
    )
    assert sig is None


def test_actual_not_yet_published_returns_none() -> None:
    """actual=None → release not yet fired → no signal."""
    _inject([_event(
        country="US", event_name="CPI YoY",
        actual=None, estimate=3.00,
    )])
    sig = detect_news(
        _df_m5(), _df_h1(), DayType.BIG_NEWS_DAY,
        _state(), "GBPUSD", _NOW,
    )
    assert sig is None


def test_no_release_in_window_returns_none() -> None:
    """No HIGH event for these currencies → no signal."""
    # Release time is six hours from now — far outside any window.
    _inject([_event(
        country="US", event_name="CPI YoY",
        time_str="2026-06-25 18:30:00",
        actual=3.30, estimate=3.00,
    )])
    sig = detect_news(
        _df_m5(), _df_h1(), DayType.BIG_NEWS_DAY,
        _state(), "GBPUSD", _NOW,
    )
    assert sig is None


def test_no_structure_anchor_in_data_direction_returns_none() -> None:
    """Data says BEARISH, but nearest_support is None → no anchor → no signal."""
    _inject([_event(
        country="US", event_name="CPI YoY",
        actual=3.30, estimate=3.00,
    )])
    sig = detect_news(
        _df_m5(), _df_h1(), DayType.BIG_NEWS_DAY,
        _state(htf_bias="BULLISH", nearest_support_price=None),
        "GBPUSD", _NOW,
    )
    assert sig is None


def test_invalid_structure_state_returns_none() -> None:
    _inject([_event(
        country="US", event_name="CPI YoY",
        actual=3.30, estimate=3.00,
    )])
    state = _state()
    # Build a copy with is_valid=False — dataclass is frozen so use
    # dataclasses.replace.
    from dataclasses import replace
    invalid = replace(state, is_valid=False)
    sig = detect_news(
        _df_m5(), _df_h1(), DayType.BIG_NEWS_DAY,
        invalid, "GBPUSD", _NOW,
    )
    assert sig is None


def test_estimate_zero_returns_none() -> None:
    """Estimate of zero would divide-by-zero in compute_deviation;
    detector skips."""
    _inject([_event(
        country="US", event_name="Trade Balance",
        actual=10.0, estimate=0.0,
    )])
    sig = detect_news(
        _df_m5(), _df_h1(), DayType.BIG_NEWS_DAY,
        _state(), "GBPUSD", _NOW,
    )
    assert sig is None


def test_release_for_currency_not_in_pair_returns_none() -> None:
    """A JPY release should not gate GBPUSD even with a real surprise."""
    _inject([_event(
        country="JP", event_name="CPI YoY",
        actual=3.30, estimate=3.00,
    )])
    sig = detect_news(
        _df_m5(), _df_h1(), DayType.BIG_NEWS_DAY,
        _state(), "GBPUSD", _NOW,
    )
    assert sig is None


def test_medium_impact_release_does_not_fire() -> None:
    """Only HIGH-impact releases gate detect_news."""
    _inject([_event(
        country="US", event_name="Industrial Production",
        impact="medium", actual=3.30, estimate=3.00,
    )])
    sig = detect_news(
        _df_m5(), _df_h1(), DayType.BIG_NEWS_DAY,
        _state(), "GBPUSD", _NOW,
    )
    assert sig is None


def test_naive_current_time_treated_as_utc() -> None:
    """Naive current_time is interpreted as UTC — parity with the rest
    of the calendar layer."""
    _inject([_event(
        country="US", event_name="CPI YoY",
        actual=3.30, estimate=3.00,
    )])
    naive_now = _NOW.replace(tzinfo=None)
    sig = detect_news(
        _df_m5(), _df_h1(), DayType.BIG_NEWS_DAY,
        _state(), "GBPUSD", naive_now,
    )
    assert sig is not None
