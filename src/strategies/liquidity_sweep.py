"""Liquidity Sweep strategy (VOLATILE regime) — Phase 11 rewrite.

Reads :class:`StructureState` directly. The Structure Engine is the
source of truth for what counts as a "swept-and-reclaimed" level —
this module simply asserts the spec §13 Liquidity-Sweep gates and
emits a Signal.

Gates (spec §13)
----------------
SELL:
    - ``regime == VOLATILE`` (dispatcher enforces)
    - ``htf_bias in (BEARISH, NEUTRAL)``
    - ``liquidity_above is not None``
    - ``current_reaction == RESISTANCE_SWEEP_RECLAIM``
    - Session: London or NY only (Asia rejected per spec).

BUY:
    - ``regime == VOLATILE``
    - ``htf_bias in (BULLISH, NEUTRAL)``
    - ``liquidity_below is not None``
    - ``current_reaction == SUPPORT_SWEEP_RECLAIM``
    - Session: London or NY only.

SL anchors on the swept-zone's *outer* edge (above the wick for shorts,
below the wick for longs) plus ATR padding. TP is ``None`` — execution
uses its structure-trail exit.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from config.pair_config import MIN_SL_PIPS, pip_size_for, price_to_pips
from regime.labels import Direction, RegimeLabel
from regime.state import RegimeState
from structure_engine import StructureLevel, StructureState

from .constants import (
    LIQ_SWEEP_ATR_MULT,
    LIQ_SWEEP_CONF_HIGH,
    LIQ_SWEEP_CONF_LOW,
    LIQ_SWEEP_STRONG_ATR_FRACTION,
)
from .sessions import london_session, ny_session
from .signal import Signal, compute_invalid_after


_STRATEGY_NAME = "liquidity_sweep"
_logger = logging.getLogger(__name__)


def detect_liquidity_sweep(
    df_m5: pd.DataFrame,
    df_h1: pd.DataFrame,  # noqa: ARG001 — kept for dispatcher uniformity
    regime_state: RegimeState,
    structure_state: StructureState,
    pair: str,
    current_time: datetime,  # noqa: ARG001 — kept for dispatcher uniformity
) -> Optional[Signal]:
    """Return a Signal for a VOLATILE sweep-reversal, else ``None``."""
    if regime_state.get("current_regime") != RegimeLabel.VOLATILE.value:
        return None
    if not structure_state.is_valid:
        return None
    # Structure-mode gate — mirrors bb_reclaim's RANGE_BALANCE and
    # ema_continuation's TREND_CONTINUATION checks. A VOLATILE regime
    # without VOLATILE_SWEEP_ZONE mode means the H1 classifier called
    # the macro state volatile but the engine doesn't see price near a
    # liquidity pool right now — sweep setups would be speculative.
    if structure_state.structure_mode != "VOLATILE_SWEEP_ZONE":
        return None

    direction = _direction_from(structure_state)
    if direction is None:
        return None

    # Session gate — uses the latest M5 bar's timestamp for reproducibility
    # across live and replay runs (same rationale as the legacy strategy:
    # current_time may be wall-clock in backtests).
    source_ts = _latest_timestamp(df_m5)
    if source_ts is None:
        _logger.warning(
            "liquidity_sweep: df_m5 has non-DatetimeIndex; cannot evaluate "
            "session gate, returning None."
        )
        return None
    if not (london_session(source_ts) or ny_session(source_ts)):
        return None

    swept_level = (
        structure_state.nearest_support
        if direction == Direction.BULLISH
        else structure_state.nearest_resistance
    )
    if swept_level is None:
        return None

    sweep_extreme = _sweep_extreme(df_m5, direction)
    if sweep_extreme is None or math.isnan(sweep_extreme):
        return None

    atr_m5 = _latest_atr(df_m5)
    if math.isnan(atr_m5) or atr_m5 <= 0:
        return None

    entry_price = _latest_close(df_m5)
    if math.isnan(entry_price):
        return None

    sl_price = _build_sl(
        direction=direction,
        anchor_price=sweep_extreme,
        atr_m5=atr_m5,
        pair=pair,
    )
    confidence = _confidence(
        direction=direction,
        swept_level=swept_level,
        sweep_extreme=sweep_extreme,
        atr_m5=atr_m5,
    )

    debug: dict[str, Any] = {
        "current_reaction": structure_state.current_reaction,
        "htf_bias": structure_state.htf_bias,
        "swept_level_price": swept_level.price,
        "swept_level_score": swept_level.score,
        "sweep_extreme": float(sweep_extreme),
        "sweep_magnitude_price": float(abs(swept_level.price - sweep_extreme)),
        "atr_m5": float(atr_m5),
        "liquidity_above_price": (
            structure_state.liquidity_above.price
            if structure_state.liquidity_above
            else None
        ),
        "liquidity_below_price": (
            structure_state.liquidity_below.price
            if structure_state.liquidity_below
            else None
        ),
        "structure_confidence": structure_state.confidence,
    }

    return Signal(
        pair=pair.upper(),
        direction=direction,
        regime=RegimeLabel.VOLATILE,
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
    htf = state.htf_bias
    if (
        reaction == "SUPPORT_SWEEP_RECLAIM"
        and htf in ("BULLISH", "NEUTRAL")
        and state.liquidity_below is not None
    ):
        return Direction.BULLISH
    if (
        reaction == "RESISTANCE_SWEEP_RECLAIM"
        and htf in ("BEARISH", "NEUTRAL")
        and state.liquidity_above is not None
    ):
        return Direction.BEARISH
    return None


def _sweep_extreme(df_m5: pd.DataFrame, direction: Direction) -> Optional[float]:
    """Return the lowest low / highest high of the 3-bar reaction window."""
    if df_m5 is None or len(df_m5) < 3:
        return None
    window = df_m5.iloc[-3:]
    if direction == Direction.BULLISH:
        return float(window["low"].min())
    return float(window["high"].max())


def _build_sl(
    *,
    direction: Direction,
    anchor_price: float,
    atr_m5: float,
    pair: str,
) -> float:
    atr_pips = price_to_pips(pair, atr_m5)
    floor_pips = MIN_SL_PIPS.get(pair.upper(), 12.0)
    sl_pips = max(floor_pips, LIQ_SWEEP_ATR_MULT * atr_pips)
    sl_distance = sl_pips * pip_size_for(pair)
    return (
        anchor_price - sl_distance
        if direction == Direction.BULLISH
        else anchor_price + sl_distance
    )


def _confidence(
    *,
    direction: Direction,  # noqa: ARG001 — kept for symmetry with other strategies
    swept_level: StructureLevel,
    sweep_extreme: float,
    atr_m5: float,
) -> float:
    if atr_m5 <= 0:
        return LIQ_SWEEP_CONF_LOW
    magnitude = abs(swept_level.price - sweep_extreme)
    return (
        LIQ_SWEEP_CONF_HIGH
        if magnitude > LIQ_SWEEP_STRONG_ATR_FRACTION * atr_m5
        else LIQ_SWEEP_CONF_LOW
    )


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


__all__ = ["detect_liquidity_sweep"]
