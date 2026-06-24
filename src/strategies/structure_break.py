"""Structure-Break (ACCEPTANCE_BREAK) strategy — clean-swap step 3.

The B-5 trading decision: a structural level (support / resistance)
breaks AND the market accepts beyond it — at least
``ACCEPTANCE_MIN_CLOSES`` consecutive closes on the far side of the
zone — confirming the break is real and continuation is on. The
Structure Engine already classifies this state as
``ReactionType.{SUPPORT,RESISTANCE}_ACCEPTANCE_BREAK`` (see
``src/structure_engine/reaction_detector.py`` §10 E/F); this detector
just consumes the reaction.

Gates (B-5 ACCEPTANCE split)
----------------------------
SELL:
    - ``structure_state.is_valid``
    - ``structure_mode == TREND_CONTINUATION``
    - ``htf_bias == BEARISH``
    - ``current_reaction == SUPPORT_ACCEPTANCE_BREAK``

BUY:
    - ``structure_state.is_valid``
    - ``structure_mode == TREND_CONTINUATION``
    - ``htf_bias == BULLISH``
    - ``current_reaction == RESISTANCE_ACCEPTANCE_BREAK``

The dispatcher routes this detector on ``DayType.BIG_NEWS_DAY`` and
``DayType.PRE_BIG_NEWS`` (alongside ema_pullback).

SL/TP
-----
SL anchors on the broken-and-accepted zone edge plus ATR padding —
mirror of ema_pullback's structure anchor. TP is ``None`` (structure
trail by execution.sl_management).

Direction source
----------------
``structure_state.htf_bias`` — verified via Phase-0 interface read
(``last_bos`` does not exist; the engine surfaces direction via
``htf_bias`` only).
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from common import Direction
from config.pair_config import MIN_SL_PIPS, pip_size_for, price_to_pips
from day_type import DayType
from structure_engine import StructureLevel, StructureState

from .constants import (
    STRUCT_BREAK_CONF_HIGH,
    STRUCT_BREAK_CONF_LOW,
)
from .management import profile_for
from .signal import Signal, compute_invalid_after


_STRATEGY_NAME = "structure_break"

# B-5 split: structure_break consumes the ACCEPTANCE reactions only.
# ema_pullback continues to handle the FAILED_RECLAIM reactions.
_BEARISH_REACTION = "SUPPORT_ACCEPTANCE_BREAK"
_BULLISH_REACTION = "RESISTANCE_ACCEPTANCE_BREAK"


def detect_structure_break(
    df_m5: pd.DataFrame,
    df_h1: pd.DataFrame,
    day_type: DayType,
    structure_state: StructureState,
    pair: str,
    current_time: datetime,  # noqa: ARG001 — kept for dispatcher uniformity
) -> Optional[Signal]:
    """Return a Signal for a structural acceptance break, else ``None``."""
    if not structure_state.is_valid:
        return None
    if structure_state.structure_mode != "TREND_CONTINUATION":
        return None

    direction = _direction_from(structure_state)
    if direction is None:
        return None

    # The reaction's level is the one we broke AND accepted past.
    if direction == Direction.BEARISH:
        anchor_level = structure_state.nearest_support
        if anchor_level is None:
            return None
        anchor_price = anchor_level.zone_high  # SL above the broken support
    else:
        anchor_level = structure_state.nearest_resistance
        if anchor_level is None:
            return None
        anchor_price = anchor_level.zone_low  # SL below the broken resistance

    atr_m5 = _latest_atr(df_m5)
    if math.isnan(atr_m5) or atr_m5 <= 0:
        return None

    entry_price = _latest_close(df_m5)
    if math.isnan(entry_price):
        return None

    profile = profile_for(_STRATEGY_NAME, day_type)
    sl_price = _build_sl(
        direction=direction,
        anchor_price=anchor_price,
        atr_m5=atr_m5,
        pair=pair,
        sl_atr_mult=profile.sl_atr_mult,
        sl_floor_pips_override=profile.sl_floor_pips_override,
    )
    confidence = _confidence(direction=direction, df_h1=df_h1)
    source_ts = _latest_timestamp(df_m5)
    if source_ts is None:
        return None

    debug: dict[str, Any] = {
        "structure_mode": structure_state.structure_mode,
        "current_reaction": structure_state.current_reaction,
        "acceptance_state": structure_state.acceptance_state,
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


def _build_sl(
    *,
    direction: Direction,
    anchor_price: float,
    atr_m5: float,
    pair: str,
    sl_atr_mult: float,
    sl_floor_pips_override: float | None,
) -> float:
    atr_pips = price_to_pips(pair, atr_m5)
    floor_pips = (
        sl_floor_pips_override
        if sl_floor_pips_override is not None
        else MIN_SL_PIPS.get(pair.upper(), 12.0)
    )
    sl_pips = max(floor_pips, sl_atr_mult * atr_pips)
    sl_distance = sl_pips * pip_size_for(pair)
    return (
        anchor_price - sl_distance
        if direction == Direction.BULLISH
        else anchor_price + sl_distance
    )


def _confidence(*, direction: Direction, df_h1: pd.DataFrame) -> float:
    """High when MACD-H1 histogram agrees with the break direction.

    Mirrors ema_pullback's MACD-alignment confidence — both strategies
    are H1-trend continuations, so a histogram disagreement is a
    meaningful warning.
    """
    if df_h1 is None or df_h1.empty:
        return STRUCT_BREAK_CONF_LOW
    hist = _safe_float(df_h1.iloc[-1].get("macd_hist_12_26_9"))
    if math.isnan(hist) or hist == 0.0:
        return STRUCT_BREAK_CONF_LOW
    aligned = (hist > 0 and direction == Direction.BULLISH) or (
        hist < 0 and direction == Direction.BEARISH
    )
    return STRUCT_BREAK_CONF_HIGH if aligned else STRUCT_BREAK_CONF_LOW


def _latest_atr(df: pd.DataFrame) -> float:
    if df is None or df.empty or "atr_14" not in df.columns:
        return float("nan")
    return _safe_float(df["atr_14"].iloc[-1])


def _latest_close(df: pd.DataFrame) -> float:
    if df is None or df.empty or "close" not in df.columns:
        return float("nan")
    return _safe_float(df["close"].iloc[-1])


def _latest_timestamp(df: pd.DataFrame) -> Optional[datetime]:
    if df is None or df.empty:
        return None
    ts = df.index[-1]
    return ts if isinstance(ts, datetime) else None


def _safe_float(value) -> float:
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


__all__ = ["detect_structure_break"]
