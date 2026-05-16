"""Structure-mode classification per spec §11.

The structure mode is *not* the regime label. Regime (`regime/`) is the
top-level H1 classification that drives strategy routing; structure
mode is a finer-grained read on what price is doing right now (range
rotation vs trend acceptance vs sweep volatility vs none of the above).

Spec §13 gates each strategy on structure_mode in addition to the
regime gate the dispatcher applies. A TREND regime can still see no
signal if structure_mode disagrees — this is by design.
"""
from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from .constants import (
    MODE_RANGE_BB_WIDTH_MAX,
    MODE_TREND_SLOPE_MIN,
    MODE_VOLATILE_ATR_LOOKBACK,
    MODE_VOLATILE_ATR_MULT,
)
from .types import Direction, ReactionType, StructureMode


def classify_mode(
    *,
    df_m5: pd.DataFrame,
    df_h1: pd.DataFrame,
    htf_bias: Direction,
    local_bias: Direction,
    current_reaction: ReactionType,
    near_liquidity: bool,
) -> tuple[StructureMode, str]:
    """Return ``(structure_mode, reason)``.

    Decision order (first-match):

    1. **VOLATILE_SWEEP_ZONE** — elevated ATR relative to its
       lookback median AND price near a liquidity pool.
    2. **TREND_CONTINUATION** — directional H1 slope, HTF bias
       agrees, current reaction is an acceptance/failed-reclaim.
    3. **RANGE_BALANCE** — BB width compressed AND no acceptance
       break in play.
    4. **TRANSITION** — HTF + local disagree (bias mismatch) and
       neither above fires.
    5. **UNKNOWN** — fallback when input is too sparse to classify.
    """
    if df_m5 is None or df_m5.empty or df_h1 is None or df_h1.empty:
        return "UNKNOWN", "insufficient_data"

    latest_m5 = df_m5.iloc[-1]
    latest_h1 = df_h1.iloc[-1]

    atr_now = _safe_float(latest_m5.get("atr_14"))
    atr_median = _atr_lookback_median(df_m5, MODE_VOLATILE_ATR_LOOKBACK)
    bb_width = _safe_float(latest_h1.get("bb_width_norm_20_2"))
    slope = _safe_float(latest_h1.get("ema_slope_norm_50_10"))

    # 1. Volatile sweep zone
    if (
        not math.isnan(atr_now)
        and not math.isnan(atr_median)
        and atr_median > 0
        and atr_now > MODE_VOLATILE_ATR_MULT * atr_median
        and near_liquidity
    ):
        return "VOLATILE_SWEEP_ZONE", (
            f"atr={atr_now:.5f} > {MODE_VOLATILE_ATR_MULT}× median "
            f"{atr_median:.5f} and price near liquidity"
        )

    # 2. Trend continuation
    if (
        not math.isnan(slope)
        and abs(slope) > MODE_TREND_SLOPE_MIN
        and htf_bias != "NEUTRAL"
        and _slope_agrees_with_bias(slope, htf_bias)
        and current_reaction
        in (
            "SUPPORT_ACCEPTANCE_BREAK",
            "RESISTANCE_ACCEPTANCE_BREAK",
            "FAILED_RECLAIM_BELOW_SUPPORT",
            "FAILED_RECLAIM_ABOVE_RESISTANCE",
        )
    ):
        return "TREND_CONTINUATION", (
            f"slope={slope:.3f} agrees with htf={htf_bias} and "
            f"reaction={current_reaction}"
        )

    # 3. Range balance
    if (
        not math.isnan(bb_width)
        and bb_width <= MODE_RANGE_BB_WIDTH_MAX
        and current_reaction
        not in (
            "SUPPORT_ACCEPTANCE_BREAK",
            "RESISTANCE_ACCEPTANCE_BREAK",
        )
    ):
        return "RANGE_BALANCE", (
            f"bb_width={bb_width:.3f} ≤ {MODE_RANGE_BB_WIDTH_MAX} and no acceptance break"
        )

    # 4. Transition — HTF and local biases disagree, no clean mode.
    if (
        htf_bias != "NEUTRAL"
        and local_bias != "NEUTRAL"
        and htf_bias != local_bias
    ):
        return "TRANSITION", f"htf={htf_bias} disagrees with local={local_bias}"

    return "UNKNOWN", "no_mode_matched"


def _slope_agrees_with_bias(slope: float, htf_bias: Direction) -> bool:
    if htf_bias == "BULLISH":
        return slope > 0
    if htf_bias == "BEARISH":
        return slope < 0
    return False


def _atr_lookback_median(df: pd.DataFrame, lookback: int) -> float:
    col = "atr_14" if "atr_14" in df.columns else "atr_m5" if "atr_m5" in df.columns else None
    if col is None:
        return float("nan")
    series = df[col].dropna()
    if series.empty:
        return float("nan")
    tail = series.iloc[-lookback:] if len(series) >= lookback else series
    return float(tail.median())


def _safe_float(value) -> float:
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


__all__ = ["classify_mode"]
