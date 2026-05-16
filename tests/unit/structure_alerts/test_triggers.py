"""Tests for structure_alerts.triggers.changes_to_events.

Per-kind shape tests: each case constructs the StructureChange that
the diff layer would produce, runs it through changes_to_events,
and asserts the resulting AlertEvent's kind, severity, dedupe_key,
and rendered text. Severity is double-checked against the locked
:func:`structure_alerts.types.severity_for` mapping to guarantee the
catalogue cannot drift via this layer.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from alerts import AlertSeverity
from structure_alerts.diff import ChangeKind, StructureChange
from structure_alerts.triggers import changes_to_events
from structure_alerts.types import AlertEventKind, severity_for

from .conftest import make_level, make_state


_NOW = datetime(2026, 5, 16, 9, 5, tzinfo=timezone.utc)


def _run_single(change: StructureChange, *, pair: str = "GBPUSD"):
    curr = make_state(pair=pair)
    events = changes_to_events([change], curr, now=_NOW)
    assert len(events) == 1
    return events[0]


# ---------------------------------------------------------------------------
# Empty / pass-through
# ---------------------------------------------------------------------------


def test_empty_changes_returns_empty_events() -> None:
    assert changes_to_events([], make_state(), now=_NOW) == []


def test_events_preserve_input_order() -> None:
    bias = StructureChange(
        kind=ChangeKind.HTF_BIAS_CHANGED, prev_value="BULLISH", curr_value="BEARISH",
    )
    mode = StructureChange(
        kind=ChangeKind.MODE_CHANGED, prev_value="RANGE_BALANCE",
        curr_value="VOLATILE_SWEEP_ZONE",
    )
    events = changes_to_events([bias, mode], make_state(), now=_NOW)
    assert [e.kind for e in events] == [
        AlertEventKind.HTF_BIAS_CHANGE,
        AlertEventKind.STRUCTURE_MODE_CHANGE,
    ]


# ---------------------------------------------------------------------------
# HTF_BIAS_CHANGE
# ---------------------------------------------------------------------------


def test_htf_bias_change_event_shape() -> None:
    change = StructureChange(
        kind=ChangeKind.HTF_BIAS_CHANGED,
        prev_value="BULLISH",
        curr_value="BEARISH",
    )
    event = _run_single(change)
    assert event.kind is AlertEventKind.HTF_BIAS_CHANGE
    assert event.severity is AlertSeverity.WARNING
    assert event.dedupe_key == "GBPUSD_HTF_BIAS_BEARISH"
    assert event.full_text == "HTF bias BULLISH -> BEARISH"
    assert event.short_text == "HTF BULLISH -> BEARISH"
    assert event.timestamp == _NOW
    assert event.debug == {"prev_htf_bias": "BULLISH", "curr_htf_bias": "BEARISH"}


# ---------------------------------------------------------------------------
# STRUCTURE_MODE_CHANGE
# ---------------------------------------------------------------------------


def test_mode_change_event_shape() -> None:
    change = StructureChange(
        kind=ChangeKind.MODE_CHANGED,
        prev_value="RANGE_BALANCE",
        curr_value="TREND_CONTINUATION",
    )
    event = _run_single(change)
    assert event.kind is AlertEventKind.STRUCTURE_MODE_CHANGE
    assert event.severity is AlertSeverity.WARNING
    assert event.dedupe_key == "GBPUSD_MODE_TREND_CONTINUATION"
    assert event.full_text == "Structure mode RANGE_BALANCE -> TREND_CONTINUATION"
    assert event.short_text == "mode RANGE_BALANCE -> TREND_CONTINUATION"


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------


def test_support_acceptance_break_event_shape() -> None:
    level = make_level(level_type="SUPPORT", price=1.33400, score=8.1, timeframe="H1")
    change = StructureChange(
        kind=ChangeKind.REACTION_OBSERVED,
        reaction="SUPPORT_ACCEPTANCE_BREAK",
        level=level,
    )
    event = _run_single(change)
    assert event.kind is AlertEventKind.SUPPORT_ACCEPTANCE_BREAK
    assert event.severity is AlertSeverity.CRITICAL
    # Spec §9 canonical example shape.
    assert event.dedupe_key == "GBPUSD_SUPPORT_ACCEPTANCE_13340"
    assert "Support broken at 1.33400" in event.full_text
    assert "H1" in event.full_text
    assert "score was 8.1" in event.full_text
    assert event.short_text == "support broken @ 1.33400"
    assert event.debug["reaction"] == "SUPPORT_ACCEPTANCE_BREAK"
    assert event.debug["side"] == "SUPPORT"


def test_resistance_acceptance_break_event_shape() -> None:
    level = make_level(level_type="RESISTANCE", price=1.31000, score=7.3, timeframe="M15")
    change = StructureChange(
        kind=ChangeKind.REACTION_OBSERVED,
        reaction="RESISTANCE_ACCEPTANCE_BREAK",
        level=level,
    )
    event = _run_single(change)
    assert event.kind is AlertEventKind.RESISTANCE_ACCEPTANCE_BREAK
    assert event.severity is AlertSeverity.CRITICAL
    assert event.dedupe_key == "GBPUSD_RESISTANCE_ACCEPTANCE_13100"
    assert "Resistance broken at 1.31000" in event.full_text
    assert event.debug["side"] == "RESISTANCE"


@pytest.mark.parametrize(
    "reaction,side",
    [
        ("SUPPORT_SWEEP_RECLAIM", "SUPPORT"),
        ("RESISTANCE_SWEEP_RECLAIM", "RESISTANCE"),
    ],
)
def test_sweep_reclaim_event_shape(reaction, side) -> None:
    level_type = "SUPPORT" if side == "SUPPORT" else "RESISTANCE"
    level = make_level(level_type=level_type, price=1.30500, timeframe="H1")
    change = StructureChange(
        kind=ChangeKind.REACTION_OBSERVED, reaction=reaction, level=level,
    )
    event = _run_single(change)
    assert event.kind is AlertEventKind.SWEEP_RECLAIM
    assert event.severity is AlertSeverity.WARNING
    assert event.dedupe_key == f"GBPUSD_SWEEP_RECLAIM_{side}_13050"
    assert event.short_text == f"sweep reclaim {side} @ 1.30500"


@pytest.mark.parametrize(
    "reaction,side",
    [
        ("FAILED_RECLAIM_BELOW_SUPPORT", "SUPPORT"),
        ("FAILED_RECLAIM_ABOVE_RESISTANCE", "RESISTANCE"),
    ],
)
def test_failed_reclaim_event_shape(reaction, side) -> None:
    level_type = "SUPPORT" if side == "SUPPORT" else "RESISTANCE"
    level = make_level(level_type=level_type, price=1.30800, timeframe="M5")
    change = StructureChange(
        kind=ChangeKind.REACTION_OBSERVED, reaction=reaction, level=level,
    )
    event = _run_single(change)
    assert event.kind is AlertEventKind.FAILED_RECLAIM
    assert event.severity is AlertSeverity.WARNING
    assert event.dedupe_key == f"GBPUSD_FAILED_RECLAIM_{side}_13080"
    assert event.short_text == f"failed reclaim {side} @ 1.30800"


def test_reaction_with_missing_level_skipped() -> None:
    change = StructureChange(
        kind=ChangeKind.REACTION_OBSERVED,
        reaction="SUPPORT_ACCEPTANCE_BREAK",
        level=None,
    )
    assert changes_to_events([change], make_state(), now=_NOW) == []


# ---------------------------------------------------------------------------
# NEW_MAJOR_LEVEL
# ---------------------------------------------------------------------------


def test_new_major_level_event_shape() -> None:
    level = make_level(level_type="SUPPORT", price=1.30200, score=7.5, timeframe="H1")
    change = StructureChange(kind=ChangeKind.NEW_MAJOR_LEVEL, level=level)
    event = _run_single(change)
    assert event.kind is AlertEventKind.NEW_MAJOR_LEVEL
    assert event.severity is AlertSeverity.INFO
    assert event.dedupe_key == "GBPUSD_NEW_LEVEL_SUPPORT_13020"
    assert "New major SUPPORT level at 1.30200" in event.full_text
    assert "score 7.5" in event.full_text
    assert event.short_text == "new SUPPORT @ 1.30200"


def test_new_major_level_resistance_side() -> None:
    level = make_level(level_type="RESISTANCE", price=1.31000, score=8.0)
    change = StructureChange(kind=ChangeKind.NEW_MAJOR_LEVEL, level=level)
    event = _run_single(change)
    assert event.dedupe_key == "GBPUSD_NEW_LEVEL_RESISTANCE_13100"
    assert event.short_text == "new RESISTANCE @ 1.31000"


# ---------------------------------------------------------------------------
# LEVEL_INVALIDATED
# ---------------------------------------------------------------------------


def test_level_invalidated_event_shape() -> None:
    level = make_level(level_type="SUPPORT", price=1.30000, score=7.0, timeframe="H1")
    change = StructureChange(kind=ChangeKind.LEVEL_INVALIDATED, level=level)
    event = _run_single(change)
    assert event.kind is AlertEventKind.LEVEL_INVALIDATED
    assert event.severity is AlertSeverity.INFO
    assert event.dedupe_key == "GBPUSD_LEVEL_INVALIDATED_SUPPORT_13000"
    assert "Support level at 1.30000 invalidated" in event.full_text
    assert event.short_text == "SUPPORT invalidated @ 1.30000"


# ---------------------------------------------------------------------------
# Pair-aware price formatting
# ---------------------------------------------------------------------------


def test_jpy_pair_renders_three_decimals() -> None:
    # USDJPY price 152.345 should render as "152.345", not "152.34500".
    level = make_level(
        pair="USDJPY", level_type="SUPPORT", price=152.345, score=7.5,
    )
    change = StructureChange(
        kind=ChangeKind.REACTION_OBSERVED,
        reaction="SUPPORT_ACCEPTANCE_BREAK",
        level=level,
    )
    curr = make_state(pair="USDJPY")
    events = changes_to_events([change], curr, now=_NOW)
    assert len(events) == 1
    assert "152.345" in events[0].full_text
    assert "152.34500" not in events[0].full_text
    # Banker's rounding: 152.345 / 0.01 = 15234.5 -> 15234.
    assert events[0].dedupe_key == "USDJPY_SUPPORT_ACCEPTANCE_15234"


# ---------------------------------------------------------------------------
# Severity is locked at the kind level
# ---------------------------------------------------------------------------


def test_every_event_severity_matches_severity_for_mapping() -> None:
    # Build one change for each non-summary AlertEventKind and assert
    # the produced event's severity equals severity_for(kind). The
    # HOURLY_SUMMARY kind is produced by build_hourly_summary in C-5,
    # not by changes_to_events, so it's covered by that suite.
    level = make_level(level_type="SUPPORT", price=1.30000, score=7.5)
    cases: list[tuple[StructureChange, AlertEventKind]] = [
        (
            StructureChange(
                kind=ChangeKind.HTF_BIAS_CHANGED,
                prev_value="BULLISH", curr_value="BEARISH",
            ),
            AlertEventKind.HTF_BIAS_CHANGE,
        ),
        (
            StructureChange(
                kind=ChangeKind.MODE_CHANGED,
                prev_value="RANGE_BALANCE", curr_value="TREND_CONTINUATION",
            ),
            AlertEventKind.STRUCTURE_MODE_CHANGE,
        ),
        (
            StructureChange(
                kind=ChangeKind.REACTION_OBSERVED,
                reaction="SUPPORT_ACCEPTANCE_BREAK", level=level,
            ),
            AlertEventKind.SUPPORT_ACCEPTANCE_BREAK,
        ),
        (
            StructureChange(
                kind=ChangeKind.REACTION_OBSERVED,
                reaction="RESISTANCE_ACCEPTANCE_BREAK",
                level=make_level(level_type="RESISTANCE", price=1.31000),
            ),
            AlertEventKind.RESISTANCE_ACCEPTANCE_BREAK,
        ),
        (
            StructureChange(
                kind=ChangeKind.REACTION_OBSERVED,
                reaction="SUPPORT_SWEEP_RECLAIM", level=level,
            ),
            AlertEventKind.SWEEP_RECLAIM,
        ),
        (
            StructureChange(
                kind=ChangeKind.REACTION_OBSERVED,
                reaction="FAILED_RECLAIM_BELOW_SUPPORT", level=level,
            ),
            AlertEventKind.FAILED_RECLAIM,
        ),
        (
            StructureChange(kind=ChangeKind.NEW_MAJOR_LEVEL, level=level),
            AlertEventKind.NEW_MAJOR_LEVEL,
        ),
        (
            StructureChange(kind=ChangeKind.LEVEL_INVALIDATED, level=level),
            AlertEventKind.LEVEL_INVALIDATED,
        ),
    ]
    for change, expected_kind in cases:
        event = _run_single(change)
        assert event.kind is expected_kind
        assert event.severity is severity_for(expected_kind)
