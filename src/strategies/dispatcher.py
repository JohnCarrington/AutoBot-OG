"""Day-type → strategy dispatcher.

Clean-swap step 2b: replaces the regime-based router with a day-type
dispatch table. The day_type comes from
:py:func:`day_type.classify_day_type`, computed once per BAR_CLOSE
upstream in ``bot.loop``.

Dispatch table
--------------
- ``DayType.BIG_NEWS_DAY`` → detect_news + detect_structure_break + detect_ema_pullback
- ``DayType.PRE_BIG_NEWS`` → detect_structure_break + detect_ema_pullback
- ``DayType.NORMAL``       → detect_bb_bounce

``detect_news`` (step 3) and ``detect_structure_break`` (step 5) do
not exist yet; for THIS step they are wired as local stub detectors
that always return ``None``. The table is complete so future steps
can drop in the real detectors without touching the dispatcher
shape.

Multi-signal returns
--------------------
Each detector returns ``Optional[Signal]``; the dispatcher collects
non-``None`` returns into a ``list[Signal]``. Unlike the prior
regime-router (which routed to a single eligible strategy), a
day-type can fan out to multiple detectors — BIG_NEWS_DAY runs all
three of news/structure-break/ema-pullback.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Optional

import pandas as pd

from day_type import DayType
from structure_engine import StructureState

from .bb_bounce import detect_bb_bounce
from .ema_pullback import detect_ema_pullback
from .signal import Signal


logger = logging.getLogger(__name__)


# TODO step-3: replace with the real detect_news implementation.
def detect_news(
    df_m5: pd.DataFrame,  # noqa: ARG001 — stub
    df_h1: pd.DataFrame,  # noqa: ARG001 — stub
    day_type: DayType,  # noqa: ARG001 — stub
    structure_state: StructureState,  # noqa: ARG001 — stub
    pair: str,  # noqa: ARG001 — stub
    current_time: datetime,  # noqa: ARG001 — stub
) -> Optional[Signal]:
    """Stub detector — step 3 will land the real news strategy."""
    return None


# TODO step-5: replace with the real detect_structure_break implementation.
def detect_structure_break(
    df_m5: pd.DataFrame,  # noqa: ARG001 — stub
    df_h1: pd.DataFrame,  # noqa: ARG001 — stub
    day_type: DayType,  # noqa: ARG001 — stub
    structure_state: StructureState,  # noqa: ARG001 — stub
    pair: str,  # noqa: ARG001 — stub
    current_time: datetime,  # noqa: ARG001 — stub
) -> Optional[Signal]:
    """Stub detector — step 5 will land the real structure-break strategy."""
    return None


Detector = Callable[
    [pd.DataFrame, pd.DataFrame, DayType, StructureState, str, datetime],
    Optional[Signal],
]


DISPATCH: dict[DayType, tuple[Detector, ...]] = {
    DayType.BIG_NEWS_DAY: (detect_news, detect_structure_break, detect_ema_pullback),
    DayType.PRE_BIG_NEWS: (detect_structure_break, detect_ema_pullback),
    DayType.NORMAL: (detect_bb_bounce,),
}


def detect_all_setups(
    *,
    df_m5: pd.DataFrame,
    df_h1: pd.DataFrame,
    day_type: DayType,
    structure_state: StructureState,
    pair: str,
    current_time: datetime,
) -> list[Signal]:
    """Return signals from every detector mapped to ``day_type``.

    The day-type table maps each ``DayType`` to a tuple of detectors.
    Each detector is called with the full input set and a non-``None``
    return is collected into the result list.
    """
    detectors = DISPATCH.get(day_type, ())
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
    "detect_all_setups",
    "detect_news",
    "detect_structure_break",
]
