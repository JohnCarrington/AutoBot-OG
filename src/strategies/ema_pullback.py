"""EMA Pullback strategy (BIG_NEWS_DAY / PRE_BIG_NEWS) — clean-swap step 2b.

The strategy reads :class:`StructureState` and applies the spec §13
EMA-Pullback gates. The old "wick-touches-EMA50 + reclaim + bullish-
bodied confirmation" 3-bar pattern is gone; the Structure Engine's
``current_reaction`` (failed reclaim) plus ``structure_mode ==
TREND_CONTINUATION`` carry the same intent.

3b note (B-5 split closure): the prior reaction set also included the
two ``*_ACCEPTANCE_BREAK`` reactions, which step-3 ``structure_break``
also consumed — so a single acceptance-break event emitted two Signals
through the dispatcher. To make the split clean (one reaction → exactly
one strategy), ema_pullback now consumes ONLY the FAILED_RECLAIM
reactions; ``structure_break`` owns ACCEPTANCE_BREAK.

Gates (spec §13, B-5 split)
---------------------------
SELL:
    - ``htf_bias == BEARISH``
    - ``structure_mode == TREND_CONTINUATION``
    - ``current_reaction == FAILED_RECLAIM_BELOW_SUPPORT``

BUY:
    - ``htf_bias == BULLISH``
    - ``structure_mode == TREND_CONTINUATION``
    - ``current_reaction == FAILED_RECLAIM_ABOVE_RESISTANCE``

The dispatcher routes this detector on ``DayType.BIG_NEWS_DAY`` and
``DayType.PRE_BIG_NEWS`` days.

SL/TP
-----
SL anchors on the *broken* level's zone edge plus ATR padding. TP is
``None`` — the execution layer uses its structure-trail exit, which
is the right behaviour for trends (no fixed target).
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from day_type import DayType
from common import Direction
from structure_engine import StructureLevel, StructureState

from ._structure_entry import (
    build_sl,
    latest_atr,
    latest_close,
    latest_timestamp,
    macd_aligned_confidence,
)
from .constants import (
    EMA_CONT_CONF_HIGH,
    EMA_CONT_CONF_LOW,
)
from .management import profile_for
from .signal import Signal, compute_invalid_after


_STRATEGY_NAME = "ema_pullback"

# 3b (B-5 split): FAILED_RECLAIM only. ACCEPTANCE_BREAK reactions are
# owned by ``strategies.structure_break``.
_BEARISH_REACTION = "FAILED_RECLAIM_BELOW_SUPPORT"
_BULLISH_REACTION = "FAILED_RECLAIM_ABOVE_RESISTANCE"


def detect_ema_pullback(
    df_m5: pd.DataFrame,
    df_h1: pd.DataFrame,
    day_type: DayType,
    structure_state: StructureState,
    pair: str,
    current_time: datetime,  # noqa: ARG001 — kept for dispatcher uniformity
) -> Optional[Signal]:
    """Return a Signal for an EMA pullback continuation, else ``None``."""
    if not structure_state.is_valid:
        return None
    if structure_state.structure_mode != "TREND_CONTINUATION":
        return None

    direction = _direction_from(structure_state)
    if direction is None:
        return None

    # The reaction's level is the one we broke / failed-to-reclaim.
    if direction == Direction.BEARISH:
        anchor_level = structure_state.nearest_support
        if anchor_level is None:
            return None
        anchor_price = anchor_level.zone_high  # SL above the broken support
    else:
        anchor_level = structure_state.nearest_resistance
        if anchor_level is None:
            return None
        anchor_price = anchor_level.zone_low

    atr_m5 = latest_atr(df_m5)
    if math.isnan(atr_m5) or atr_m5 <= 0:
        return None

    entry_price = latest_close(df_m5)
    if math.isnan(entry_price):
        return None

    profile = profile_for(_STRATEGY_NAME, day_type)
    sl_price = build_sl(
        direction=direction,
        anchor_price=anchor_price,
        atr_m5=atr_m5,
        pair=pair,
        sl_atr_mult=profile.sl_atr_mult,
        sl_floor_pips_override=profile.sl_floor_pips_override,
    )
    confidence = macd_aligned_confidence(
        direction=direction, df_h1=df_h1,
        high=EMA_CONT_CONF_HIGH, low=EMA_CONT_CONF_LOW,
    )
    source_ts = latest_timestamp(df_m5)
    if source_ts is None:
        return None

    debug: dict[str, Any] = {
        "structure_mode": structure_state.structure_mode,
        "current_reaction": structure_state.current_reaction,
        "htf_bias": structure_state.htf_bias,
        "local_bias": structure_state.local_bias,
        "anchor_level_price": anchor_level.price,
        "anchor_level_score": anchor_level.score,
        "atr_m5": float(atr_m5),
        "structure_confidence": structure_state.confidence,
    }

    return Signal(
        pair=pair.upper(),
        direction=direction,
        day_type=day_type,
        strategy_name=_STRATEGY_NAME,
        suggested_entry_price=entry_price,
        suggested_sl_price=sl_price,
        suggested_tp_price=None,
        confidence_score=confidence,
        source_candle_ts=source_ts,
        invalid_after_candle_ts=compute_invalid_after(source_ts),
        debug=debug,
    )


def _direction_from(state: StructureState) -> Optional[Direction]:
    reaction = state.current_reaction
    if state.htf_bias == "BEARISH" and reaction == _BEARISH_REACTION:
        return Direction.BEARISH
    if state.htf_bias == "BULLISH" and reaction == _BULLISH_REACTION:
        return Direction.BULLISH
    return None


__all__ = ["detect_ema_pullback"]
