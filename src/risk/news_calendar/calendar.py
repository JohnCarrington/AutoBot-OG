"""Calendar cache + public query orchestration.

This module owns the module-level event cache (protected by a
``threading.Lock``), the periodic-poll entry point, and two public
query surfaces:

- ``get_actual_for_event(title, *, currency)`` — post-hoc "did this
  scheduled event publish, and what was the surprise?"
- ``is_blackout(currency, query_time, ...)`` — pre-trade "is there a
  high/medium-impact event for this currency near this instant?"
  (review H7, on the critical path for Phase 5 risk-layer wiring).

Failure semantics
-----------------
``poll_for_actual`` distinguishes "fetch failed" from "fetch returned
nothing" (review H4). On failure the cache is **preserved** — the last
successful snapshot remains queryable. ``last_successful_fetch`` is
only advanced on confirmed success. ``is_blackout`` fails closed when
the cache is stale (``cache_staleness_seconds() >
CACHE_STALENESS_THRESHOLD_SECS``).

Typical usage::

    from risk.news_calendar import (
        poll_for_actual, get_actual_for_event, is_blackout,
    )

    poll_for_actual()
    info = get_actual_for_event("CPI Y/Y", currency="USD")
    if info and info["beat_miss"] == "BEAT":
        ...

    result = is_blackout("GBP", now())
    if result.is_blocked:
        skip_entry(reason=result.reason)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from typing import Iterable

from .finnhub_client import FINNHUB_ENABLED, fetch_calendar
from .impact import (
    DEVIATION_THRESHOLD,
    Impact,
    classify_beat_miss,
    classify_direction,
    compute_deviation,
    parse_impact,
)
from .matcher import countries_for_currency, match_event


# Impact ordinals for `events_in_window`'s `impact_min` floor. HIGH is the
# most blocking; LOW the least. Kept local to this module — the public
# `Impact` enum is intentionally not ordered (callers reason about it via
# explicit checks elsewhere).
_IMPACT_RANK: dict[Impact, int] = {
    Impact.HIGH: 2,
    Impact.MEDIUM: 1,
    Impact.LOW: 0,
}


logger = logging.getLogger(__name__)


# Strategy callers poll on a 10-second cadence in production; the
# function is rate-limited to avoid hammering Finnhub. Legacy env name
# (TE_POLL_INTERVAL_SECS) is honoured for parity with .env files.
POLL_INTERVAL: float = float(
    os.getenv(
        "NEWS_POLL_INTERVAL_SECS",
        os.getenv("TE_POLL_INTERVAL_SECS", "10"),
    )
)


# Staleness threshold for the fail-closed blackout check (review H4).
# Default 5 minutes — comfortably longer than a single missed poll,
# short enough that a sustained outage stops the bot trading.
CACHE_STALENESS_THRESHOLD_SECS: float = float(
    os.getenv("NEWS_CACHE_STALENESS_THRESHOLD_SECS", "300")
)


_cache: dict[str, Any] = {
    "events": [],
    "last_fetch_attempt": 0.0,      # rate-limit (success + failure both update)
    "last_successful_fetch": 0.0,   # staleness check (only success updates)
}
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

def poll_for_actual(min_interval: float = POLL_INTERVAL) -> None:
    """Refresh the cached Finnhub event list. No-op within ``min_interval``.

    Safe to call from multiple threads. The fetch happens outside the
    lock so a slow Finnhub response does not block readers.

    Failure handling (review H4): if ``fetch_calendar`` returns ``None``
    (network/HTTP/JSON error or Finnhub disabled), the cache is
    **preserved**. ``last_fetch_attempt`` is still advanced so the rate
    limit applies to retries, but ``last_successful_fetch`` is not —
    ``cache_staleness_seconds`` will report the true age of the data.
    """
    now = time.time()
    with _lock:
        if now - _cache["last_fetch_attempt"] < min_interval:
            return
        _cache["last_fetch_attempt"] = now

    if not FINNHUB_ENABLED:
        logger.debug("[NEWS-CAL] Finnhub disabled (no FINNHUB_API_KEY)")
        return

    result = fetch_calendar()
    if result is None:
        logger.warning(
            "[NEWS-CAL] Finnhub fetch failed — preserving %d cached events",
            len(_cache.get("events") or []),
        )
        return

    with _lock:
        _cache["events"] = result
        _cache["last_successful_fetch"] = time.time()
    logger.debug("[NEWS-CAL] polled: %d events", len(result))


def cache_staleness_seconds() -> float:
    """Seconds since the last successful fetch. ``inf`` if never fetched."""
    with _lock:
        last = _cache.get("last_successful_fetch", 0.0)
    if not last:
        return float("inf")
    return time.time() - last


# ---------------------------------------------------------------------------
# Public lookup: actual/forecast for a named scheduled event
# ---------------------------------------------------------------------------

def get_actual_for_event(
    event_title: str,
    *,
    currency: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Look up actual/forecast for a scheduled event.

    ``currency`` is the ISO-3 code of the *scheduled* event (``"GBP"``,
    ``"USD"``, etc.). It gates Finnhub candidates to matching countries
    so a GBP poll cannot silently return a DE event — see
    ``matcher.match_event`` and AutoBot commit 03ac162. Callers passing
    ``None``/``""`` get ``None`` back (fail closed).

    Returns a dict with keys::

        source, te_event, actual, actual_str, forecast, forecast_str,
        previous, deviation, beat_miss, direction_hint, surprise_source

    or ``None`` if no match (e.g. the release has not landed yet).
    """
    if not currency:
        logger.warning(
            "[NEWS-CAL] get_actual_for_event called without currency for %r "
            "— returning None (country filter requires currency).",
            event_title,
        )
        return None

    with _lock:
        events = list(_cache.get("events", []))

    best = match_event(event_title, currency, events)
    if best is None:
        return None

    actual = float(best["actual"])
    estimate = best.get("estimate")
    result: dict[str, Any] = {
        "source": "finnhub",
        "te_event": best.get("event", ""),
        "actual": actual,
        "actual_str": str(best.get("actual", "")),
        "forecast": float(estimate) if estimate is not None else None,
        "forecast_str": str(estimate) if estimate is not None else "",
        "previous": best.get("prev"),
        # Raw Finnhub time string for diagnostics / logging (review M2).
        # The parsed-datetime form is internal to ``is_blackout``; callers
        # that need a datetime can re-parse this via ``_parse_event_time``.
        "event_time": best.get("time"),
    }

    # Surprise/beat-miss preference order:
    #   1. Finnhub-provided ``surprise`` field (Enterprise tier only).
    #   2. Local computation from actual + estimate via compute_deviation.
    #   3. None across the board if estimate is missing/zero.
    #
    # H5: both branches now classify beat_miss through classify_beat_miss
    # (threshold-gated), so the same input produces the same label
    # regardless of which branch ran.
    #
    # H6: if Finnhub's surprise is outside [-1.0, +1.0] it is almost
    # certainly reported in absolute units rather than fractional. Reject
    # and fall back to the locally-computed deviation.
    surprise_source = "none"
    finnhub_surprise = best.get("surprise")
    if finnhub_surprise is not None:
        try:
            fh_dev = float(finnhub_surprise)
            if abs(fh_dev) > 1.0:
                logger.warning(
                    "[NEWS-CAL] Finnhub surprise %.4f outside fractional range "
                    "[-1.0, 1.0] for %r — falling back to computed deviation.",
                    fh_dev, best.get("event", ""),
                )
            else:
                result["deviation"] = fh_dev
                result["beat_miss"] = classify_beat_miss(fh_dev)
                result["direction_hint"] = classify_direction(fh_dev)
                surprise_source = "finnhub"
        except (TypeError, ValueError):
            pass

    if surprise_source == "none":
        if estimate is not None and float(estimate) != 0:
            result.update(compute_deviation(actual, float(estimate)))
            surprise_source = "computed"
        else:
            result.update(
                {"deviation": None, "direction_hint": None, "beat_miss": None}
            )

    result["surprise_source"] = surprise_source

    logger.info(
        "[NEWS-CAL] %s actual=%s estimate=%s surprise=%s (source=%s) → %s",
        best.get("event"),
        best.get("actual"),
        estimate,
        f"{result['deviation']*100:+.2f}%"
        if result.get("deviation") is not None
        else "N/A",
        surprise_source,
        result.get("direction_hint", "N/A"),
    )
    return result


# ---------------------------------------------------------------------------
# Public blackout check (review H7)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BlackoutResult:
    """Result of an ``is_blackout`` query.

    Attributes:
        is_blocked: True if the risk layer should reject a new entry.
        reason: short machine-readable code describing the decision.
            One of: ``"high-impact event in window"``,
            ``"medium-impact event in window"``,
            ``"no event in window"``,
            ``"unknown-currency"``,
            ``"cache-stale"``.
        event_summary: short human-readable description of the triggering
            event (``"BoE Interest Rate Decision @ 2026-05-14 12:00:00"``),
            or ``None`` if no event triggered.
        confidence: ``"high"`` if a HIGH-impact event drove the decision;
            ``"medium"`` if a MEDIUM-impact event did; ``"low"`` otherwise
            (no event in window, unknown currency, or stale cache).
    """

    is_blocked: bool
    reason: str
    event_summary: Optional[str]
    confidence: str


def _parse_event_time(time_str: Any) -> Optional[datetime]:
    """Parse an event time string or numeric into a UTC-aware ``datetime``.

    Handles multiple incoming representations because Finnhub's response
    format has shifted over time and a Phase-4 callsite that passes
    cached data through different code paths can produce any of these.
    Naive datetimes are interpreted as UTC (the documented Finnhub
    convention).

    Supported inputs (tried in order):

    - ``int`` or ``float``: Unix epoch seconds.
    - String matching ``datetime.fromisoformat``: covers
      ``"YYYY-MM-DD HH:MM:SS"`` (legacy Finnhub), ``"YYYY-MM-DDTHH:MM:SS"``
      (ISO 8601), ``"...Z"`` (UTC marker), ``"...±HH:MM"`` (offset).
      Python 3.11+ ``fromisoformat`` natively handles all of these.
    - Numeric string: parsed as Unix epoch seconds.

    On all attempts failing the function logs a WARNING and returns
    ``None`` — the caller can skip the entry rather than raise. The
    warning intentionally surfaces unparseable strings so a Finnhub
    format change is loud rather than silently dropping events from
    blackout evaluation (review N1).
    """
    if time_str is None:
        return None

    # Numeric Unix timestamp (int or float — but not bool, which is an
    # int subclass and would silently parse as 0/1).
    if isinstance(time_str, bool):
        logger.warning("[NEWS-CAL] could not parse event time %r", time_str)
        return None
    if isinstance(time_str, (int, float)):
        try:
            return datetime.fromtimestamp(float(time_str), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            logger.warning("[NEWS-CAL] could not parse event time %r", time_str)
            return None

    s = str(time_str).strip()
    if not s:
        return None

    # Python 3.11+ fromisoformat handles every string form we care about:
    # space-separated, T-separated, Z suffix, ±HH:MM and ±HHMM offsets.
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = None

    if dt is not None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    # Numeric string — Unix seconds.
    try:
        return datetime.fromtimestamp(float(s), tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        pass

    logger.warning(
        "[NEWS-CAL] could not parse event time %r — event will be excluded "
        "from blackout evaluation",
        time_str,
    )
    return None


def is_blackout(
    currency: str,
    query_time: datetime,
    *,
    lookback_min: int = 15,
    lookahead_min: int = 15,
) -> BlackoutResult:
    """Return whether ``query_time`` is within a news-blackout window.

    The window is ``[query_time - lookback_min, query_time + lookahead_min]``
    (default ±15 min, per ``docs/v1_architecture.md`` §6.6).

    Args:
        currency: ISO-3 currency code (``"GBP"``, ``"USD"``, ``"EUR"``, ...).
        query_time: timezone-aware ``datetime``. A naive datetime is
            interpreted as UTC (the same convention the Finnhub event
            timestamps use).
        lookback_min: minutes BEFORE the event that count as blackout.
        lookahead_min: minutes AFTER the event that count as blackout.

    Fail-closed:
        - Unknown currency → ``is_blocked=True``, reason ``"unknown-currency"``.
        - Cache staler than ``CACHE_STALENESS_THRESHOLD_SECS`` →
          ``is_blocked=True``, reason ``"cache-stale"``.

    Impact handling:
        - HIGH event in window → block, confidence ``"high"``.
        - MEDIUM event in window → block (soft), confidence ``"medium"``.
          The caller (risk layer) decides what "soft" means — this
          function reports the cause; the caller decides the action.
        - LOW events are ignored entirely.

    If both HIGH and MEDIUM events are in the same window, HIGH wins.
    """
    allowed = countries_for_currency(currency)
    if allowed is None:
        return BlackoutResult(
            is_blocked=True,
            reason="unknown-currency",
            event_summary=None,
            confidence="low",
        )

    if cache_staleness_seconds() > CACHE_STALENESS_THRESHOLD_SECS:
        return BlackoutResult(
            is_blocked=True,
            reason="cache-stale",
            event_summary=None,
            confidence="low",
        )

    if query_time.tzinfo is None:
        query_time = query_time.replace(tzinfo=timezone.utc)

    window_start = query_time - timedelta(minutes=lookback_min)
    window_end = query_time + timedelta(minutes=lookahead_min)

    with _lock:
        events = list(_cache.get("events", []))

    triggering_event: Optional[dict[str, Any]] = None
    triggering_impact: Impact = Impact.LOW
    for ev in events:
        if ev.get("country") not in allowed:
            continue
        impact = parse_impact(ev.get("impact"))
        if impact is Impact.LOW:
            continue
        ev_time = _parse_event_time(ev.get("time", ""))
        if ev_time is None:
            continue
        if not (window_start <= ev_time <= window_end):
            continue
        # Prefer HIGH over MEDIUM if multiple events sit in the same window.
        if triggering_event is None or (
            impact is Impact.HIGH and triggering_impact is not Impact.HIGH
        ):
            triggering_event = ev
            triggering_impact = impact
        if triggering_impact is Impact.HIGH:
            break  # nothing more severe to find

    if triggering_event is None:
        return BlackoutResult(
            is_blocked=False,
            reason="no event in window",
            event_summary=None,
            confidence="low",
        )

    summary = (
        f"{triggering_event.get('event', '?')} "
        f"@ {triggering_event.get('time', '?')}"
    )
    return BlackoutResult(
        is_blocked=True,
        reason=f"{triggering_impact.value}-impact event in window",
        event_summary=summary,
        confidence=triggering_impact.value,
    )


# ---------------------------------------------------------------------------
# Window query (used by the day-type classifier)
# ---------------------------------------------------------------------------

def events_in_window(
    currencies: Iterable[str],
    start_utc: datetime,
    end_utc: datetime,
    *,
    impact_min: Impact = Impact.HIGH,
) -> list[dict[str, Any]]:
    """Return cached events whose ``time`` lies in ``[start_utc, end_utc]``.

    Country gating reuses :py:func:`matcher.countries_for_currency` — the
    union of allowed countries across ``currencies`` is the match set.
    Unknown currencies contribute nothing (no country added); an entirely
    unknown set returns ``[]``.

    Impact gating keeps events at ``impact_min`` or higher in the
    HIGH > MEDIUM > LOW order. Default is HIGH-only, matching the
    day-type classifier's "big news" definition.

    Boundaries are inclusive on both ends, mirroring
    :py:func:`is_blackout`'s window semantics. Naive ``start_utc`` /
    ``end_utc`` are interpreted as UTC.

    Note: this query does NOT fail-closed on a stale cache. Staleness
    policy is the caller's concern (the day-type classifier wraps it).
    Returns whatever the cache currently holds.
    """
    allowed_countries: set[str] = set()
    for cur in currencies:
        countries = countries_for_currency(cur)
        if countries is not None:
            allowed_countries.update(countries)
    if not allowed_countries:
        return []
    impact_floor = _IMPACT_RANK[impact_min]

    if start_utc.tzinfo is None:
        start_utc = start_utc.replace(tzinfo=timezone.utc)
    if end_utc.tzinfo is None:
        end_utc = end_utc.replace(tzinfo=timezone.utc)

    with _lock:
        events = list(_cache.get("events", []))

    out: list[dict[str, Any]] = []
    for ev in events:
        if ev.get("country") not in allowed_countries:
            continue
        if _IMPACT_RANK[parse_impact(ev.get("impact"))] < impact_floor:
            continue
        ev_time = _parse_event_time(ev.get("time", ""))
        if ev_time is None:
            continue
        if start_utc <= ev_time <= end_utc:
            out.append(ev)
    return out


# ---------------------------------------------------------------------------
# Test-only helpers. Prefixed with _ to signal "not part of the public API";
# the alternative is to let tests poke ``_cache`` directly, which couples
# tests to the cache structure. Keeping the seam here is cheap and lets the
# cache implementation evolve.
# ---------------------------------------------------------------------------

def _reset_cache_for_tests() -> None:
    """Reset the module-level cache. For use in test fixtures only."""
    with _lock:
        _cache["events"] = []
        _cache["last_fetch_attempt"] = 0.0
        _cache["last_successful_fetch"] = 0.0


def _inject_events_for_tests(events: list[dict[str, Any]]) -> None:
    """Prefill the cache with synthetic events. For use in test fixtures only.

    Marks ``last_successful_fetch`` as ``now`` so ``is_blackout`` does not
    immediately fail closed on staleness.
    """
    with _lock:
        _cache["events"] = list(events)
        now = time.time()
        _cache["last_fetch_attempt"] = now
        _cache["last_successful_fetch"] = now


def _force_cache_age_for_tests(age_seconds: float) -> None:
    """Force the cache to appear ``age_seconds`` old. For use in test fixtures."""
    with _lock:
        _cache["last_successful_fetch"] = time.time() - age_seconds


__all__ = [
    "POLL_INTERVAL",
    "CACHE_STALENESS_THRESHOLD_SECS",
    "BlackoutResult",
    "poll_for_actual",
    "cache_staleness_seconds",
    "get_actual_for_event",
    "is_blackout",
    "events_in_window",
]
