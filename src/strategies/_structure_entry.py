"""Shared structure-entry helpers (step 5b refactor).

Lifted byte-for-byte out of ema_pullback.py and structure_break.py
because the third structure-anchored detector (``detect_news``,
step 5b) needs the same primitives. Keeping a single canonical
implementation prevents drift across the three call sites.

Pure functions: each takes its inputs explicitly (df, direction,
pair, mults, …) and returns the computed value. No module state.

Naming: the previous in-file copies were leading-underscore module
privates. Lifting them into a shared module gives them a meaningful
import path, so the leading underscore is dropped — they're public
within the ``strategies`` package even though the module itself is
underscore-prefixed (the module is a strategies-internal seam, not
a strategies public API).
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

import pandas as pd

from common import Direction
from config.pair_config import MIN_SL_PIPS, pip_size_for, price_to_pips


def safe_float(value) -> float:
    """Coerce to float or NaN — never raises."""
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def latest_atr(df: pd.DataFrame) -> float:
    """Latest ``atr_14`` value from a candle DataFrame, or NaN."""
    if df is None or df.empty or "atr_14" not in df.columns:
        return float("nan")
    return safe_float(df["atr_14"].iloc[-1])


def latest_close(df: pd.DataFrame) -> float:
    """Latest ``close`` value, or NaN."""
    if df is None or df.empty or "close" not in df.columns:
        return float("nan")
    return safe_float(df["close"].iloc[-1])


def latest_timestamp(df: pd.DataFrame) -> Optional[datetime]:
    """Index of the latest row as a ``datetime``, or ``None``."""
    if df is None or df.empty:
        return None
    ts = df.index[-1]
    return ts if isinstance(ts, datetime) else None


def build_sl(
    *,
    direction: Direction,
    anchor_price: float,
    atr_m5: float,
    pair: str,
    sl_atr_mult: float,
    sl_floor_pips_override: float | None,
) -> float:
    """Compute SL price for a structure-anchored entry.

    SL = ``anchor_price`` ± max(floor, mult × ATR), where ``±`` flips
    by direction (BULLISH → SL below anchor, BEARISH → SL above).
    Floor is the pair's ``MIN_SL_PIPS`` unless ``sl_floor_pips_override``
    is supplied.
    """
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


def macd_aligned_confidence(
    *,
    direction: Direction,
    df_h1: pd.DataFrame,
    high: float,
    low: float,
) -> float:
    """Binary confidence from H1 MACD-histogram alignment with direction.

    Returns ``high`` when the histogram sign agrees with ``direction``,
    else ``low``. ``low`` is also returned when ``df_h1`` is empty or
    the histogram value is missing / zero — agnostic input → low
    confidence, the existing convention.
    """
    if df_h1 is None or df_h1.empty:
        return low
    hist = safe_float(df_h1.iloc[-1].get("macd_hist_12_26_9"))
    if math.isnan(hist) or hist == 0.0:
        return low
    aligned = (hist > 0 and direction == Direction.BULLISH) or (
        hist < 0 and direction == Direction.BEARISH
    )
    return high if aligned else low


__all__ = [
    "build_sl",
    "latest_atr",
    "latest_close",
    "latest_timestamp",
    "macd_aligned_confidence",
    "safe_float",
]
