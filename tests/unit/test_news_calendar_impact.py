"""Tests for risk.news_calendar.impact — severity enum + deviation helpers."""

from __future__ import annotations

import pytest

from risk.news_calendar.impact import (
    DEVIATION_THRESHOLD,
    Impact,
    compute_deviation,
    compute_surprise,
    parse_impact,
)


# ---------------------------------------------------------------------------
# parse_impact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("high", Impact.HIGH),
        ("HIGH", Impact.HIGH),
        (" High ", Impact.HIGH),
        ("medium", Impact.MEDIUM),
        ("MEDIUM", Impact.MEDIUM),
        ("low", Impact.LOW),
        ("", Impact.LOW),
        ("nonsense", Impact.LOW),
        (None, Impact.LOW),
    ],
)
def test_parse_impact(raw, expected) -> None:
    assert parse_impact(raw) is expected


def test_impact_enum_values_match_finnhub_lowercase() -> None:
    """Finnhub returns ``high``/``medium``/``low``; enum values stay aligned
    so parse_impact stays a trivial mapping."""
    assert Impact.HIGH.value == "high"
    assert Impact.MEDIUM.value == "medium"
    assert Impact.LOW.value == "low"


# ---------------------------------------------------------------------------
# compute_deviation — BEAT / MISS / IN_LINE + direction_hint
# ---------------------------------------------------------------------------


def test_compute_deviation_zero_forecast_returns_nones() -> None:
    out = compute_deviation(actual=1.0, forecast=0.0)
    assert out == {"deviation": None, "direction_hint": None, "beat_miss": None}


def test_compute_deviation_beat_continuation() -> None:
    # +10% surprise (above default 5% threshold)
    out = compute_deviation(actual=110.0, forecast=100.0)
    assert out["beat_miss"] == "BEAT"
    assert out["direction_hint"] == "CONTINUATION"
    assert out["deviation"] == pytest.approx(0.10)


def test_compute_deviation_miss_continuation() -> None:
    # -10% surprise
    out = compute_deviation(actual=90.0, forecast=100.0)
    assert out["beat_miss"] == "MISS"
    assert out["direction_hint"] == "CONTINUATION"
    assert out["deviation"] == pytest.approx(-0.10)


def test_compute_deviation_inline_reversal() -> None:
    # 2% surprise (below default 5% threshold)
    out = compute_deviation(actual=102.0, forecast=100.0)
    assert out["beat_miss"] == "IN_LINE"
    assert out["direction_hint"] == "REVERSAL"


def test_compute_deviation_threshold_boundary_strict() -> None:
    """At exactly the threshold, the legacy convention is REVERSAL (not
    CONTINUATION) — the comparison is strict ``>``."""
    out = compute_deviation(actual=100.0 * (1 + DEVIATION_THRESHOLD), forecast=100.0)
    assert out["beat_miss"] == "IN_LINE"
    assert out["direction_hint"] == "REVERSAL"


def test_compute_deviation_negative_forecast() -> None:
    """``deviation`` uses ``abs(forecast)`` denominator so the sign is from
    the numerator only — a negative forecast doesn't flip BEAT/MISS."""
    out = compute_deviation(actual=-50.0, forecast=-100.0)
    # actual - forecast = +50, abs(forecast) = 100, deviation = +0.5
    assert out["deviation"] == pytest.approx(0.5)
    assert out["beat_miss"] == "BEAT"


# ---------------------------------------------------------------------------
# compute_surprise — coarse classification, no threshold gating
# ---------------------------------------------------------------------------


def test_compute_surprise_none_inputs() -> None:
    assert compute_surprise(None, 100.0) == (None, None)
    assert compute_surprise(100.0, None) == (None, None)
    assert compute_surprise(None, None) == (None, None)


def test_compute_surprise_zero_estimate() -> None:
    assert compute_surprise(100.0, 0.0) == (None, None)


def test_compute_surprise_beat() -> None:
    pct, label = compute_surprise(110.0, 100.0)
    assert pct == pytest.approx(0.10)
    assert label == "BEAT"


def test_compute_surprise_miss() -> None:
    pct, label = compute_surprise(90.0, 100.0)
    assert pct == pytest.approx(-0.10)
    assert label == "MISS"


def test_compute_surprise_inline() -> None:
    pct, label = compute_surprise(100.0, 100.0)
    assert pct == pytest.approx(0.0)
    assert label == "IN_LINE"


def test_compute_surprise_non_numeric_returns_none() -> None:
    """String inputs must not raise — they degrade to ``(None, None)``."""
    assert compute_surprise("not-a-number", 100.0) == (None, None)
    assert compute_surprise(100.0, "not-a-number") == (None, None)
