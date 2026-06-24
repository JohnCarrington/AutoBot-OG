"""News release-window predicate (step 5b).

Defines the time-window contract that decides which detectors are
active on a ``BIG_NEWS_DAY``:

  - Inside any HIGH-impact release window for the pair's currencies
    → only :func:`strategies.news.detect_news` runs.
  - Outside the window → only ``detect_structure_break`` and
    ``detect_ema_pullback`` run.

The asymmetric window ``[release - 15, release + 30]`` is the
spec-default — a 15-minute pre-release pause (no trend entries while
the market is anticipating), then a 30-minute post-release window for
the data-driven entry. Both bounds are env-overridable for tuning.

``PRE_BIG_NEWS`` and ``NORMAL`` are never window-suppressed; this
predicate is only consulted by the dispatcher on ``BIG_NEWS_DAY``.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Iterable

from risk.news_calendar import Impact, events_in_window, parse_event_time


NEWS_WINDOW_PRE_MIN: int = int(
    os.getenv("STRATEGY_NEWS_WINDOW_PRE_MIN", "15")
)
"""Minutes before a HIGH-impact release that count as inside the window."""

NEWS_WINDOW_POST_MIN: int = int(
    os.getenv("STRATEGY_NEWS_WINDOW_POST_MIN", "30")
)
"""Minutes after a HIGH-impact release that count as inside the window."""


def is_in_release_window(
    now_utc: datetime,
    currencies: Iterable[str],
    *,
    pre_min: int = NEWS_WINDOW_PRE_MIN,
    post_min: int = NEWS_WINDOW_POST_MIN,
) -> bool:
    """True if ``now_utc`` is inside ANY HIGH-impact release window for
    ``currencies``.

    The window per release is ``[release - pre_min, release + post_min]``.
    Events whose currency is not in ``currencies`` do not gate; this is
    a per-pair predicate.

    Returns ``False`` when the calendar cache holds no HIGH events for
    the currencies in the relevant time horizon — the dispatcher's
    consumers (structure_break / ema_pullback) then fire as normal.
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    horizon_start = now_utc - timedelta(minutes=post_min)
    horizon_end = now_utc + timedelta(minutes=pre_min)
    candidates = events_in_window(
        currencies=currencies,
        start_utc=horizon_start,
        end_utc=horizon_end,
        impact_min=Impact.HIGH,
    )
    for ev in candidates:
        release_dt = parse_event_time(ev.get("time"))
        if release_dt is None:
            continue
        window_start = release_dt - timedelta(minutes=pre_min)
        window_end = release_dt + timedelta(minutes=post_min)
        if window_start <= now_utc <= window_end:
            return True
    return False


__all__ = [
    "NEWS_WINDOW_PRE_MIN",
    "NEWS_WINDOW_POST_MIN",
    "is_in_release_window",
]
