"""Pure day-type classifier.

One function, no state machine. The calendar IS the state.

Day boundary
------------
"Today" is the NY trading session containing ``now_utc``. Sessions end
at :data:`risk.constants.NY_CLOSE_HOUR_LOCAL` (17:00) in
:data:`risk.constants.NY_TZ_NAME` (``America/New_York``). The label
date is the session's *end* date — same convention as
:py:func:`risk.state.circuit_breaker_state.current_session_date_ny`,
which the daily-DD breaker uses. So a HIGH event at 23:30 UTC Sun lives
in the NY session ending Monday 17:00 NY, not Sunday's session.

Fail-closed
-----------
If the news_calendar cache is staler than
:data:`risk.news_calendar.CACHE_STALENESS_THRESHOLD_SECS`, the
classifier returns :py:attr:`DayType.BIG_NEWS_DAY`. Parity with
:py:func:`risk.news_calendar.is_blackout`: when we don't know what
the calendar contains, assume the most blocking posture.

Purity
------
No hysteresis, no per-bar accumulation, no M5 confirmation. Two
calls with the same ``now_utc`` and currencies always return the
same answer (modulo cache state, which is the unit under test).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from risk.constants import NY_CLOSE_HOUR_LOCAL, NY_TZ_NAME
from risk.news_calendar import (
    CACHE_STALENESS_THRESHOLD_SECS,
    Impact,
    cache_staleness_seconds,
    events_in_window,
)
from risk.state.circuit_breaker_state import current_session_date_ny

from .labels import DayType


def classify_day_type(
    *,
    now_utc: datetime,
    currencies: Iterable[str],
    pre_big_news_lookahead_hours: int = 24,
) -> DayType:
    """Classify ``now_utc`` as ``BIG_NEWS_DAY`` / ``PRE_BIG_NEWS`` / ``NORMAL``.

    Parameters
    ----------
    now_utc : datetime
        Query instant. Naive datetimes are interpreted as UTC (parity
        with :py:func:`risk.news_calendar.is_blackout`).
    currencies : Iterable[str]
        ISO-3 currency codes the caller cares about (typically the
        pair's two currencies, e.g. ``("GBP", "USD")``). Unknown codes
        contribute no countries to the match set; an entirely unknown
        set therefore returns :py:attr:`DayType.NORMAL` unless the
        cache is stale.
    pre_big_news_lookahead_hours : int, default 24
        Horizon for the PRE_BIG_NEWS lookahead. Tunable by the caller;
        the spec default of 24h covers "anything in tomorrow's session
        is a heads-up today".

    Returns
    -------
    DayType
        See module docstring for semantics.
    """
    # Fail-closed: unknown calendar state ≡ BIG_NEWS_DAY.
    if cache_staleness_seconds() > CACHE_STALENESS_THRESHOLD_SECS:
        return DayType.BIG_NEWS_DAY

    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    # Current NY session window. Session ends at NY_CLOSE_HOUR_LOCAL
    # (17:00) NY-local on `session_label_date`; session started 24h
    # earlier. The label date is the END date — matches
    # current_session_date_ny.
    ny = ZoneInfo(NY_TZ_NAME)
    session_label_date = current_session_date_ny(now_utc)
    session_end_ny = datetime(
        session_label_date.year,
        session_label_date.month,
        session_label_date.day,
        NY_CLOSE_HOUR_LOCAL,
        0,
        tzinfo=ny,
    )
    session_end_utc = session_end_ny.astimezone(timezone.utc)
    session_start_utc = session_end_utc - timedelta(days=1)

    # BIG_NEWS_DAY — any HIGH event in the current NY session.
    today_highs = events_in_window(
        currencies=currencies,
        start_utc=session_start_utc,
        end_utc=session_end_utc,
        impact_min=Impact.HIGH,
    )
    if today_highs:
        return DayType.BIG_NEWS_DAY

    # PRE_BIG_NEWS — current session is calm, but a HIGH falls inside
    # (now_utc, now_utc + lookahead]. When the lookahead overlaps with
    # the current session there is nothing to find there (BIG branch
    # already proved no HIGH in-session), so any hit is genuinely
    # "next session or later".
    lookahead_end = now_utc + timedelta(hours=pre_big_news_lookahead_hours)
    ahead_highs = events_in_window(
        currencies=currencies,
        start_utc=now_utc,
        end_utc=lookahead_end,
        impact_min=Impact.HIGH,
    )
    if ahead_highs:
        return DayType.PRE_BIG_NEWS

    return DayType.NORMAL


__all__ = ["classify_day_type"]
