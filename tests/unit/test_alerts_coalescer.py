"""Tests for alerts.coalescer — windowed grouping semantics."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from alerts.coalescer import AlertCoalescer
from alerts.types import Alert, AlertCategory, AlertSeverity


_NOW0 = datetime(2026, 5, 15, 13, 0, tzinfo=timezone.utc)


def _alert(
    *,
    pair: str = "GBPUSD",
    event_subtype: str = "TRADE_OPENED",
    category: AlertCategory = AlertCategory.TRADE,
    severity: AlertSeverity = AlertSeverity.INFO,
    short_text: str = "bullish",
) -> Alert:
    return Alert(
        category=category,
        event_subtype=event_subtype,
        severity=severity,
        pair=pair,
        full_text=f"{pair} {short_text}",
        short_text=short_text,
        timestamp=_NOW0,
    )


def _coalescer(clock_box: list, *, window: int = 30) -> AlertCoalescer:
    return AlertCoalescer(window_seconds=window, clock=lambda: clock_box[0])


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_rejects_non_positive_window() -> None:
    with pytest.raises(ValueError):
        AlertCoalescer(window_seconds=0)


def test_initial_pending_count_is_zero() -> None:
    c = _coalescer([_NOW0])
    assert c.pending_count == 0


# ---------------------------------------------------------------------------
# Within-window collapse
# ---------------------------------------------------------------------------


def test_first_non_critical_alert_is_held_back() -> None:
    box = [_NOW0]
    c = _coalescer(box)
    batches = c.add(_alert())
    assert batches == []  # nothing sent yet
    assert c.pending_count == 1


def test_multiple_same_key_within_window_collapse_into_one_pending_group() -> None:
    box = [_NOW0]
    c = _coalescer(box)
    c.add(_alert(short_text="a"))
    box[0] = _NOW0 + timedelta(seconds=10)
    c.add(_alert(short_text="b"))
    box[0] = _NOW0 + timedelta(seconds=20)
    c.add(_alert(short_text="c"))
    assert c.pending_count == 3
    # No sends fired yet — window not elapsed for any key.


def test_post_window_send_flushes_previous_group_and_starts_new() -> None:
    box = [_NOW0]
    c = _coalescer(box)
    c.add(_alert(short_text="a"))
    # Advance past window.
    box[0] = _NOW0 + timedelta(seconds=35)
    batches = c.add(_alert(short_text="b"))
    assert len(batches) == 1
    # The flushed batch is the original alert.
    assert len(batches[0]) == 1
    assert batches[0][0].short_text == "a"
    # The new alert is now pending.
    assert c.pending_count == 1


# ---------------------------------------------------------------------------
# Pair as part of the coalesce key (Phase 9 plan refinement #1)
# ---------------------------------------------------------------------------


def test_different_pairs_do_not_coalesce() -> None:
    box = [_NOW0]
    c = _coalescer(box)
    c.add(_alert(pair="GBPUSD"))
    box[0] = _NOW0 + timedelta(seconds=10)
    c.add(_alert(pair="EURUSD"))
    # Two distinct pending groups.
    assert c.pending_count == 2


def test_other_keys_dont_flush_each_other_within_window() -> None:
    box = [_NOW0]
    c = _coalescer(box)
    c.add(_alert(pair="GBPUSD"))
    box[0] = _NOW0 + timedelta(seconds=10)
    batches = c.add(_alert(pair="EURUSD"))
    assert batches == []  # neither key's window has elapsed


# ---------------------------------------------------------------------------
# CRITICAL bypass
# ---------------------------------------------------------------------------


def test_critical_alert_bypasses_coalescing() -> None:
    box = [_NOW0]
    c = _coalescer(box)
    batches = c.add(_alert(
        severity=AlertSeverity.CRITICAL,
        event_subtype="FAILURE_THRESHOLD_TRIPPED",
        category=AlertCategory.SYSTEM,
        pair=None,
    ))
    assert len(batches) == 1
    assert batches[0][0].severity is AlertSeverity.CRITICAL


def test_critical_flushes_pending_same_key_non_critical_first() -> None:
    """CRITICAL preserves timeline by flushing pending of same key first."""
    box = [_NOW0]
    c = _coalescer(box)
    # Three non-critical alerts buffered.
    c.add(_alert(severity=AlertSeverity.WARNING, event_subtype="FEED_STALE",
                 category=AlertCategory.SYSTEM, pair=None, short_text="stale 1"))
    box[0] = _NOW0 + timedelta(seconds=5)
    c.add(_alert(severity=AlertSeverity.WARNING, event_subtype="FEED_STALE",
                 category=AlertCategory.SYSTEM, pair=None, short_text="stale 2"))
    # CRITICAL for the SAME key — pair=None, event_subtype=FEED_STALE.
    # (Realistically severities differ between subtypes; this test
    # forces the bypass logic with a contrived setup.)
    box[0] = _NOW0 + timedelta(seconds=10)
    batches = c.add(_alert(severity=AlertSeverity.CRITICAL,
                           event_subtype="FEED_STALE",
                           category=AlertCategory.SYSTEM, pair=None,
                           short_text="stale CRIT"))
    # Two batches: the pending pair flushed, then the critical on its own.
    assert len(batches) == 2
    assert len(batches[0]) == 2  # the pending pair
    assert batches[0][0].short_text == "stale 1"
    assert batches[0][1].short_text == "stale 2"
    assert batches[1][0].severity is AlertSeverity.CRITICAL


def test_critical_with_no_pending_same_key_sends_alone() -> None:
    box = [_NOW0]
    c = _coalescer(box)
    batches = c.add(_alert(
        severity=AlertSeverity.CRITICAL,
        event_subtype="FAILURE_THRESHOLD_TRIPPED",
        category=AlertCategory.SYSTEM,
        pair=None,
    ))
    assert len(batches) == 1
    assert len(batches[0]) == 1


# ---------------------------------------------------------------------------
# Side effect: every add() drains other keys' elapsed windows
# ---------------------------------------------------------------------------


def test_add_drains_elapsed_other_keys() -> None:
    box = [_NOW0]
    c = _coalescer(box)
    c.add(_alert(pair="GBPUSD", short_text="gbp"))
    # Advance past window; now an EURUSD alert arrives.
    box[0] = _NOW0 + timedelta(seconds=35)
    batches = c.add(_alert(pair="EURUSD", short_text="eur"))
    # GBPUSD's elapsed group flushed even though add() concerns EURUSD.
    assert len(batches) == 1
    assert batches[0][0].pair == "GBPUSD"
    # EURUSD is now pending.
    assert c.pending_count == 1


# ---------------------------------------------------------------------------
# tick()
# ---------------------------------------------------------------------------


def test_tick_with_no_pending_returns_empty() -> None:
    c = _coalescer([_NOW0])
    assert c.tick() == []


def test_tick_drains_elapsed_pending_groups() -> None:
    box = [_NOW0]
    c = _coalescer(box)
    c.add(_alert(pair="GBPUSD"))
    c.add(_alert(pair="EURUSD"))
    # Advance past window.
    box[0] = _NOW0 + timedelta(seconds=40)
    batches = c.tick()
    assert len(batches) == 2
    # Pending state cleared.
    assert c.pending_count == 0


def test_tick_leaves_in_window_groups_pending() -> None:
    box = [_NOW0]
    c = _coalescer(box)
    c.add(_alert(pair="GBPUSD"))
    box[0] = _NOW0 + timedelta(seconds=15)  # still within window
    batches = c.tick()
    assert batches == []
    assert c.pending_count == 1


# ---------------------------------------------------------------------------
# drain_all()
# ---------------------------------------------------------------------------


def test_drain_all_flushes_regardless_of_window_age() -> None:
    box = [_NOW0]
    c = _coalescer(box)
    c.add(_alert(pair="GBPUSD"))
    c.add(_alert(pair="EURUSD"))
    # Within window — tick() wouldn't drain.
    batches = c.drain_all()
    assert len(batches) == 2
    assert c.pending_count == 0


def test_drain_all_when_empty_returns_empty() -> None:
    c = _coalescer([_NOW0])
    assert c.drain_all() == []


# ---------------------------------------------------------------------------
# Exact window-boundary behaviour
# ---------------------------------------------------------------------------


def test_window_boundary_at_exact_elapsed_seconds_flushes() -> None:
    """`>=` semantics: exactly window_seconds after first arrival flushes."""
    box = [_NOW0]
    c = _coalescer(box, window=30)
    c.add(_alert())
    box[0] = _NOW0 + timedelta(seconds=30)
    batches = c.tick()
    assert len(batches) == 1


def test_one_second_short_of_window_does_not_flush() -> None:
    box = [_NOW0]
    c = _coalescer(box, window=30)
    c.add(_alert())
    box[0] = _NOW0 + timedelta(seconds=29)
    batches = c.tick()
    assert batches == []
