"""Tests for risk.news_calendar.calendar.

Covers seven review items:

- H4: fetch failure preserves the cache; stale cache fails ``is_blackout``
      closed.
- H5: ``beat_miss`` is identical between the Finnhub-surprise and the
      locally-computed branches for the same input.
- H6: Finnhub ``surprise`` values outside ``[-1.0, 1.0]`` are rejected
      (treated as absolute-units rather than fractional) and we fall back
      to the computed deviation.
- H7: ``is_blackout`` public surface — HIGH/MEDIUM in-window vs out-of-window,
      unknown currency, no events.
- N1: ``parse_event_time`` accepts space-separated, ISO-8601 (T-separator,
      ``Z`` suffix, ±offset), Unix int/float/string; logs WARNING on
      unparseable input.
- M2: ``get_actual_for_event`` result dict carries the original Finnhub
      ``time`` string as ``event_time``.
- M3: ``is_blackout`` walks the cache by time window, not by date string —
      events around the UTC date boundary are evaluated correctly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from risk.news_calendar import (
    BlackoutResult,
    cache_staleness_seconds,
    get_actual_for_event,
    is_blackout,
    poll_for_actual,
)
from risk.news_calendar import calendar as cal_mod
from risk.news_calendar.calendar import (
    _force_cache_age_for_tests,
    _inject_events_for_tests,
    parse_event_time,
    _reset_cache_for_tests,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    _reset_cache_for_tests()
    yield
    _reset_cache_for_tests()


# Fixed reference time used across tests. UTC-aware.
T0 = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)


def _event(
    *,
    country: str = "GB",
    event: str = "BoE Interest Rate Decision",
    impact: str = "high",
    time: str = "2026-05-14 12:00:00",
    actual=None,
    estimate=None,
    surprise=None,
):
    ev = {"country": country, "event": event, "impact": impact, "time": time}
    if actual is not None:
        ev["actual"] = actual
    if estimate is not None:
        ev["estimate"] = estimate
    if surprise is not None:
        ev["surprise"] = surprise
    return ev


# ===========================================================================
# H4 — failure preserves cache; stale cache fails is_blackout closed
# ===========================================================================


def test_h4_finnhub_failure_preserves_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-populate the cache, force fetch_calendar() to fail (return None),
    confirm the cache still holds the previously good data after polling."""
    seed = _event(actual=5.25, estimate=5.25)
    _inject_events_for_tests([seed])

    # Sanity: cache is populated.
    assert len(cal_mod._cache["events"]) == 1

    # Force fetch failure. Enable Finnhub so poll_for_actual reaches the fetch.
    monkeypatch.setattr("risk.news_calendar.calendar.FINNHUB_ENABLED", True)
    monkeypatch.setattr("risk.news_calendar.calendar.fetch_calendar", lambda: None)

    poll_for_actual(min_interval=0)

    # Cache must STILL hold the seed event.
    assert len(cal_mod._cache["events"]) == 1
    assert cal_mod._cache["events"][0] is seed


def test_h4_finnhub_success_replaces_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """The opposite path: a successful fetch returning [] (legitimately
    empty calendar window) DOES clear the cache. Only failure preserves."""
    _inject_events_for_tests([_event()])
    assert len(cal_mod._cache["events"]) == 1

    monkeypatch.setattr("risk.news_calendar.calendar.FINNHUB_ENABLED", True)
    monkeypatch.setattr("risk.news_calendar.calendar.fetch_calendar", lambda: [])

    poll_for_actual(min_interval=0)

    assert cal_mod._cache["events"] == []


def test_h4_failure_does_not_advance_last_successful_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``last_successful_fetch`` must not advance on failure — that
    timestamp is what drives the staleness check."""
    _inject_events_for_tests([_event()])
    last_good = cal_mod._cache["last_successful_fetch"]

    monkeypatch.setattr("risk.news_calendar.calendar.FINNHUB_ENABLED", True)
    monkeypatch.setattr("risk.news_calendar.calendar.fetch_calendar", lambda: None)
    poll_for_actual(min_interval=0)

    assert cal_mod._cache["last_successful_fetch"] == last_good


def test_h4_is_blackout_stale_cache_fails_closed() -> None:
    """If the cache is older than CACHE_STALENESS_THRESHOLD_SECS,
    is_blackout must report blocked with reason ``cache-stale``."""
    _inject_events_for_tests([])  # cache age is now ~0
    # Age it past the threshold.
    _force_cache_age_for_tests(cal_mod.CACHE_STALENESS_THRESHOLD_SECS + 10)

    r = is_blackout("GBP", T0)
    assert r.is_blocked is True
    assert r.reason == "cache-stale"
    assert r.confidence == "low"


def test_h4_is_blackout_never_fetched_fails_closed() -> None:
    """A cold cache (no successful fetch yet) is infinite-staleness and
    must fail closed."""
    _reset_cache_for_tests()
    assert cache_staleness_seconds() == float("inf")
    r = is_blackout("GBP", T0)
    assert r.is_blocked is True
    assert r.reason == "cache-stale"


# ===========================================================================
# H5 — beat_miss is identical between Finnhub-surprise and computed paths
# ===========================================================================


@pytest.mark.parametrize(
    "deviation,expected_label,expected_hint",
    [
        (0.10,  "BEAT",    "CONTINUATION"),
        (-0.10, "MISS",    "CONTINUATION"),
        (0.02,  "IN_LINE", "REVERSAL"),
        (-0.02, "IN_LINE", "REVERSAL"),
        (0.00,  "IN_LINE", "REVERSAL"),
    ],
)
def test_h5_finnhub_branch_classification(
    deviation: float, expected_label: str, expected_hint: str,
) -> None:
    """Finnhub-supplied surprise must classify via the same
    threshold-gated logic as the computed branch."""
    # actual/estimate chosen so deviation matches `deviation` exactly.
    actual = 100.0 * (1.0 + deviation)
    estimate = 100.0
    ev = _event(actual=actual, estimate=estimate, surprise=deviation)
    _inject_events_for_tests([ev])

    info = get_actual_for_event("BoE Interest Rate Decision", currency="GBP")
    assert info is not None
    assert info["surprise_source"] == "finnhub"
    assert info["beat_miss"] == expected_label
    assert info["direction_hint"] == expected_hint


@pytest.mark.parametrize(
    "deviation,expected_label,expected_hint",
    [
        (0.10,  "BEAT",    "CONTINUATION"),
        (-0.10, "MISS",    "CONTINUATION"),
        (0.02,  "IN_LINE", "REVERSAL"),
        (-0.02, "IN_LINE", "REVERSAL"),
    ],
)
def test_h5_computed_branch_classification(
    deviation: float, expected_label: str, expected_hint: str,
) -> None:
    """When Finnhub does not provide ``surprise``, the computed branch
    must produce the same label set."""
    actual = 100.0 * (1.0 + deviation)
    estimate = 100.0
    ev = _event(actual=actual, estimate=estimate)  # no surprise field
    _inject_events_for_tests([ev])

    info = get_actual_for_event("BoE Interest Rate Decision", currency="GBP")
    assert info is not None
    assert info["surprise_source"] == "computed"
    assert info["beat_miss"] == expected_label
    assert info["direction_hint"] == expected_hint


def test_h5_same_input_same_output_across_branches() -> None:
    """The review's H5 probe: an input that would have produced
    BEAT in the legacy Finnhub branch but IN_LINE in the computed
    branch must now produce the SAME label."""
    # 2% surprise — within ±5% threshold → IN_LINE in both branches.
    ev_with_fh = _event(actual=102.0, estimate=100.0, surprise=0.02)
    ev_no_fh = _event(actual=102.0, estimate=100.0)  # no surprise field

    _inject_events_for_tests([ev_with_fh])
    info_fh = get_actual_for_event("BoE Interest Rate Decision", currency="GBP")

    _inject_events_for_tests([ev_no_fh])
    info_computed = get_actual_for_event("BoE Interest Rate Decision", currency="GBP")

    assert info_fh is not None and info_computed is not None
    assert info_fh["beat_miss"] == info_computed["beat_miss"] == "IN_LINE"
    assert info_fh["direction_hint"] == info_computed["direction_hint"] == "REVERSAL"


# ===========================================================================
# H6 — Finnhub surprise field unit validation
# ===========================================================================


def test_h6_finnhub_surprise_above_one_falls_back_to_computed() -> None:
    """A Finnhub ``surprise=5.0`` is almost certainly absolute units
    (not the 0.05 fractional form). Must be rejected, and the result
    must be computed locally from actual/estimate instead."""
    # 10% real fractional surprise; Finnhub claims +5.0 (absolute units).
    ev = _event(actual=110.0, estimate=100.0, surprise=5.0)
    _inject_events_for_tests([ev])

    info = get_actual_for_event("BoE Interest Rate Decision", currency="GBP")
    assert info is not None
    # Surprise must not have been trusted.
    assert info["surprise_source"] == "computed"
    # And the result reflects the REAL fractional surprise.
    assert info["deviation"] == pytest.approx(0.10)
    assert info["beat_miss"] == "BEAT"


def test_h6_finnhub_surprise_below_minus_one_falls_back() -> None:
    """Same rule on the negative side: ``surprise=-3.0`` is rejected."""
    ev = _event(actual=70.0, estimate=100.0, surprise=-3.0)
    _inject_events_for_tests([ev])

    info = get_actual_for_event("BoE Interest Rate Decision", currency="GBP")
    assert info is not None
    assert info["surprise_source"] == "computed"
    assert info["deviation"] == pytest.approx(-0.30)
    assert info["beat_miss"] == "MISS"


def test_h6_finnhub_surprise_within_bounds_is_trusted() -> None:
    """Inside ``[-1.0, 1.0]`` the Finnhub value is used."""
    ev = _event(actual=102.0, estimate=100.0, surprise=0.5)
    _inject_events_for_tests([ev])

    info = get_actual_for_event("BoE Interest Rate Decision", currency="GBP")
    assert info is not None
    assert info["surprise_source"] == "finnhub"
    assert info["deviation"] == pytest.approx(0.5)
    assert info["beat_miss"] == "BEAT"


# ===========================================================================
# H7 — is_blackout public API
# ===========================================================================


def test_h7_high_impact_in_window_blocked() -> None:
    """A HIGH-impact event 5 minutes after T0 is inside the ±15 min
    window, so is_blackout must report blocked with confidence=high."""
    ev = _event(impact="high", time="2026-05-14 12:05:00")
    _inject_events_for_tests([ev])

    r = is_blackout("GBP", T0)
    assert isinstance(r, BlackoutResult)
    assert r.is_blocked is True
    assert r.confidence == "high"
    assert "high-impact" in r.reason
    assert r.event_summary is not None
    assert "BoE Interest Rate Decision" in r.event_summary


def test_h7_medium_impact_in_window_blocked_with_medium_confidence() -> None:
    """MEDIUM events also block, but the caller can distinguish them
    via ``confidence == "medium"`` for the soft-block behaviour."""
    ev = _event(
        country="DE", event="ZEW Economic Sentiment", impact="medium",
        time="2026-05-14 11:55:00",
    )
    _inject_events_for_tests([ev])

    r = is_blackout("EUR", T0)
    assert r.is_blocked is True
    assert r.confidence == "medium"
    assert "medium-impact" in r.reason


def test_h7_event_out_of_window_clear() -> None:
    """An event outside the ±15 min window must not block."""
    # 2 hours after T0 — well outside.
    ev = _event(impact="high", time="2026-05-14 14:00:00")
    _inject_events_for_tests([ev])

    r = is_blackout("GBP", T0)
    assert r.is_blocked is False
    assert r.reason == "no event in window"
    assert r.confidence == "low"
    assert r.event_summary is None


def test_h7_no_events_clear() -> None:
    """Empty cache (but recently fetched) must report clear."""
    _inject_events_for_tests([])

    r = is_blackout("GBP", T0)
    assert r.is_blocked is False
    assert r.reason == "no event in window"


def test_h7_high_wins_when_both_high_and_medium_in_window() -> None:
    """If both a HIGH and a MEDIUM event sit in the same window, the
    HIGH event drives the result. The caller cannot relax the block
    just because they polled near a MEDIUM."""
    high = _event(impact="high", event="BoE Rate Decision",
                  time="2026-05-14 12:05:00")
    medium = _event(impact="medium", event="UK PMI",
                    time="2026-05-14 11:55:00")
    _inject_events_for_tests([high, medium])

    r = is_blackout("GBP", T0)
    assert r.is_blocked is True
    assert r.confidence == "high"


def test_h7_unknown_currency_fails_closed() -> None:
    _inject_events_for_tests([])
    r = is_blackout("XYZ", T0)
    assert r.is_blocked is True
    assert r.reason == "unknown-currency"
    assert r.confidence == "low"


def test_h7_wrong_country_event_ignored() -> None:
    """A US event in the cache must not block a GBP query."""
    ev = _event(country="US", event="NFP", time="2026-05-14 12:00:00")
    _inject_events_for_tests([ev])

    r = is_blackout("GBP", T0)
    assert r.is_blocked is False


def test_h7_naive_query_time_treated_as_utc() -> None:
    """The Finnhub event timestamps are UTC. A naive datetime input is
    interpreted as UTC (documented contract)."""
    ev = _event(impact="high", time="2026-05-14 12:05:00")
    _inject_events_for_tests([ev])

    naive = datetime(2026, 5, 14, 12, 0, 0)  # no tzinfo
    r = is_blackout("GBP", naive)
    assert r.is_blocked is True


def test_h7_lookback_lookahead_are_respected() -> None:
    """Custom lookback/lookahead values change the window."""
    # Event 30 minutes after T0 — outside default ±15 min, inside ±60 min.
    ev = _event(impact="high", time="2026-05-14 12:30:00")
    _inject_events_for_tests([ev])

    default = is_blackout("GBP", T0)
    assert default.is_blocked is False

    wide = is_blackout("GBP", T0, lookahead_min=60)
    assert wide.is_blocked is True


def test_h7_malformed_event_time_is_skipped() -> None:
    """An event with a garbage ``time`` field must not crash and must
    not contribute to the blackout decision."""
    ev = _event(impact="high", time="not-a-timestamp")
    _inject_events_for_tests([ev])

    r = is_blackout("GBP", T0)
    assert r.is_blocked is False  # no parseable in-window event


# ===========================================================================
# N1 — robust event-time parser
# ===========================================================================


def test_n1_parser_legacy_space_separated() -> None:
    """The legacy Finnhub format must continue to parse."""
    dt = parse_event_time("2026-05-14 12:00:00")
    assert dt == datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)


def test_n1_parser_iso_t_separator_no_offset() -> None:
    """ISO 8601 with T separator and no offset is assumed UTC."""
    dt = parse_event_time("2026-05-14T12:00:00")
    assert dt == datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)


def test_n1_parser_iso_z_suffix() -> None:
    """ISO 8601 with explicit Z suffix."""
    dt = parse_event_time("2026-05-14T12:00:00Z")
    assert dt == datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)


def test_n1_parser_iso_positive_offset_converted_to_utc() -> None:
    """``+01:00`` offset: ``13:00+01:00`` is the same moment as ``12:00 UTC``."""
    dt = parse_event_time("2026-05-14T13:00:00+01:00")
    assert dt is not None
    # Same instant as 12:00 UTC.
    assert dt.astimezone(timezone.utc) == datetime(
        2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc
    )


def test_n1_parser_iso_negative_offset() -> None:
    """``-05:00`` offset: ``07:00-05:00`` is the same moment as ``12:00 UTC``."""
    dt = parse_event_time("2026-05-14T07:00:00-05:00")
    assert dt is not None
    assert dt.astimezone(timezone.utc) == datetime(
        2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc
    )


def test_n1_parser_unix_int() -> None:
    """Unix-seconds as an int."""
    # 1715688000 == 2024-05-14 12:00:00 UTC
    dt = parse_event_time(1715688000)
    assert dt == datetime(2024, 5, 14, 12, 0, 0, tzinfo=timezone.utc)


def test_n1_parser_unix_float() -> None:
    """Unix-seconds as a float keeps sub-second precision."""
    dt = parse_event_time(1715688000.5)
    assert dt is not None
    assert dt == datetime(2024, 5, 14, 12, 0, 0, 500000, tzinfo=timezone.utc)


def test_n1_parser_unix_string() -> None:
    """A numeric string is treated as Unix seconds."""
    dt = parse_event_time("1715688000")
    assert dt == datetime(2024, 5, 14, 12, 0, 0, tzinfo=timezone.utc)


def test_n1_parser_empty_and_none() -> None:
    assert parse_event_time("") is None
    assert parse_event_time("   ") is None
    assert parse_event_time(None) is None


def test_n1_parser_unparseable_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unparseable string must surface in the WARNING log so a Finnhub
    format change is loud, not silent."""
    import logging

    bad = "definitely-not-a-timestamp"
    with caplog.at_level(logging.WARNING, logger="risk.news_calendar.calendar"):
        result = parse_event_time(bad)
    assert result is None
    # The garbage string must appear in at least one captured record.
    assert any(bad in rec.getMessage() for rec in caplog.records), (
        f"WARNING did not include the unparseable value. Captured: {caplog.text}"
    )


def test_n1_parser_bool_treated_as_unparseable() -> None:
    """``bool`` is a subclass of ``int`` in Python; we must not silently
    convert ``True``/``False`` to Unix seconds 0/1."""
    assert parse_event_time(True) is None
    assert parse_event_time(False) is None


def test_n1_is_blackout_accepts_iso_time_strings() -> None:
    """End-to-end: an event with an ISO 8601 ``time`` field must be
    recognised by ``is_blackout`` after the N1 parser upgrade."""
    ev = _event(impact="high", time="2026-05-14T12:05:00Z")
    _inject_events_for_tests([ev])
    r = is_blackout("GBP", T0)
    assert r.is_blocked is True
    assert r.confidence == "high"


# ===========================================================================
# M2 — event_time in get_actual_for_event result
# ===========================================================================


def test_m2_event_time_present_in_result() -> None:
    """The result dict must carry the raw Finnhub ``time`` string under
    ``event_time`` for diagnostic / logging use."""
    ev = _event(
        event="BoE Interest Rate Decision",
        actual=5.25, estimate=5.25,
        time="2026-05-14 12:00:00",
    )
    _inject_events_for_tests([ev])
    info = get_actual_for_event("BoE Interest Rate Decision", currency="GBP")
    assert info is not None
    assert info["event_time"] == "2026-05-14 12:00:00"


def test_m2_event_time_preserves_iso_format() -> None:
    """The raw string is preserved verbatim — no normalisation."""
    ev = _event(
        event="BoE Interest Rate Decision",
        actual=5.25, estimate=5.25,
        time="2026-05-14T12:00:00Z",
    )
    _inject_events_for_tests([ev])
    info = get_actual_for_event("BoE Interest Rate Decision", currency="GBP")
    assert info is not None
    assert info["event_time"] == "2026-05-14T12:00:00Z"


# ===========================================================================
# M3 — cache lookup honours the time window across UTC date boundaries
# ===========================================================================


_MIDNIGHT_BOUNDARY = datetime(2026, 5, 15, 0, 0, 0, tzinfo=timezone.utc)


def test_m3_window_spans_midnight_both_events_considered() -> None:
    """Two events bracketing midnight UTC must both be inside a
    ±15-minute window centred on midnight. ``is_blackout`` walks the
    flat event list and compares parsed times against the window — no
    date-string filter."""
    pre_midnight = _event(
        country="GB", event="UK Late Event", impact="high",
        time="2026-05-14 23:55:00",
    )
    post_midnight = _event(
        country="GB", event="UK Early Event", impact="high",
        time="2026-05-15 00:05:00",
    )
    _inject_events_for_tests([pre_midnight, post_midnight])

    r = is_blackout("GBP", _MIDNIGHT_BOUNDARY)
    assert r.is_blocked is True
    assert r.confidence == "high"


def test_m3_only_pre_midnight_event_still_triggers() -> None:
    """Just the 23:55 day-N event is in the cache. The 00:00 day-N+1
    query must still see it (lookback brings 23:45 day-N into the
    window)."""
    ev = _event(
        country="GB", event="UK Late Event", impact="high",
        time="2026-05-14 23:55:00",
    )
    _inject_events_for_tests([ev])

    r = is_blackout("GBP", _MIDNIGHT_BOUNDARY)
    assert r.is_blocked is True
    assert r.event_summary is not None
    assert "UK Late Event" in r.event_summary
    assert "2026-05-14 23:55:00" in r.event_summary


def test_m3_only_post_midnight_event_still_triggers() -> None:
    """Just the 00:05 day-N+1 event in cache. Query at 00:00 day-N+1
    catches it via lookahead."""
    ev = _event(
        country="GB", event="UK Early Event", impact="high",
        time="2026-05-15 00:05:00",
    )
    _inject_events_for_tests([ev])

    r = is_blackout("GBP", _MIDNIGHT_BOUNDARY)
    assert r.is_blocked is True
    assert r.event_summary is not None
    assert "UK Early Event" in r.event_summary
    assert "2026-05-15 00:05:00" in r.event_summary


def test_m3_event_outside_window_across_boundary_not_blocked() -> None:
    """An event further than the window from the query, even on the
    other side of a UTC date boundary, must NOT block."""
    # 30 min before midnight; query at midnight; default window ±15 min.
    ev = _event(
        country="GB", event="UK Earlier Event", impact="high",
        time="2026-05-14 23:30:00",
    )
    _inject_events_for_tests([ev])

    r = is_blackout("GBP", _MIDNIGHT_BOUNDARY)
    assert r.is_blocked is False
