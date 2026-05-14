"""Tests for DST-aware session predicates."""
from __future__ import annotations

from datetime import datetime, timezone

from strategies.sessions import london_ny_overlap, london_session, ny_session


# --- London session ---------------------------------------------------------


def test_london_open_summer() -> None:
    # 2025-05-14 Wed, 07:00 BST = 06:00 UTC.
    now = datetime(2025, 5, 14, 6, 0, tzinfo=timezone.utc)
    assert london_session(now) is True


def test_london_just_before_open_returns_false() -> None:
    # 06:59 BST = 05:59 UTC — one minute before open.
    now = datetime(2025, 5, 14, 5, 59, tzinfo=timezone.utc)
    assert london_session(now) is False


def test_london_close_summer_is_exclusive() -> None:
    # 15:00 BST = 14:00 UTC — exact close, predicate is end-exclusive.
    now = datetime(2025, 5, 14, 14, 0, tzinfo=timezone.utc)
    assert london_session(now) is False


def test_london_dst_winter() -> None:
    # 2025-01-15 Wed, 07:00 GMT = 07:00 UTC.
    now = datetime(2025, 1, 15, 7, 0, tzinfo=timezone.utc)
    assert london_session(now) is True


# --- NY session -------------------------------------------------------------


def test_ny_open_summer() -> None:
    # 2025-05-14 Wed, 08:00 EDT = 12:00 UTC.
    now = datetime(2025, 5, 14, 12, 0, tzinfo=timezone.utc)
    assert ny_session(now) is True


def test_ny_close_summer_is_exclusive() -> None:
    # 17:00 EDT = 21:00 UTC.
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    assert ny_session(now) is False


def test_ny_dst_winter() -> None:
    # 2025-01-15 Wed, 08:00 EST = 13:00 UTC.
    now = datetime(2025, 1, 15, 13, 0, tzinfo=timezone.utc)
    assert ny_session(now) is True


# --- Overlap ----------------------------------------------------------------


def test_overlap_is_intersection() -> None:
    # 2025-05-14 Wed, 12:00 UTC = 13:00 BST + 08:00 EDT → both open.
    now = datetime(2025, 5, 14, 12, 0, tzinfo=timezone.utc)
    assert london_session(now) is True
    assert ny_session(now) is True
    assert london_ny_overlap(now) is True


def test_overlap_false_outside_intersection() -> None:
    # 06:30 UTC: London not open yet, NY not open.
    now = datetime(2025, 5, 14, 6, 30, tzinfo=timezone.utc)
    assert london_ny_overlap(now) is False


# --- Asia / out-of-session --------------------------------------------------


def test_asia_session_returns_false_for_both() -> None:
    # 03:00 UTC — Tokyo open, but neither London nor NY active.
    now = datetime(2025, 5, 14, 3, 0, tzinfo=timezone.utc)
    assert london_session(now) is False
    assert ny_session(now) is False


# --- Naive datetime ---------------------------------------------------------


def test_naive_datetime_treated_as_utc() -> None:
    # Mirror test_ny_open_summer with a naive datetime.
    now = datetime(2025, 5, 14, 12, 0)  # naive
    assert ny_session(now) is True


# --- Spring-forward edge (London DST transition) ---------------------------


def test_london_spring_forward_2025() -> None:
    # 2025-03-30 BST starts at 01:00 UTC (clocks jump 01:00 GMT → 02:00 BST).
    # 06:30 UTC = 07:30 BST → in session.
    now = datetime(2025, 3, 30, 6, 30, tzinfo=timezone.utc)
    assert london_session(now) is True
