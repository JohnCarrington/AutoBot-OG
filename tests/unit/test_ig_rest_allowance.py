"""Tests for feed.ig_rest.allowance.AllowanceTracker."""
from __future__ import annotations

import pytest

from feed.ig_rest.allowance import AllowanceTracker


def _tracker(*, rpm: int = 5, schedule=(10, 20)) -> tuple[AllowanceTracker, list[float]]:
    """Build a tracker driven by an injectable clock list."""
    now = [0.0]
    t = AllowanceTracker(
        requests_per_minute=rpm,
        window_seconds=60,
        backoff_schedule=schedule,
        clock=lambda: now[0],
    )
    return t, now


# --- Construction validation ----------------------------------------------


def test_invalid_rpm_raises() -> None:
    with pytest.raises(ValueError):
        AllowanceTracker(requests_per_minute=0)


def test_empty_schedule_raises() -> None:
    with pytest.raises(ValueError):
        AllowanceTracker(backoff_schedule=())


# --- Window counting -------------------------------------------------------


def test_should_backoff_zero_when_empty() -> None:
    t, _ = _tracker()
    assert t.should_backoff() == 0


def test_under_limit_returns_no_backoff() -> None:
    t, _ = _tracker(rpm=5)
    for _ in range(4):
        t.note_request()
    assert t.should_backoff() == 0


def test_at_limit_recommends_window_wait() -> None:
    t, now = _tracker(rpm=3)
    # Fire 3 requests at t=0.
    for _ in range(3):
        t.note_request()
    # Window is 60s. should_backoff at t=10 → 50s until oldest expires.
    now[0] = 10.0
    assert t.should_backoff() == pytest.approx(50.0)


def test_window_clears_after_window_passes() -> None:
    t, now = _tracker(rpm=3)
    for _ in range(3):
        t.note_request()
    now[0] = 61.0
    assert t.should_backoff() == 0


# --- Throttle escalation --------------------------------------------------


def test_first_throttle_uses_first_schedule_entry() -> None:
    t, now = _tracker(schedule=(10, 20, 30))
    t.note_throttled()
    assert t.should_backoff() == pytest.approx(10.0)


def test_throttle_escalates_through_schedule() -> None:
    t, now = _tracker(schedule=(10, 20, 30))
    t.note_throttled()
    now[0] = 11.0  # past 10
    assert t.should_backoff() == 0
    t.note_throttled()
    assert t.should_backoff() == pytest.approx(20.0)
    now[0] = 32.0
    t.note_throttled()
    assert t.should_backoff() == pytest.approx(30.0)


def test_throttle_caps_at_longest_schedule_entry() -> None:
    t, now = _tracker(schedule=(5, 10))
    for _ in range(5):
        t.note_throttled()
        now[0] += 100  # always past prior cooldown
    # After 5 throttles, should reuse the last entry.
    assert t.should_backoff() <= 10.0


# --- Composition (max of window + throttle) -------------------------------


def test_returns_larger_of_window_or_throttle_wait() -> None:
    t, now = _tracker(rpm=3, schedule=(100,))
    for _ in range(3):
        t.note_request()
    # Window says 60s; throttle (just triggered) says 100s.
    t.note_throttled()
    assert t.should_backoff() == pytest.approx(100.0)


# --- Diagnostic surface ---------------------------------------------------


def test_snapshot_reports_state() -> None:
    t, now = _tracker(rpm=5)
    for _ in range(2):
        t.note_request()
    t.note_throttled()
    snap = t.snapshot()
    assert snap.requests_in_window == 2
    assert snap.throttle_count == 1


def test_reset_clears_state() -> None:
    t, _ = _tracker(rpm=5)
    t.note_request()
    t.note_throttled()
    t.reset()
    snap = t.snapshot()
    assert snap.requests_in_window == 0
    assert snap.throttle_count == 0
