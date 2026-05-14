"""EMA Continuation strategy (TREND regime).

See ``docs/v1_architecture.md`` §5.2. Stateless 3-bar inspection.

Pattern (LONG bullish TREND; SHORT bearish TREND mirrors)
---------------------------------------------------------
1. **Pullback** — ``pullback.low <= ema_50`` (wick-penetrates EMA50)
   AND ``pullback.close >= ema_50 - EMA_PULLBACK_CLOSE_TOLERANCE_PIPS``
   (close may sit slightly past, controlled by env tunable).
2. **Reclaim** — ``reclaim.close > ema_50``.
3. **Confirmation** — bullish-bodied bar that closes above the reclaim
   and above EMA50: ``confirmation.close > reclaim.close``,
   ``confirmation.close > confirmation.ema_50``,
   ``confirmation.close > confirmation.open``.

Plus a structure cross-check: ``get_structure_state(df_m5).recent_pattern``
must agree with the TREND direction (HH/HL for bullish, LH/LL for
bearish).
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
    EMA_CONT_ATR_MULT,
    EMA_CONT_CONF_HIGH,
    EMA_CONT_CONF_LOW,
    EMA_PULLBACK_CLOSE_TOLERANCE_PIPS,
)
from .signal import Signal, compute_invalid_after


_STRATEGY_NAME = "ema_continuation"

# Gate threshold from §5.2.
_SLOPE_TREND_MIN = 0.35

_BULLISH_PATTERNS = frozenset({"HH", "HL"})
_BEARISH_PATTERNS = frozenset({"LH", "LL"})


def detect_ema_continuation(
    df_m5: pd.DataFrame,
    df_h1: pd.DataFrame,
    regime_state: RegimeState,
    pair: str,
    current_time: datetime,  # noqa: ARG001 — kept for dispatcher uniformity
) -> Optional[Signal]:
    """Return a Signal for a confirmed TREND continuation, else ``None``."""
    if regime_state.get("current_regime") != RegimeLabel.TREND.value:
        return None
    direction_str = regime_state.get("current_direction")
    if direction_str not in (Direction.BULLISH.value, Direction.BEARISH.value):
        return None
    direction = Direction(direction_str)

    if len(df_m5) < 3 or len(df_h1) == 0:
        return None

    h1 = df_h1.iloc[-1]
    slope = _safe(h1, "ema_slope_norm_50_10")
    if math.isnan(slope) or abs(slope) <= _SLOPE_TREND_MIN:
        return None

    # Structure alignment — the structure module already considers swing
    # confirmation lag in its own state, so we trust ``recent_pattern``
    # directly.
    structure = get_structure_state(df_m5)
    recent_pattern = structure["recent_pattern"]
    aligned_patterns = (
        _BULLISH_PATTERNS
        if direction == Direction.BULLISH
        else _BEARISH_PATTERNS
    )
    if recent_pattern not in aligned_patterns:
        return None

    pullback, reclaim, confirmation = (
        df_m5.iloc[-3],
        df_m5.iloc[-2],
        df_m5.iloc[-1],
    )

    if not _pattern_matches(direction, pullback, reclaim, confirmation, pair):
        return None

    atr_m5 = _safe(confirmation, "atr_14")
    if math.isnan(atr_m5) or atr_m5 <= 0:
        return None

    anchor_price = (
        float(pullback["low"])
        if direction == Direction.BULLISH
        else float(pullback["high"])
    )
    sl_price = _build_sl(
        direction=direction,
        anchor_price=anchor_price,
        atr_m5=atr_m5,
        pair=pair,
    )
    confidence = _confidence(direction=direction, h1=h1)
    source_ts = confirmation.name
    if not isinstance(source_ts, datetime):
        return None

    debug: dict[str, Any] = {
        "slope_norm": float(slope),
        "atr_m5": float(atr_m5),
        "recent_pattern": recent_pattern,
        "pullback_low": float(pullback["low"]),
        "pullback_high": float(pullback["high"]),
        "pullback_close": float(pullback["close"]),
        "pullback_ema_50": float(_safe(pullback, "ema_50")),
        "reclaim_close": float(reclaim["close"]),
        "reclaim_ema_50": float(_safe(reclaim, "ema_50")),
        "confirmation_close": float(confirmation["close"]),
        "confirmation_open": float(confirmation["open"]),
        "anchor_price": anchor_price,
        "macd_hist_h1": float(_safe(h1, "macd_hist_12_26_9")),
    }

    return Signal(
        pair=pair.upper(),
        direction=direction,
        regime=RegimeLabel.TREND,
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


def _pattern_matches(
    direction: Direction,
    pullback: pd.Series,
    reclaim: pd.Series,
    confirmation: pd.Series,
    pair: str,
) -> bool:
    """Apply the LONG / SHORT pattern test described in the module docstring."""
    tol_price = EMA_PULLBACK_CLOSE_TOLERANCE_PIPS * pip_size_for(pair)

    pullback_low = _safe(pullback, "low")
    pullback_high = _safe(pullback, "high")
    pullback_close = _safe(pullback, "close")
    pullback_ema = _safe(pullback, "ema_50")
    reclaim_close = _safe(reclaim, "close")
    reclaim_ema = _safe(reclaim, "ema_50")
    conf_open = _safe(confirmation, "open")
    conf_close = _safe(confirmation, "close")
    conf_ema = _safe(confirmation, "ema_50")

    nans = [
        pullback_low,
        pullback_high,
        pullback_close,
        pullback_ema,
        reclaim_close,
        reclaim_ema,
        conf_open,
        conf_close,
        conf_ema,
    ]
    if any(math.isnan(v) for v in nans):
        return False

    if direction == Direction.BULLISH:
        return (
            pullback_low <= pullback_ema
            and pullback_close >= pullback_ema - tol_price
            and reclaim_close > reclaim_ema
            and conf_close > reclaim_close
            and conf_close > conf_ema
            and conf_close > conf_open
        )
    # BEARISH mirror
    return (
        pullback_high >= pullback_ema
        and pullback_close <= pullback_ema + tol_price
        and reclaim_close < reclaim_ema
        and conf_close < reclaim_close
        and conf_close < conf_ema
        and conf_close < conf_open
    )


def _build_sl(
    *,
    direction: Direction,
    anchor_price: float,
    atr_m5: float,
    pair: str,
) -> float:
    atr_pips = price_to_pips(pair, atr_m5)
    floor_pips = MIN_SL_PIPS.get(pair.upper(), 12.0)
    sl_pips = max(floor_pips, EMA_CONT_ATR_MULT * atr_pips)
    sl_distance = sl_pips * pip_size_for(pair)
    return (
        anchor_price - sl_distance
        if direction == Direction.BULLISH
        else anchor_price + sl_distance
    )


def _confidence(*, direction: Direction, h1: pd.Series) -> float:
    hist = _safe(h1, "macd_hist_12_26_9")
    if math.isnan(hist) or hist == 0.0:
        return EMA_CONT_CONF_LOW
    aligned = (hist > 0 and direction == Direction.BULLISH) or (
        hist < 0 and direction == Direction.BEARISH
    )
    return EMA_CONT_CONF_HIGH if aligned else EMA_CONT_CONF_LOW


def _safe(row: pd.Series, column: str) -> float:
    value = row.get(column) if hasattr(row, "get") else None
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


__all__ = ["detect_ema_continuation"]
