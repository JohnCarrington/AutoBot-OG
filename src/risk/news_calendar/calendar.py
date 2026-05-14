"""Calendar cache + public query orchestration.

This module owns the module-level event cache (protected by a
``threading.Lock``), the periodic-poll entry point, and the public
``get_actual_for_event`` lookup. Sub-modules are kept stateless so they
can be unit-tested without touching the cache.

Typical usage::

    from risk.news_calendar import poll_for_actual, get_actual_for_event

    poll_for_actual()                           # refresh cache (rate-limited)
    info = get_actual_for_event("CPI Y/Y", currency="USD")
    if info and info["beat_miss"] == "BEAT":
        ...
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Optional

from .finnhub_client import FINNHUB_ENABLED, fetch_calendar
from .impact import DEVIATION_THRESHOLD, compute_deviation, compute_surprise
from .matcher import match_event


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


_cache: dict[str, Any] = {"events": [], "last_fetch": 0.0}
_lock = threading.Lock()


def poll_for_actual(min_interval: float = POLL_INTERVAL) -> None:
    """Refresh the cached Finnhub event list. No-op within ``min_interval``.

    Safe to call from multiple threads. The fetch happens outside the
    lock so a slow Finnhub response does not block readers.
    """
    now = time.time()
    with _lock:
        if now - _cache["last_fetch"] < min_interval:
            return

    if not FINNHUB_ENABLED:
        logger.debug("[NEWS-CAL] Finnhub disabled (no FINNHUB_API_KEY)")
        return

    events = fetch_calendar()
    with _lock:
        _cache["events"] = events
        _cache["last_fetch"] = time.time()
    logger.debug("[NEWS-CAL] polled: %d events", len(events))


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
    }

    # Surprise/beat-miss preference order:
    #   1. Finnhub-provided ``surprise`` field (Enterprise tier only).
    #   2. Local computation from actual + estimate.
    #   3. None across the board if estimate is missing/zero.
    surprise_source = "none"
    finnhub_surprise = best.get("surprise")
    if finnhub_surprise is not None:
        try:
            fh_dev = float(finnhub_surprise)
            _, fh_beat_miss = (
                compute_surprise(actual, float(estimate))
                if estimate is not None
                else (None, None)
            )
            result["deviation"] = fh_dev
            result["beat_miss"] = fh_beat_miss or (
                "BEAT" if fh_dev > 0 else ("MISS" if fh_dev < 0 else "IN_LINE")
            )
            result["direction_hint"] = (
                "CONTINUATION" if abs(fh_dev) > DEVIATION_THRESHOLD else "REVERSAL"
            )
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
# Test-only helpers. Prefixed with _ to signal "not part of the public API";
# the alternative is to let tests poke ``_cache`` directly, which couples
# tests to the cache structure. Keeping the seam here is cheap and lets the
# cache implementation evolve.
# ---------------------------------------------------------------------------

def _reset_cache_for_tests() -> None:
    """Reset the module-level cache. For use in test fixtures only."""
    with _lock:
        _cache["events"] = []
        _cache["last_fetch"] = 0.0


def _inject_events_for_tests(events: list[dict[str, Any]]) -> None:
    """Prefill the cache with synthetic events. For use in test fixtures only."""
    with _lock:
        _cache["events"] = list(events)
        _cache["last_fetch"] = time.time()


__all__ = [
    "POLL_INTERVAL",
    "poll_for_actual",
    "get_actual_for_event",
]
