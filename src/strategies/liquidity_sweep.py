"""Liquidity Sweep strategy (VOLATILE regime).

See ``docs/v1_architecture.md`` §5.3. Stateless 3-bar inspection.

Pattern (LONG fades a sweep of a swing *low*; SHORT mirrors a swing high)
-------------------------------------------------------------------------
1. **Sweep** — ``sweep.low < last_swing_low`` (the wick took the liquidity
   resting below the recent structural low).
2. **Reclaim** — ``reclaim.close > last_swing_low`` (the bar that
   reclaimed the swept level back inside structure).
3. **Confirmation** — ``confirmation.close > reclaim.close`` AND
   ``confirmation.close > confirmation.open`` (continuation in the
   reclaim direction).

Additional gates
----------------
- Session: London or NY only. Asia rejected per spec.
- Swing source: ``get_structure_state(df_m5).last_swing_low``. Reject
  when the swing is older than :data:`STRATEGY_SWEEP_SWING_MAX_AGE_BARS`
  M5 bars (~2 hours) or absent.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from config.pair_config import MIN_SL_PIPS, pip_size_for, price_to_pips
from regime.labels import Direction, RegimeLabel
from regime.state import RegimeState
from structure import get_structure_state

from .constants import (
    LIQ_SWEEP_ATR_MULT,
    LIQ_SWEEP_CONF_HIGH,
    LIQ_SWEEP_CONF_LOW,
    LIQ_SWEEP_STRONG_ATR_FRACTION,
    SWEEP_SWING_MAX_AGE_BARS,
)
from .sessions import london_session, ny_session
from .signal import Signal, compute_invalid_after


_STRATEGY_NAME = "liquidity_sweep"


def detect_liquidity_sweep(
    df_m5: pd.DataFrame,
    df_h1: pd.DataFrame,
    regime_state: RegimeState,
    pair: str,
    current_time: datetime,
) -> Optional[Signal]:
    """Return a Signal for a confirmed VOLATILE sweep-reversal, else ``None``.

    ``df_h1`` is consulted only for the MACD confidence bump; the gate
    logic is M5-driven.
    """
    if regime_state.get("current_regime") != RegimeLabel.VOLATILE.value:
        return None
    # Session gate: Asia rejected.
    if not (london_session(current_time) or ny_session(current_time)):
        return None

    if len(df_m5) < 3:
        return None

    structure = get_structure_state(df_m5)
    sweep, reclaim, confirmation = (
        df_m5.iloc[-3],
        df_m5.iloc[-2],
        df_m5.iloc[-1],
    )

    setup = _try_long(
        sweep=sweep,
        reclaim=reclaim,
        confirmation=confirmation,
        structure=structure,
    ) or _try_short(
        sweep=sweep,
        reclaim=reclaim,
        confirmation=confirmation,
        structure=structure,
    )
    if setup is None:
        return None
    direction, swing_level, sweep_extreme = setup

    atr_m5 = _safe(confirmation, "atr_14")
    if math.isnan(atr_m5) or atr_m5 <= 0:
        return None

    sl_price = _build_sl(
        direction=direction,
        anchor_price=sweep_extreme,
        atr_m5=atr_m5,
        pair=pair,
    )
    confidence = _confidence(
        direction=direction,
        swing_level=swing_level,
        sweep_extreme=sweep_extreme,
        atr_m5=atr_m5,
    )
    source_ts = confirmation.name
    if not isinstance(source_ts, datetime):
        return None

    debug: dict[str, Any] = {
        "atr_m5": float(atr_m5),
        "swing_level": float(swing_level),
        "sweep_extreme": float(sweep_extreme),
        "sweep_magnitude_price": float(abs(swing_level - sweep_extreme)),
        "swing_age_bars": _swing_age_for(direction, structure),
        "reclaim_close": float(reclaim["close"]),
        "confirmation_close": float(confirmation["close"]),
        "confirmation_open": float(confirmation["open"]),
    }

    return Signal(
        pair=pair.upper(),
        direction=direction,
        regime=RegimeLabel.VOLATILE,
        strategy_name=_STRATEGY_NAME,
        suggested_entry_price=float(confirmation["close"]),
        suggested_sl_price=sl_price,
        suggested_tp_price=None,
        confidence_score=confidence,
        source_candle_ts=source_ts,
        invalid_after_candle_ts=compute_invalid_after(source_ts),
        debug=debug,
    )


# --- Pattern helpers --------------------------------------------------------


def _try_long(
    *,
    sweep: pd.Series,
    reclaim: pd.Series,
    confirmation: pd.Series,
    structure: dict,
) -> Optional[tuple[Direction, float, float]]:
    swing_level = structure.get("last_swing_low")
    age = structure.get("swing_low_age_bars")
    if swing_level is None or age is None or age > SWEEP_SWING_MAX_AGE_BARS:
        return None

    sweep_low = _safe(sweep, "low")
    if math.isnan(sweep_low) or not (sweep_low < swing_level):
        return None

    reclaim_close = _safe(reclaim, "close")
    if math.isnan(reclaim_close) or not (reclaim_close > swing_level):
        return None

    conf_open = _safe(confirmation, "open")
    conf_close = _safe(confirmation, "close")
    if math.isnan(conf_open) or math.isnan(conf_close):
        return None
    if not (conf_close > reclaim_close and conf_close > conf_open):
        return None

    return Direction.BULLISH, float(swing_level), float(sweep_low)


def _try_short(
    *,
    sweep: pd.Series,
    reclaim: pd.Series,
    confirmation: pd.Series,
    structure: dict,
) -> Optional[tuple[Direction, float, float]]:
    swing_level = structure.get("last_swing_high")
    age = structure.get("swing_high_age_bars")
    if swing_level is None or age is None or age > SWEEP_SWING_MAX_AGE_BARS:
        return None

    sweep_high = _safe(sweep, "high")
    if math.isnan(sweep_high) or not (sweep_high > swing_level):
        return None

    reclaim_close = _safe(reclaim, "close")
    if math.isnan(reclaim_close) or not (reclaim_close < swing_level):
        return None

    conf_open = _safe(confirmation, "open")
    conf_close = _safe(confirmation, "close")
    if math.isnan(conf_open) or math.isnan(conf_close):
        return None
    if not (conf_close < reclaim_close and conf_close < conf_open):
        return None

    return Direction.BEARISH, float(swing_level), float(sweep_high)


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
    swing_level: float,
    sweep_extreme: float,
    atr_m5: float,
) -> float:
    """High-confidence when the wick extended ≥ ``LIQ_SWEEP_STRONG_ATR_FRACTION``
    × ATR_M5 beyond the swept level; else low-confidence."""
    if atr_m5 <= 0:
        return LIQ_SWEEP_CONF_LOW
    magnitude = abs(swing_level - sweep_extreme)
    return (
        LIQ_SWEEP_CONF_HIGH
        if magnitude > LIQ_SWEEP_STRONG_ATR_FRACTION * atr_m5
        else LIQ_SWEEP_CONF_LOW
    )


def _swing_age_for(direction: Direction, structure: dict) -> Optional[int]:
    return (
        structure.get("swing_low_age_bars")
        if direction == Direction.BULLISH
        else structure.get("swing_high_age_bars")
    )


def _safe(row: pd.Series, column: str) -> float:
    value = row.get(column) if hasattr(row, "get") else None
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


__all__ = ["detect_liquidity_sweep"]
