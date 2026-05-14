"""DST-aware FX session-window predicates.

The trading day is segmented into London / NY / Asia sessions; the
liquidity-sweep strategy gates on London or NY *only* (Asia is rejected
per ``docs/v1_architecture.md`` §5.3). We compute each window in its
*local* timezone via :mod:`zoneinfo` so DST transitions are handled
automatically — converting a fixed UTC range would mis-align in March
and November every year.

Window definitions (local time, end-exclusive)
----------------------------------------------
- **London**: 07:00 ≤ Europe/London < 15:00.
- **NY**: 08:00 ≤ America/New_York < 17:00.
- **Overlap**: both windows true simultaneously.

Helpers return ``bool`` and accept a single ``datetime`` (naive or
aware). A naive datetime is interpreted as UTC.
"""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo


_LONDON_TZ = ZoneInfo("Europe/London")
_NY_TZ = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")

_LONDON_OPEN = time(7, 0)
_LONDON_CLOSE = time(15, 0)
_NY_OPEN = time(8, 0)
_NY_CLOSE = time(17, 0)


def _to_aware_utc(now: datetime) -> datetime:
    """Treat naive datetimes as UTC; pass-through for aware ones."""
    if now.tzinfo is None:
        return now.replace(tzinfo=_UTC)
    return now


def london_session(now: datetime) -> bool:
    """Return ``True`` if ``now`` falls inside the London window.

    07:00 ≤ ``Europe/London`` time < 15:00. DST handled by
    :class:`zoneinfo.ZoneInfo`.
    """
    local = _to_aware_utc(now).astimezone(_LONDON_TZ).time()
    return _LONDON_OPEN <= local < _LONDON_CLOSE


def ny_session(now: datetime) -> bool:
    """Return ``True`` if ``now`` falls inside the NY window.

    08:00 ≤ ``America/New_York`` time < 17:00. DST handled by
    :class:`zoneinfo.ZoneInfo`.
    """
    local = _to_aware_utc(now).astimezone(_NY_TZ).time()
    return _NY_OPEN <= local < _NY_CLOSE


def london_ny_overlap(now: datetime) -> bool:
    """Return ``True`` if both London *and* NY sessions are open.

    The overlap is the highest-liquidity window of the day in
    practice; v1 doesn't gate any strategy on the overlap directly,
    but the helper is exported for v2 use.
    """
    return london_session(now) and ny_session(now)


__all__ = ["london_ny_overlap", "london_session", "ny_session"]
