"""Bollinger Reclaim strategy (RANGE regime) — Phase 11 rewrite.

The strategy is now a **thin wrapper** over :class:`StructureState`:
the Structure Engine identifies the support / resistance zones and
classifies the current reaction; this module's job is to check the
spec §13 BB-Reclaim gates and emit a Signal when they line up.

Gates (spec §13)
----------------
LONG:
    - ``structure_mode == RANGE_BALANCE``
    - ``nearest_support.score >= STRONG_LEVEL_THRESHOLD``
    - ``current_reaction in (SUPPORT_REJECTION, SUPPORT_SWEEP_RECLAIM)``

SHORT:
    - ``structure_mode == RANGE_BALANCE``
    - ``nearest_resistance.score >= STRONG_LEVEL_THRESHOLD``
    - ``current_reaction in (RESISTANCE_REJECTION, RESISTANCE_SWEEP_RECLAIM)``

The dispatcher also enforces ``regime == RANGE`` before calling this
function — RANGE remains the Phase 3 regime that routes here.

SL/TP
-----
SL is ATR-padded around the support/resistance zone edge that the
reaction occurred at; TP is the opposite zone's midpoint (mean-reversion
target). When the opposite zone is absent (one-sided structure), TP is
left ``None`` and the execution layer falls back to its structure-trail
exit.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from config.pair_config import MIN_SL_PIPS, pip_size_for, price_to_pips
from regime.labels import Direction, RegimeLabel
from regime.state import RegimeState
from structure_engine import StructureLevel, StructureState
from structure_engine.constants import STRONG_LEVEL_THRESHOLD

from .constants import (
    BB_RECLAIM_ATR_MULT,
    BB_RECLAIM_CONF_HIGH,
    BB_RECLAIM_CONF_LOW,
)
from .signal import Signal, compute_invalid_after


_STRATEGY_NAME = "bb_reclaim"

_LONG_REACTIONS = frozenset({"SUPPORT_REJECTION", "SUPPORT_SWEEP_RECLAIM"})
_SHORT_REACTIONS = frozenset({"RESISTANCE_REJECTION", "RESISTANCE_SWEEP_RECLAIM"})


def detect_bb_reclaim(
    df_m5: pd.DataFrame,
    df_h1: pd.DataFrame,
    regime_state: RegimeState,
    structure_state: StructureState,
    pair: str,
    current_time: datetime,  # noqa: ARG001 — kept for dispatcher uniformity
) -> Optional[Signal]:
    """Return a Signal when StructureState meets BB-Reclaim gates.

    ``df_h1`` is accepted for dispatcher symmetry but no longer drives
    pattern detection — the Structure Engine has already considered H1
    in its bias and mode classification.
    """
    if regime_state.get("current_regime") != RegimeLabel.RANGE.value:
        return None
    if not structure_state.is_valid:
        return None
    if structure_state.structure_mode != "RANGE_BALANCE":
        return None

    direction = _direction_from_reaction(
        reaction=structure_state.current_reaction,
        nearest_support=structure_state.nearest_support,
        nearest_resistance=structure_state.nearest_resistance,
    )
    if direction is None:
        return None

    atr_m5 = _latest_atr(df_m5)
    if math.isnan(atr_m5) or atr_m5 <= 0:
        return None

    if direction == Direction.BULLISH:
        anchor_level = structure_state.nearest_support
        target_level = structure_state.nearest_resistance
        anchor_price = anchor_level.zone_low if anchor_level is not None else None
    else:
        anchor_level = structure_state.nearest_resistance
        target_level = structure_state.nearest_support
        anchor_price = anchor_level.zone_high if anchor_level is not None else None

    if anchor_level is None or anchor_price is None:
        return None

    entry_price = _latest_close(df_m5)
    if math.isnan(entry_price):
        return None

    sl_price = _build_sl(
        direction=direction,
        anchor_price=anchor_price,
        atr_m5=atr_m5,
        pair=pair,
    )
    tp_price = _midpoint(target_level) if target_level is not None else None
    confidence = _confidence(direction=direction, df_h1=df_h1)
    source_ts = _latest_timestamp(df_m5)
    if source_ts is None:
        return None

    debug: dict[str, Any] = {
        "structure_mode": structure_state.structure_mode,
        "current_reaction": structure_state.current_reaction,
        "support_score": anchor_level.score if direction == Direction.BULLISH else None,
        "resistance_score": (
            anchor_level.score if direction == Direction.BEARISH else None
        ),
        "support_price": (
            structure_state.nearest_support.price
            if structure_state.nearest_support
            else None
        ),
        "resistance_price": (
            structure_state.nearest_resistance.price
            if structure_state.nearest_resistance
            else None
        ),
        "atr_m5": float(atr_m5),
        "htf_bias": structure_state.htf_bias,
        "local_bias": structure_state.local_bias,
        "structure_confidence": structure_state.confidence,
    }

    return Signal(
        pair=pair.upper(),
        direction=direction,
        regime=RegimeLabel.RANGE,
        strategy_name=_STRATEGY_NAME,
        suggested_entry_price=entry_price,
        suggested_sl_price=sl_price,
        suggested_tp_price=tp_price,
        confidence_score=confidence,
        source_candle_ts=source_ts,
        invalid_after_candle_ts=compute_invalid_after(source_ts),
        debug=debug,
    )


def _direction_from_reaction(
    *,
    reaction: str,
    nearest_support: Optional[StructureLevel],
    nearest_resistance: Optional[StructureLevel],
) -> Optional[Direction]:
    if reaction in _LONG_REACTIONS:
        if nearest_support is None:
            return None
        if nearest_support.score < STRONG_LEVEL_THRESHOLD:
            return None
        return Direction.BULLISH
    if reaction in _SHORT_REACTIONS:
        if nearest_resistance is None:
            return None
        if nearest_resistance.score < STRONG_LEVEL_THRESHOLD:
            return None
        return Direction.BEARISH
    return None


def _build_sl(
    *,
    direction: Direction,
    anchor_price: float,
    atr_m5: float,
    pair: str,
) -> float:
    atr_pips = price_to_pips(pair, atr_m5)
    floor_pips = MIN_SL_PIPS.get(pair.upper(), 12.0)
    sl_pips = max(floor_pips, BB_RECLAIM_ATR_MULT * atr_pips)
    sl_distance = sl_pips * pip_size_for(pair)
    return (
        anchor_price - sl_distance
        if direction == Direction.BULLISH
        else anchor_price + sl_distance
    )


def _midpoint(level: StructureLevel) -> float:
    return (level.zone_low + level.zone_high) / 2.0


def _confidence(*, direction: Direction, df_h1: pd.DataFrame) -> float:
    """High when the H1 MACD-hist sign agrees with direction; else low."""
    if df_h1 is None or df_h1.empty:
        return BB_RECLAIM_CONF_LOW
    hist = _safe_float(df_h1.iloc[-1].get("macd_hist_12_26_9"))
    if math.isnan(hist) or hist == 0.0:
        return BB_RECLAIM_CONF_LOW
    aligned = (hist > 0 and direction == Direction.BULLISH) or (
        hist < 0 and direction == Direction.BEARISH
    )
    return BB_RECLAIM_CONF_HIGH if aligned else BB_RECLAIM_CONF_LOW


def _latest_atr(df: pd.DataFrame) -> float:
    if df is None or df.empty:
        return float("nan")
    if "atr_14" not in df.columns:
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


__all__ = ["detect_bb_reclaim"]
