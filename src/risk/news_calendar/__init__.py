"""Economic news calendar — Finnhub-backed.

Ported from the legacy AutoBot codebase (``/opt/tradingbot/te_calendar.py``)
and split into sub-modules per the AutoBot-OG layered architecture:

- ``finnhub_client`` : HTTP fetch from Finnhub's economic-calendar endpoint.
- ``impact``         : ``Impact`` enum and actual-vs-forecast helpers.
- ``matcher``        : event-name + country matching (includes the
                       2026-04-23 country-filter regression fix from
                       AutoBot commit 03ac162).
- ``calendar``       : module-level cache, poll orchestration, and the
                       public ``get_actual_for_event`` lookup.

The legacy Trading Economics guest-API fallback is **not** ported.
Finnhub became the primary source in legacy commit 6389cde, and the
TE fallback's US/GB-only country filter would have re-introduced the
03ac162 bug for EUR/JPY/CAD events. Failing closed (no Finnhub → no
match) is the v1-spec-aligned behaviour: when calendar data is
unavailable, the risk layer treats every scheduled event as a
blackout, which is the safe default (``docs/v1_architecture.md`` §6.6).
"""

from .calendar import (
    CACHE_STALENESS_THRESHOLD_SECS,
    POLL_INTERVAL,
    BlackoutResult,
    cache_staleness_seconds,
    get_actual_for_event,
    is_blackout,
    poll_for_actual,
)
from .impact import (
    DEVIATION_THRESHOLD,
    Impact,
    classify_beat_miss,
    classify_direction,
    compute_deviation,
    compute_surprise,
    parse_impact,
)


__all__ = [
    # Public API
    "get_actual_for_event",
    "poll_for_actual",
    "is_blackout",
    "BlackoutResult",
    "Impact",
    "parse_impact",
    # Knobs / helpers exposed for the risk layer and tests
    "POLL_INTERVAL",
    "CACHE_STALENESS_THRESHOLD_SECS",
    "DEVIATION_THRESHOLD",
    "cache_staleness_seconds",
    "classify_beat_miss",
    "classify_direction",
    "compute_deviation",
    "compute_surprise",
]
