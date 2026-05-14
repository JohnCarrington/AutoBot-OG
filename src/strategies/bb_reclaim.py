"""Bollinger Reclaim strategy (RANGE regime).

See ``docs/v1_architecture.md`` §5.1 for the locked spec. This module
is *stateless* — every call inspects the last three M5 bars of the
supplied DataFrame.

Pattern (LONG; SHORT is the mirror)
-----------------------------------
1. **Pierce** — `df_m5.iloc[-3].close < bb_lower_20_2`.
2. **Rejection** — `bb_lower_20_2 <= df_m5.iloc[-2].close <= bb_upper_20_2`.
3. **Confirmation** — `df_m5.iloc[-1].close > df_m5.iloc[-2].close` AND
   `df_m5.iloc[-1].close > bb_lower_20_2`.

Stop is anchored to the *pierce wick*; target is the BB midline at the
pierce bar (the canonical reversion target for this setup).
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from config.pair_config import MIN_SL_PIPS, pip_size_for, price_to_pips
from regime.labels import Direction, RegimeLabel
from regime.state import RegimeState

from .constants import (
    BB_RECLAIM_ATR_MULT,
    BB_RECLAIM_CONF_HIGH,
    BB_RECLAIM_CONF_LOW,
)
from .signal import Signal, compute_invalid_after


_STRATEGY_NAME = "bb_reclaim"

# Gate thresholds from §5.1.
_BB_WIDTH_MAX = 1.8
_SLOPE_FLAT_MAX = 0.15


def detect_bb_reclaim(
    df_m5: pd.DataFrame,
    df_h1: pd.DataFrame,
    regime_state: RegimeState,
    pair: str,
    current_time: datetime,  # noqa: ARG001 — kept for dispatcher uniformity
) -> Optional[Signal]:
    """Return a :py:class:`Signal` if the last 3 M5 bars complete a BB
    reclaim setup; otherwise ``None``.

    See module docstring for the pattern definition. ``current_time`` is
    not used by this strategy (the M5 close timestamps drive everything),
    but every ``detect_*`` shares the same signature so the dispatcher
    can call them uniformly.
    """
    # --- Regime + indicator gates --------------------------------------------
    if regime_state.get("current_regime") != RegimeLabel.RANGE.value:
        return None
    if len(df_m5) < 3 or len(df_h1) == 0:
        return None

    h1 = df_h1.iloc[-1]
    bb_width = _safe(h1, "bb_width_norm_20_2")
    slope = _safe(h1, "ema_slope_norm_50_10")
    if math.isnan(bb_width) or bb_width >= _BB_WIDTH_MAX:
        return None
    if math.isnan(slope) or abs(slope) > _SLOPE_FLAT_MAX:
        return None

    pierce, rejection, confirmation = (
        df_m5.iloc[-3],
        df_m5.iloc[-2],
        df_m5.iloc[-1],
    )

    # --- Try LONG then SHORT -------------------------------------------------
    setup = _try_long(pierce, rejection, confirmation) or _try_short(
        pierce, rejection, confirmation
    )
    if setup is None:
        return None
    direction, anchor_price = setup

    atr_m5 = _safe(confirmation, "atr_14")
    if math.isnan(atr_m5) or atr_m5 <= 0:
        return None

    sl_price = _build_sl(
        direction=direction,
        anchor_price=anchor_price,
        atr_m5=atr_m5,
        pair=pair,
    )
    tp_price = _safe(pierce, "bb_mid_20_2")
    if math.isnan(tp_price):
        return None

    confidence = _confidence(direction=direction, h1=h1)
    source_ts = confirmation.name
    if not isinstance(source_ts, datetime):
        return None

    debug: dict[str, Any] = {
        "bb_width_norm": float(bb_width),
        "slope_norm": float(slope),
        "atr_m5": float(atr_m5),
        "pierce_close": float(pierce["close"]),
        "rejection_close": float(rejection["close"]),
        "confirmation_close": float(confirmation["close"]),
        "anchor_wick_price": float(anchor_price),
        "macd_hist_h1": float(_safe(h1, "macd_hist_12_26_9")),
    }

    return Signal(
        pair=pair.upper(),
        direction=direction,
        regime=RegimeLabel.RANGE,
        strategy_name=_STRATEGY_NAME,
        suggested_entry_price=float(confirmation["close"]),
        suggested_sl_price=sl_price,
        suggested_tp_price=float(tp_price),
        confidence_score=confidence,
        source_candle_ts=source_ts,
        invalid_after_candle_ts=compute_invalid_after(source_ts),
        debug=debug,
    )


# --- Pattern helpers --------------------------------------------------------


def _try_long(
    pierce: pd.Series,
    rejection: pd.Series,
    confirmation: pd.Series,
) -> Optional[tuple[Direction, float]]:
    """Return ``(BULLISH, pierce_low)`` if a LONG setup is present."""
    pierce_close = _safe(pierce, "close")
    pierce_lower = _safe(pierce, "bb_lower_20_2")
    if math.isnan(pierce_close) or math.isnan(pierce_lower):
        return None
    if not pierce_close < pierce_lower:
        return None

    rej_close = _safe(rejection, "close")
    rej_lower = _safe(rejection, "bb_lower_20_2")
    rej_upper = _safe(rejection, "bb_upper_20_2")
    if any(math.isnan(v) for v in (rej_close, rej_lower, rej_upper)):
        return None
    if not (rej_lower <= rej_close <= rej_upper):
        return None

    conf_close = _safe(confirmation, "close")
    conf_lower = _safe(confirmation, "bb_lower_20_2")
    if math.isnan(conf_close) or math.isnan(conf_lower):
        return None
    if not (conf_close > rej_close and conf_close > conf_lower):
        return None

    return Direction.BULLISH, float(pierce["low"])


def _try_short(
    pierce: pd.Series,
    rejection: pd.Series,
    confirmation: pd.Series,
) -> Optional[tuple[Direction, float]]:
    """Return ``(BEARISH, pierce_high)`` if a SHORT setup is present."""
    pierce_close = _safe(pierce, "close")
    pierce_upper = _safe(pierce, "bb_upper_20_2")
    if math.isnan(pierce_close) or math.isnan(pierce_upper):
        return None
    if not pierce_close > pierce_upper:
        return None

    rej_close = _safe(rejection, "close")
    rej_lower = _safe(rejection, "bb_lower_20_2")
    rej_upper = _safe(rejection, "bb_upper_20_2")
    if any(math.isnan(v) for v in (rej_close, rej_lower, rej_upper)):
        return None
    if not (rej_lower <= rej_close <= rej_upper):
        return None

    conf_close = _safe(confirmation, "close")
    conf_upper = _safe(confirmation, "bb_upper_20_2")
    if math.isnan(conf_close) or math.isnan(conf_upper):
        return None
    if not (conf_close < rej_close and conf_close < conf_upper):
        return None

    return Direction.BEARISH, float(pierce["high"])


def _build_sl(
    *,
    direction: Direction,
    anchor_price: float,
    atr_m5: float,
    pair: str,
) -> float:
    """Compute SL price using ``max(MIN_SL_PIPS, 0.8 × ATR_M5)``."""
    atr_pips = price_to_pips(pair, atr_m5)
    floor_pips = MIN_SL_PIPS.get(pair.upper(), 12.0)
    sl_pips = max(floor_pips, BB_RECLAIM_ATR_MULT * atr_pips)
    sl_distance = sl_pips * pip_size_for(pair)
    return (
        anchor_price - sl_distance
        if direction == Direction.BULLISH
        else anchor_price + sl_distance
    )


def _confidence(*, direction: Direction, h1: pd.Series) -> float:
    """High if MACD-H1 histogram agrees with the direction; else low."""
    hist = _safe(h1, "macd_hist_12_26_9")
    if math.isnan(hist) or hist == 0.0:
        return BB_RECLAIM_CONF_LOW
    aligned = (hist > 0 and direction == Direction.BULLISH) or (
        hist < 0 and direction == Direction.BEARISH
    )
    return BB_RECLAIM_CONF_HIGH if aligned else BB_RECLAIM_CONF_LOW


def _safe(row: pd.Series, column: str) -> float:
    """Return a float for ``row[column]`` or NaN when missing / non-numeric."""
    value = row.get(column) if hasattr(row, "get") else None
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


__all__ = ["detect_bb_reclaim"]
