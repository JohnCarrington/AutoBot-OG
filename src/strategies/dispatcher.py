"""Day-type → strategy dispatcher.

Clean-swap step 2b: replaces the regime-based router with a day-type
dispatch table. The day_type comes from
:py:func:`day_type.classify_day_type`, computed once per BAR_CLOSE
upstream in ``bot.loop``.

Dispatch table
--------------
- ``DayType.BIG_NEWS_DAY`` →
    - inside a HIGH-impact release window for the pair's currencies:
      ``detect_news`` only.
    - outside the window:
      ``detect_structure_break`` + ``detect_ema_pullback`` only.
- ``DayType.PRE_BIG_NEWS`` → detect_structure_break + detect_ema_pullback.
- ``DayType.NORMAL``       → detect_bb_bounce.

Window-suppression contract (step 5b)
-------------------------------------
On ``BIG_NEWS_DAY`` only, the dispatcher splits the day into release
windows (``[release - 15, release + 30]`` for each HIGH event, default).
Inside any window, structure_break / ema_pullback are MUTED because the
data-driven entry from ``detect_news`` is the right tool. Outside any
window, ``detect_news`` would have no fresh release to read so it is
skipped, and the structure detectors fire normally.

``PRE_BIG_NEWS`` and ``NORMAL`` are NEVER window-suppressed.

Multi-signal returns
--------------------
Each detector returns ``Optional[Signal]``; the dispatcher collects
non-``None`` returns into a ``list[Signal]``. Unlike the prior
regime-router (which routed to a single eligible strategy), a
day-type can fan out to multiple detectors — outside-window
BIG_NEWS_DAY / PRE_BIG_NEWS run both structure detectors.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Optional

import pandas as pd

from day_type import DayType
from risk.rules.news_blackout import _currencies_for
from structure_engine import StructureState

from .bb_bounce import detect_bb_bounce
from .ema_pullback import detect_ema_pullback
from .news import detect_news
from .news_window import is_in_release_window
from .signal import Signal
from .structure_break import detect_structure_break


logger = logging.getLogger(__name__)


Detector = Callable[
    [pd.DataFrame, pd.DataFrame, DayType, StructureState, str, datetime],
    Optional[Signal],
]


# Static map: day_type → "all potential detectors for this day_type".
# For ``BIG_NEWS_DAY`` the *active* subset depends on the release-
# window predicate (see :func:`_active_detectors`); for the other
# day-types this tuple IS the active set.
#
# Contract: on ``BIG_NEWS_DAY`` the news detector occupies slot [0]
# and the structure detectors occupy slots [1:]. ``_active_detectors``
# slices on that ordering when partitioning by the window predicate,
# so reordering the BIG_NEWS_DAY tuple changes which detectors fire
# inside vs outside the window.
DISPATCH: dict[DayType, tuple[Detector, ...]] = {
    DayType.BIG_NEWS_DAY: (detect_news, detect_structure_break, detect_ema_pullback),
    DayType.PRE_BIG_NEWS: (detect_structure_break, detect_ema_pullback),
    DayType.NORMAL: (detect_bb_bounce,),
}


def _active_detectors(
    day_type: DayType, pair: str, current_time: datetime,
) -> tuple[Detector, ...]:
    """Return the detectors that should actually run for this bar.

    On ``BIG_NEWS_DAY`` consults :func:`is_in_release_window` against
    the pair's currencies and the cached calendar:
      - inside → first slot of ``DISPATCH[BIG_NEWS_DAY]`` (news);
      - outside → remaining slots (the structure detectors).

    Driven from the DISPATCH table so tests that monkeypatch DISPATCH
    flow through cleanly. Everything else returns the static
    ``DISPATCH`` entry verbatim.
    """
    if day_type is not DayType.BIG_NEWS_DAY:
        return DISPATCH.get(day_type, ())
    table = DISPATCH.get(DayType.BIG_NEWS_DAY, ())
    if not table:
        return ()
    currencies = _currencies_for(pair)
    inside = is_in_release_window(current_time, currencies)
    return table[:1] if inside else table[1:]


def detect_all_setups(
    *,
    df_m5: pd.DataFrame,
    df_h1: pd.DataFrame,
    day_type: DayType,
    structure_state: StructureState,
    pair: str,
    current_time: datetime,
) -> list[Signal]:
    """Return signals from every active detector for this bar.

    The active set depends on ``day_type`` and (for BIG_NEWS_DAY) the
    release-window predicate. Each detector is called with the full
    input set and a non-``None`` return is collected into the result
    list.
    """
    detectors = _active_detectors(day_type, pair, current_time)
    if not detectors:
        logger.info(
            "no setups for %s: day_type=%s has no detectors mapped",
            pair, day_type,
        )
        return []

    signals: list[Signal] = []
    for detector in detectors:
        result = detector(
            df_m5, df_h1, day_type, structure_state, pair, current_time,
        )
        if result is not None:
            signals.append(result)

    if not signals:
        logger.info(
            "no setups for %s: day_type=%s — %d detector(s) ran, all returned None",
            pair, day_type, len(detectors),
        )
    return signals


__all__ = [
    "DISPATCH",
    "Detector",
    "_active_detectors",
    "detect_all_setups",
    "detect_news",
    "detect_structure_break",
]
