"""Tests for ``day_type.classify_day_type``.

Mirrors the news_calendar test fixture pattern: each test seeds the
module-level cache via ``_inject_events_for_tests``, the autouse
fixture cleans up before/after.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from day_type import DayType, classify_day_type
from risk.news_calendar.calendar import (
    CACHE_STALENESS_THRESHOLD_SECS,
    _force_cache_age_for_tests,
    _inject_events_for_tests,
    _reset_cache_for_tests,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    _reset_cache_for_tests()
    yield
    _reset_cache_for_tests()


def _high(
    *,
    country: str = "US",
    event: str = "FOMC",
    time: str = "2026-05-14 12:00:00",
) -> dict:
    return {
        "country": country,
        "event": event,
        "impact": "high",
        "time": time,
    }


def _medium(
    *,
    country: str = "US",
    event: str = "Retail Sales",
    time: str = "2026-05-14 12:00:00",
) -> dict:
    return {
        "country": country,
        "event": event,
        "impact": "medium",
        "time": time,
    }


# --- BIG_NEWS_DAY -----------------------------------------------------------


def test_high_release_today_for_currency_returns_big_news_day() -> None:
    """US HIGH at 12:00 UTC on 2026-05-14 falls inside the NY session
    ending Thu 17:00 NY (= 2026-05-14 session). Query at 10:00 UTC same
    day sits in the same session → BIG_NEWS_DAY."""
    _inject_events_for_tests([_high(country="US", time="2026-05-14 12:00:00")])
    now = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    assert classify_day_type(now_utc=now, currencies=["USD"]) == DayType.BIG_NEWS_DAY


def test_big_news_day_recognised_via_either_pair_currency() -> None:
    """Currency match is the union over the pair's currency set —
    a GB event qualifies a GBPUSD query even if no USD event exists."""
    _inject_events_for_tests([_high(country="GB", event="BoE Decision",
                                    time="2026-05-14 11:00:00")])
    now = datetime(2026, 5, 14, 9, 0, tzinfo=timezone.utc)
    assert classify_day_type(
        now_utc=now, currencies=["GBP", "USD"]
    ) == DayType.BIG_NEWS_DAY


# --- PRE_BIG_NEWS -----------------------------------------------------------


def test_no_high_today_but_high_tomorrow_returns_pre_big_news() -> None:
    """Calm current session (no HIGH events), HIGH in tomorrow's session
    inside the 24h default lookahead.

    Now = 2026-05-14 16:00 UTC (noon EDT) → current session label =
    2026-05-14 (session ends 17:00 NY = 21:00 UTC). Event at
    2026-05-15 12:00 UTC = 20h ahead, inside the default 24h lookahead,
    and in tomorrow's session (label 2026-05-15)."""
    _inject_events_for_tests([_high(country="US", time="2026-05-15 12:00:00")])
    now = datetime(2026, 5, 14, 16, 0, tzinfo=timezone.utc)
    assert classify_day_type(now_utc=now, currencies=["USD"]) == DayType.PRE_BIG_NEWS


# --- NORMAL -----------------------------------------------------------------


def test_high_today_but_for_different_currency_returns_normal() -> None:
    """Currency filter works: US HIGH does NOT light up a GBP-only query."""
    _inject_events_for_tests([_high(country="US", time="2026-05-14 12:00:00")])
    now = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    assert classify_day_type(now_utc=now, currencies=["GBP"]) == DayType.NORMAL


def test_nothing_in_window_returns_normal() -> None:
    """Empty cache (fresh fetch returned nothing matching) → NORMAL."""
    _inject_events_for_tests([])
    now = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    assert classify_day_type(
        now_utc=now, currencies=["USD", "GBP"]
    ) == DayType.NORMAL


def test_medium_only_returns_normal_with_default_impact_floor() -> None:
    """The classifier asks events_in_window for HIGH+; MEDIUMs alone
    do not flip BIG_NEWS_DAY or PRE_BIG_NEWS."""
    _inject_events_for_tests([
        _medium(country="US", time="2026-05-14 12:00:00"),
        _medium(country="US", time="2026-05-15 12:00:00"),
    ])
    now = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    assert classify_day_type(now_utc=now, currencies=["USD"]) == DayType.NORMAL


# --- Stale cache (fail-closed) ---------------------------------------------


def test_stale_cache_returns_big_news_day_fail_closed() -> None:
    """Parity with is_blackout's cache-stale fail-closed: when we don't
    know what the calendar contains, assume the worst."""
    _inject_events_for_tests([])
    _force_cache_age_for_tests(CACHE_STALENESS_THRESHOLD_SECS + 10)
    now = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    assert classify_day_type(now_utc=now, currencies=["USD"]) == DayType.BIG_NEWS_DAY


def test_cold_cache_never_fetched_fails_closed() -> None:
    """Cold cache (no successful fetch) → infinite staleness → BIG."""
    _reset_cache_for_tests()
    now = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    assert classify_day_type(now_utc=now, currencies=["USD"]) == DayType.BIG_NEWS_DAY


# --- NY-day-boundary regression --------------------------------------------


def test_ny_session_boundary_high_at_23_30_utc_classified_by_ny_session() -> None:
    """Event at 2026-06-15 23:30 UTC = 19:30 EDT → after 17:00 NY close
    → falls into the NY session ending 2026-06-16 17:00 NY.

    Query now_utc = 2026-06-16 02:00 UTC = 22:00 EDT 2026-06-15 → also
    after 17:00 NY close → same NY session label (2026-06-16).

    A naive UTC-calendar-day impl would put the event on 2026-06-15 and
    the query on 2026-06-16 — different days → miss → NORMAL. The
    NY-session-anchored impl puts both in the SAME session → BIG."""
    _inject_events_for_tests([_high(country="US", time="2026-06-15 23:30:00")])
    now = datetime(2026, 6, 16, 2, 0, tzinfo=timezone.utc)
    assert classify_day_type(now_utc=now, currencies=["USD"]) == DayType.BIG_NEWS_DAY


def test_ny_session_boundary_high_just_before_17_ny_belongs_to_today() -> None:
    """An event at 16:30 EDT = 20:30 UTC on 2026-06-15 is BEFORE the
    NY 17:00 close → still part of the session ending 2026-06-15 17:00 NY.

    Query at 14:00 UTC same day (10:00 EDT) is before 17:00 NY → same
    session (label 2026-06-15). Event 20:30 UTC same day is also before
    17:00 NY (16:30 EDT) → same session. → BIG_NEWS_DAY."""
    _inject_events_for_tests([_high(country="US", time="2026-06-15 20:30:00")])
    now = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    assert classify_day_type(now_utc=now, currencies=["USD"]) == DayType.BIG_NEWS_DAY


# --- Lookahead boundary -----------------------------------------------------


def test_lookahead_just_inside_default_24h_returns_pre_big_news() -> None:
    """A HIGH event 23h ahead of `now` sits inside the default 24h
    lookahead — and (because current session has no HIGH) is the
    qualifying PRE_BIG_NEWS hit."""
    now = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    event_time = now + timedelta(hours=23)
    _inject_events_for_tests([
        _high(country="US", time=event_time.strftime("%Y-%m-%d %H:%M:%S"))
    ])
    assert classify_day_type(now_utc=now, currencies=["USD"]) == DayType.PRE_BIG_NEWS


def test_lookahead_just_outside_default_24h_returns_normal() -> None:
    """An event 25h ahead is past the 24h default lookahead → NORMAL."""
    now = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    event_time = now + timedelta(hours=25)
    _inject_events_for_tests([
        _high(country="US", time=event_time.strftime("%Y-%m-%d %H:%M:%S"))
    ])
    assert classify_day_type(now_utc=now, currencies=["USD"]) == DayType.NORMAL


def test_lookahead_horizon_is_tunable() -> None:
    """Same event, two different lookaheads → two different verdicts."""
    now = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    event_time = now + timedelta(hours=36)
    _inject_events_for_tests([
        _high(country="US", time=event_time.strftime("%Y-%m-%d %H:%M:%S"))
    ])
    assert classify_day_type(
        now_utc=now, currencies=["USD"], pre_big_news_lookahead_hours=24,
    ) == DayType.NORMAL
    assert classify_day_type(
        now_utc=now, currencies=["USD"], pre_big_news_lookahead_hours=48,
    ) == DayType.PRE_BIG_NEWS
