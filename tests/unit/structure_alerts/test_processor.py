"""Tests for structure_alerts.processor.process_structure_alerts.

Pipeline integration: compute_structure_diff → changes_to_events →
dedupe filter. Each upstream layer is unit-tested separately
(test_diff, test_triggers, test_dedupe) — these tests verify
correct wiring + cold-start contract + dedupe-cache mutation
visibility.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from alerts import AlertSeverity
from structure_alerts.dedupe import DedupeCache
from structure_alerts.processor import process_structure_alerts
from structure_alerts.types import AlertEventKind

from .conftest import make_level, make_state


_NOW = datetime(2026, 5, 16, 9, 5, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Cold-start contract
# ---------------------------------------------------------------------------


def test_returns_empty_when_prev_is_none() -> None:
    """Cold start: prev=None always returns [] regardless of curr.
    Dedupe cache is NOT mutated."""
    dedupe = DedupeCache()
    curr = make_state(htf_bias="BULLISH", structure_mode="TREND_CONTINUATION")
    events = process_structure_alerts(
        prev=None, curr=curr, dedupe=dedupe, now=_NOW,
    )
    assert events == []
    assert dedupe.size == 0


def test_returns_empty_when_curr_invalid() -> None:
    dedupe = DedupeCache()
    prev = make_state(htf_bias="NEUTRAL")
    curr = make_state(htf_bias="BULLISH", is_valid=False)
    events = process_structure_alerts(
        prev=prev, curr=curr, dedupe=dedupe, now=_NOW,
    )
    assert events == []
    assert dedupe.size == 0


def test_returns_empty_when_no_transitions() -> None:
    """prev == curr in every diff-relevant field → no events."""
    dedupe = DedupeCache()
    state = make_state(htf_bias="BULLISH", structure_mode="TREND_CONTINUATION")
    events = process_structure_alerts(
        prev=state, curr=state, dedupe=dedupe, now=_NOW,
    )
    assert events == []
    assert dedupe.size == 0


# ---------------------------------------------------------------------------
# Single-event path
# ---------------------------------------------------------------------------


def test_single_change_produces_single_event_and_records_dedupe() -> None:
    dedupe = DedupeCache()
    prev = make_state(htf_bias="BULLISH")
    curr = make_state(htf_bias="BEARISH")
    events = process_structure_alerts(
        prev=prev, curr=curr, dedupe=dedupe, now=_NOW,
    )
    assert len(events) == 1
    assert events[0].kind is AlertEventKind.HTF_BIAS_CHANGE
    assert events[0].timestamp == _NOW
    # Dedupe cache mutated.
    assert dedupe.last_fired("GBPUSD_HTF_BIAS_BEARISH") == _NOW


# ---------------------------------------------------------------------------
# Dedupe filtering
# ---------------------------------------------------------------------------


def test_dedupe_filters_repeat_event_within_cooldown() -> None:
    """First bar's HTF bias change fires; a same-bar-equivalent
    transition recurring within the WARNING 1h cooldown is blocked."""
    dedupe = DedupeCache()
    prev = make_state(htf_bias="BULLISH")
    curr = make_state(htf_bias="BEARISH")

    first = process_structure_alerts(
        prev=prev, curr=curr, dedupe=dedupe, now=_NOW,
    )
    assert len(first) == 1

    # Simulate the next bar where the bias flips back to BULLISH
    # then back to BEARISH (rare but possible across two bars). The
    # second BEARISH transition within 30 minutes lands on the same
    # dedupe key (curr_bias=BEARISH) and is blocked.
    second = process_structure_alerts(
        prev=make_state(htf_bias="BULLISH"),
        curr=make_state(htf_bias="BEARISH"),
        dedupe=dedupe,
        now=_NOW + timedelta(minutes=30),
    )
    assert second == []


def test_dedupe_allows_event_after_cooldown_elapses() -> None:
    dedupe = DedupeCache()
    prev = make_state(htf_bias="BULLISH")
    curr = make_state(htf_bias="BEARISH")

    process_structure_alerts(prev=prev, curr=curr, dedupe=dedupe, now=_NOW)
    # WARNING cooldown is 1h.
    later = process_structure_alerts(
        prev=make_state(htf_bias="BULLISH"),
        curr=make_state(htf_bias="BEARISH"),
        dedupe=dedupe,
        now=_NOW + timedelta(hours=1, minutes=1),
    )
    assert len(later) == 1


# ---------------------------------------------------------------------------
# Multi-event ordering and dedupe interaction
# ---------------------------------------------------------------------------


def test_multi_event_bar_preserves_order_through_dedupe() -> None:
    """A bar that fires HTF + MODE + REACTION + NEW_LEVEL +
    INVALIDATED simultaneously: all five reach the processor's
    output (none deduped — empty cache), in the diff/trigger
    emit order (bias → mode → reaction → new-level → invalidated).
    """
    dedupe = DedupeCache()
    prev_support = make_level(level_type="SUPPORT", price=1.30000, score=7.0)
    new_resistance = make_level(level_type="RESISTANCE", price=1.31500, score=8.0)
    prev = make_state(
        htf_bias="BULLISH",
        structure_mode="TREND_CONTINUATION",
        nearest_support=prev_support,
        levels=[prev_support],
    )
    curr = make_state(
        htf_bias="BEARISH",
        structure_mode="VOLATILE_SWEEP_ZONE",
        nearest_support=None,
        nearest_resistance=new_resistance,
        current_reaction="RESISTANCE_ACCEPTANCE_BREAK",
        levels=[new_resistance],
    )
    events = process_structure_alerts(
        prev=prev, curr=curr, dedupe=dedupe, now=_NOW,
    )
    kinds = [e.kind for e in events]
    assert kinds == [
        AlertEventKind.HTF_BIAS_CHANGE,
        AlertEventKind.STRUCTURE_MODE_CHANGE,
        AlertEventKind.RESISTANCE_ACCEPTANCE_BREAK,
        AlertEventKind.NEW_MAJOR_LEVEL,
        AlertEventKind.LEVEL_INVALIDATED,
    ]


def test_multi_event_bar_some_deduped_some_not() -> None:
    """Mid-session: one of the events on this bar has already fired
    this hour (cooldown active), the others are fresh. The deduped
    event drops out; the fresh events survive in order.
    """
    dedupe = DedupeCache()
    # Pre-seed dedupe so the HTF_BIAS_CHANGE for BEARISH is blocked.
    dedupe.should_fire(
        "GBPUSD_HTF_BIAS_BEARISH", AlertSeverity.WARNING, now=_NOW,
    )

    prev = make_state(
        htf_bias="BULLISH",
        structure_mode="TREND_CONTINUATION",
    )
    curr = make_state(
        htf_bias="BEARISH",
        structure_mode="VOLATILE_SWEEP_ZONE",
    )
    events = process_structure_alerts(
        prev=prev, curr=curr, dedupe=dedupe, now=_NOW + timedelta(seconds=1),
    )
    kinds = [e.kind for e in events]
    # HTF_BIAS_CHANGE blocked, STRUCTURE_MODE_CHANGE allowed.
    assert kinds == [AlertEventKind.STRUCTURE_MODE_CHANGE]


# ---------------------------------------------------------------------------
# Event timestamp threading
# ---------------------------------------------------------------------------


def test_event_timestamps_set_from_now_parameter() -> None:
    dedupe = DedupeCache()
    prev = make_state(htf_bias="BULLISH")
    curr = make_state(htf_bias="BEARISH")
    distinct_now = datetime(2026, 5, 16, 14, 0, tzinfo=timezone.utc)
    events = process_structure_alerts(
        prev=prev, curr=curr, dedupe=dedupe, now=distinct_now,
    )
    assert len(events) == 1
    assert events[0].timestamp == distinct_now


# ---------------------------------------------------------------------------
# Persistence is NOT a processor concern
# ---------------------------------------------------------------------------


def test_processor_does_not_persist_to_disk(tmp_path) -> None:
    """The processor returns events but does not write jsonl.
    BotLoop (C-6) owns the persistence call. This is testable by
    asserting tmp_path is untouched after a normal processor call."""
    dedupe = DedupeCache()
    prev = make_state(htf_bias="BULLISH")
    curr = make_state(htf_bias="BEARISH")
    events = process_structure_alerts(
        prev=prev, curr=curr, dedupe=dedupe, now=_NOW,
    )
    assert len(events) == 1
    # tmp_path was not touched — processor has no jsonl side effect.
    assert list(tmp_path.iterdir()) == []
