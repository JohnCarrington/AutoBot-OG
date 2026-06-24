"""Tests for strategies.news_window.is_in_release_window (step 5b).

The predicate that decides whether ``detect_news`` runs vs the
structure detectors on a ``BIG_NEWS_DAY``. Window is asymmetric:
``[release - 15, release + 30]`` (defaults; env-overridable).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from risk.news_calendar.calendar import (
    _inject_events_for_tests,
    _reset_cache_for_tests,
)
from strategies.news_window import (
    NEWS_WINDOW_POST_MIN,
    NEWS_WINDOW_PRE_MIN,
    is_in_release_window,
)


_RELEASE = datetime(2026, 6, 25, 12, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean_cache():
    _reset_cache_for_tests()
    yield
    _reset_cache_for_tests()


def _ev(*, country: str = "US", impact: str = "high",
        time_str: str = "2026-06-25 12:30:00") -> dict:
    return {
        "country": country,
        "event": "CPI YoY",
        "time": time_str,
        "actual": 3.30,
        "estimate": 3.00,
        "impact": impact,
        "prev": 3.10,
    }


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_defaults_are_15_pre_30_post() -> None:
    assert NEWS_WINDOW_PRE_MIN == 15
    assert NEWS_WINDOW_POST_MIN == 30


# ---------------------------------------------------------------------------
# Window membership
# ---------------------------------------------------------------------------


def test_inside_window_at_release_minus_5() -> None:
    _inject_events_for_tests([_ev()])
    assert is_in_release_window(
        _RELEASE - timedelta(minutes=5), ("USD", "GBP"),
    )


def test_inside_window_at_release_exactly() -> None:
    _inject_events_for_tests([_ev()])
    assert is_in_release_window(_RELEASE, ("USD", "GBP"))


def test_inside_window_at_release_plus_20() -> None:
    _inject_events_for_tests([_ev()])
    assert is_in_release_window(
        _RELEASE + timedelta(minutes=20), ("USD", "GBP"),
    )


def test_boundary_at_release_minus_15_is_inside() -> None:
    """Pre boundary is inclusive."""
    _inject_events_for_tests([_ev()])
    assert is_in_release_window(
        _RELEASE - timedelta(minutes=15), ("USD", "GBP"),
    )


def test_boundary_at_release_plus_30_is_inside() -> None:
    """Post boundary is inclusive."""
    _inject_events_for_tests([_ev()])
    assert is_in_release_window(
        _RELEASE + timedelta(minutes=30), ("USD", "GBP"),
    )


def test_outside_window_before() -> None:
    _inject_events_for_tests([_ev()])
    assert not is_in_release_window(
        _RELEASE - timedelta(minutes=16), ("USD", "GBP"),
    )


def test_outside_window_after() -> None:
    _inject_events_for_tests([_ev()])
    assert not is_in_release_window(
        _RELEASE + timedelta(minutes=31), ("USD", "GBP"),
    )


# ---------------------------------------------------------------------------
# Cache empty
# ---------------------------------------------------------------------------


def test_empty_cache_returns_false() -> None:
    """No events at all → not in any window."""
    _inject_events_for_tests([])
    assert not is_in_release_window(_RELEASE, ("USD", "GBP"))


# ---------------------------------------------------------------------------
# Currency gating
# ---------------------------------------------------------------------------


def test_different_currency_release_does_not_gate() -> None:
    """A JPY release should not put a GBPUSD pair in-window."""
    _inject_events_for_tests([_ev(country="JP")])
    assert not is_in_release_window(_RELEASE, ("USD", "GBP"))


def test_only_one_currency_in_pair_matches() -> None:
    """A US release gates any pair containing USD."""
    _inject_events_for_tests([_ev(country="US")])
    assert is_in_release_window(_RELEASE, ("USD", "GBP"))
    assert is_in_release_window(_RELEASE, ("USD", "JPY"))
    assert not is_in_release_window(_RELEASE, ("GBP", "JPY"))


# ---------------------------------------------------------------------------
# Impact gating
# ---------------------------------------------------------------------------


def test_medium_impact_release_does_not_gate() -> None:
    """Only HIGH-impact releases drive the suppression window."""
    _inject_events_for_tests([_ev(impact="medium")])
    assert not is_in_release_window(_RELEASE, ("USD", "GBP"))


# ---------------------------------------------------------------------------
# Multiple releases — union
# ---------------------------------------------------------------------------


def test_multiple_releases_union_membership() -> None:
    """Inside either window → inside the union."""
    ev_early = _ev(time_str="2026-06-25 08:00:00")
    ev_late = _ev(time_str="2026-06-25 12:30:00")
    _inject_events_for_tests([ev_early, ev_late])
    # Inside ev_late's window only.
    assert is_in_release_window(_RELEASE, ("USD", "GBP"))
    # Inside ev_early's window only.
    assert is_in_release_window(
        datetime(2026, 6, 25, 8, 10, tzinfo=timezone.utc),
        ("USD", "GBP"),
    )
    # Between the two — outside both.
    assert not is_in_release_window(
        datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc),
        ("USD", "GBP"),
    )


# ---------------------------------------------------------------------------
# Custom window bounds
# ---------------------------------------------------------------------------


def test_custom_window_bounds_override_defaults() -> None:
    """``pre_min``/``post_min`` kwargs override the defaults so the
    dispatcher can tune per-call without env juggling."""
    _inject_events_for_tests([_ev()])
    # Default (15 pre / 30 post) would put this outside.
    assert not is_in_release_window(
        _RELEASE + timedelta(minutes=45), ("USD", "GBP"),
    )
    # Widen post to 60 → now inside.
    assert is_in_release_window(
        _RELEASE + timedelta(minutes=45), ("USD", "GBP"),
        pre_min=15, post_min=60,
    )


# ---------------------------------------------------------------------------
# tz-naive input
# ---------------------------------------------------------------------------


def test_naive_now_utc_treated_as_utc() -> None:
    _inject_events_for_tests([_ev()])
    naive = _RELEASE.replace(tzinfo=None)
    assert is_in_release_window(naive, ("USD", "GBP"))
