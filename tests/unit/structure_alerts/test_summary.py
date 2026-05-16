"""Tests for structure_alerts.summary.build_hourly_summary.

Verifies the spec §11 template, NaN / None handling, JPY rendering,
and the hour-bucket dedupe key.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from alerts import AlertSeverity
from structure_alerts.summary import _hour_bucket, build_hourly_summary
from structure_alerts.types import AlertEventKind

from .conftest import make_level, make_state


_NOW = datetime(2026, 5, 16, 9, 0, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Catalogue shape — severity / kind / dedupe-key format
# ---------------------------------------------------------------------------


def test_event_kind_and_severity() -> None:
    state = make_state()
    event = build_hourly_summary(state, now=_NOW)
    assert event.kind is AlertEventKind.HOURLY_SUMMARY
    assert event.severity is AlertSeverity.INFO


def test_event_pair_threads_through() -> None:
    state = make_state(pair="EURUSD")
    event = build_hourly_summary(state, now=_NOW)
    assert event.pair == "EURUSD"
    assert event.dedupe_key.startswith("EURUSD_HOURLY_SUMMARY_")


def test_event_timestamp_uses_now_parameter() -> None:
    """``state.timestamp`` is the bar-close ISO string; ``now`` is
    the wall-clock at dispatch. The AlertEvent.timestamp is ``now``;
    the dedupe-key hour bucket comes from state.timestamp."""
    state = make_state(timestamp="2026-05-16T09:00:00+00:00")
    event = build_hourly_summary(state, now=_NOW)
    assert event.timestamp == _NOW
    assert "2026-05-16T09:00:00+00:00" in event.dedupe_key


# ---------------------------------------------------------------------------
# Hour-bucket truncation
# ---------------------------------------------------------------------------


def test_hour_bucket_rounds_down_to_hour() -> None:
    assert _hour_bucket("2026-05-16T09:05:30+00:00") == "2026-05-16T09:00:00+00:00"
    assert _hour_bucket("2026-05-16T14:00:00+00:00") == "2026-05-16T14:00:00+00:00"
    assert _hour_bucket("2026-05-16T23:59:59+00:00") == "2026-05-16T23:00:00+00:00"


def test_hour_bucket_invalid_returns_verbatim() -> None:
    # Defensive: pre-Phase 11 record with weird timestamp doesn't
    # crash. Dedupe key becomes bar-stable instead of hour-rolling —
    # acceptable degradation.
    assert _hour_bucket("") == ""
    assert _hour_bucket("not-a-date") == "not-a-date"


def test_hour_bucket_in_dedupe_key_format() -> None:
    state = make_state(
        pair="GBPUSD", timestamp="2026-05-16T14:00:00+00:00",
    )
    event = build_hourly_summary(state, now=_NOW)
    assert event.dedupe_key == "GBPUSD_HOURLY_SUMMARY_2026-05-16T14:00:00+00:00"


# ---------------------------------------------------------------------------
# Spec §11 template
# ---------------------------------------------------------------------------


def test_full_text_matches_spec_template() -> None:
    """End-to-end: build a state with every field populated, assert
    the four-line summary matches the spec §11 shape."""
    sup = make_level(level_type="SUPPORT", price=1.30050, score=7.5, timeframe="H1")
    res = make_level(level_type="RESISTANCE", price=1.30420, score=8.1, timeframe="M15")
    liq_above = make_level(level_type="LIQUIDITY_HIGH", price=1.30580, score=8.5)
    liq_below = make_level(level_type="LIQUIDITY_LOW", price=1.29900, score=7.8)
    state = make_state(
        pair="GBPUSD",
        htf_bias="BULLISH",
        local_bias="BULLISH",
        structure_mode="TREND_CONTINUATION",
        confidence=0.74,
        nearest_support=sup,
        nearest_resistance=res,
        liquidity_above=liq_above,
        liquidity_below=liq_below,
        current_reaction="SUPPORT_REJECTION",
        acceptance_state="INSIDE_RANGE",
    )
    event = build_hourly_summary(state, now=_NOW)
    lines = event.full_text.split("\n")
    assert len(lines) == 4
    assert lines[0] == (
        "HTF=BULLISH local=BULLISH mode=TREND_CONTINUATION conf=0.74"
    )
    assert lines[1] == (
        "support=1.30050 (s=7.5)  resistance=1.30420 (s=8.1)"
    )
    assert lines[2] == "liq_above=1.30580  liq_below=1.29900"
    assert lines[3] == (
        "reaction=SUPPORT_REJECTION acceptance=INSIDE_RANGE"
    )


def test_short_text_is_one_line_digest() -> None:
    state = make_state(
        htf_bias="BEARISH",
        structure_mode="VOLATILE_SWEEP_ZONE",
        confidence=0.42,
    )
    event = build_hourly_summary(state, now=_NOW)
    assert event.short_text == "HTF=BEARISH mode=VOLATILE_SWEEP_ZONE conf=0.42"
    assert "\n" not in event.short_text


# ---------------------------------------------------------------------------
# None / NaN handling
# ---------------------------------------------------------------------------


def test_none_levels_render_as_em_dash() -> None:
    state = make_state(
        nearest_support=None,
        nearest_resistance=None,
        liquidity_above=None,
        liquidity_below=None,
    )
    event = build_hourly_summary(state, now=_NOW)
    lines = event.full_text.split("\n")
    assert "support=—" in lines[1]
    assert "resistance=—" in lines[1]
    assert lines[2] == "liq_above=—  liq_below=—"


def test_nan_confidence_renders_as_em_dash() -> None:
    state = make_state(confidence=float("nan"))
    event = build_hourly_summary(state, now=_NOW)
    assert "conf=—" in event.full_text.split("\n")[0]
    # debug carries None rather than NaN so JSON serialisation
    # downstream doesn't choke on it.
    assert event.debug["confidence"] is None


def test_nan_price_renders_as_em_dash() -> None:
    sup = make_level(level_type="SUPPORT", price=float("nan"))
    state = make_state(nearest_support=sup)
    event = build_hourly_summary(state, now=_NOW)
    levels_line = event.full_text.split("\n")[1]
    assert "support=—" in levels_line


def test_nan_score_omits_score_token() -> None:
    sup = make_level(level_type="SUPPORT", price=1.30000, score=float("nan"))
    state = make_state(nearest_support=sup)
    event = build_hourly_summary(state, now=_NOW)
    levels_line = event.full_text.split("\n")[1]
    # Price renders, score-token suppressed.
    assert "support=1.30000" in levels_line
    assert "(s=" not in levels_line.split("resistance=")[0]


# ---------------------------------------------------------------------------
# Pair-aware price formatting
# ---------------------------------------------------------------------------


def test_jpy_pair_three_decimal_rendering() -> None:
    sup = make_level(pair="USDJPY", level_type="SUPPORT", price=152.345, score=7.2)
    res = make_level(pair="USDJPY", level_type="RESISTANCE", price=153.120, score=7.8)
    state = make_state(
        pair="USDJPY",
        nearest_support=sup,
        nearest_resistance=res,
    )
    event = build_hourly_summary(state, now=_NOW)
    levels_line = event.full_text.split("\n")[1]
    assert "support=152.345" in levels_line
    assert "resistance=153.120" in levels_line
    # Not five-decimal:
    assert "152.34500" not in levels_line


# ---------------------------------------------------------------------------
# Debug payload
# ---------------------------------------------------------------------------


def test_debug_payload_carries_summary_inputs() -> None:
    state = make_state(
        htf_bias="BEARISH",
        local_bias="NEUTRAL",
        structure_mode="RANGE_BALANCE",
        confidence=0.65,
        current_reaction="SUPPORT_REJECTION",
        acceptance_state="INSIDE_RANGE",
        timestamp="2026-05-16T14:00:00+00:00",
    )
    event = build_hourly_summary(state, now=_NOW)
    assert event.debug["htf_bias"] == "BEARISH"
    assert event.debug["local_bias"] == "NEUTRAL"
    assert event.debug["structure_mode"] == "RANGE_BALANCE"
    assert event.debug["confidence"] == 0.65
    assert event.debug["current_reaction"] == "SUPPORT_REJECTION"
    assert event.debug["acceptance_state"] == "INSIDE_RANGE"
    assert event.debug["hour_bucket"] == "2026-05-16T14:00:00+00:00"
    assert event.debug["bar_timestamp"] == "2026-05-16T14:00:00+00:00"
