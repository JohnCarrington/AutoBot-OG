"""N-bar fractal swing detection per timeframe (spec §5).

A swing HIGH at bar ``i`` is a bar whose ``high`` is strictly greater
than the ``high`` of the ``N`` bars on each side. Mirror condition on
``low`` for a swing LOW. The detector is *static* — it operates over a
whole DataFrame in one pass and is meant to be re-run every BAR_CLOSE.

Window defaults (in :mod:`structure_engine.constants`):

- H1 : 2 bars left/right
- M15: 2 bars left/right
- M5 : 3 bars left/right

Strength is a 0–1 score combining bar-displacement after the swing,
wick size, and recency. It feeds the level scorer (spec §8 reaction
score).
"""
from __future__ import annotations

import math
from typing import Iterable

import pandas as pd

from .constants import (
    SWING_WINDOW_H1,
    SWING_WINDOW_M15,
    SWING_WINDOW_M5,
)
from .types import Swing, Timeframe


_WINDOW_BY_TIMEFRAME: dict[str, int] = {
    "H1": SWING_WINDOW_H1,
    "M15": SWING_WINDOW_M15,
    "M5": SWING_WINDOW_M5,
}


def detect_swings(df: pd.DataFrame, timeframe: Timeframe) -> list[Swing]:
    """Return all confirmed swing points in ``df`` for ``timeframe``.

    A swing at index ``i`` is *confirmed* only when ``i + window`` bars
    exist after it. The last ``window`` bars therefore never contain a
    confirmed swing — strategies that need the swing buffer to be
    populated for the very latest bar must accept this lag (it's the
    same lag the legacy ``add_fractal_swings`` carries).
    """
    if df is None or df.empty:
        return []
    if "high" not in df.columns or "low" not in df.columns:
        return []
    window = _WINDOW_BY_TIMEFRAME.get(timeframe)
    if window is None or window < 1:
        return []
    if len(df) < 2 * window + 1:
        return []

    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = (
        df["close"].to_numpy(dtype=float)
        if "close" in df.columns
        else highs
    )
    opens = (
        df["open"].to_numpy(dtype=float)
        if "open" in df.columns
        else closes
    )
    timestamps = list(df.index)

    atr_series = _resolve_atr(df)

    swings: list[Swing] = []
    last_index = len(df) - window
    for i in range(window, last_index):
        # HIGH: strictly greater than each of the 2*window neighbours.
        high_i = highs[i]
        if not math.isnan(high_i) and _strictly_greater(highs, i, window):
            swings.append(
                _build_swing(
                    type="HIGH",
                    bar_index=i,
                    price=float(high_i),
                    timestamp=timestamps[i],
                    timeframe=timeframe,
                    highs=highs,
                    lows=lows,
                    closes=closes,
                    opens=opens,
                    atr_at_i=atr_series[i] if atr_series is not None else None,
                    total_bars=len(df),
                )
            )
            continue
        low_i = lows[i]
        if not math.isnan(low_i) and _strictly_less(lows, i, window):
            swings.append(
                _build_swing(
                    type="LOW",
                    bar_index=i,
                    price=float(low_i),
                    timestamp=timestamps[i],
                    timeframe=timeframe,
                    highs=highs,
                    lows=lows,
                    closes=closes,
                    opens=opens,
                    atr_at_i=atr_series[i] if atr_series is not None else None,
                    total_bars=len(df),
                )
            )

    return swings


def _strictly_greater(values, i: int, window: int) -> bool:
    """``values[i]`` strictly greater than each of the ``2*window`` neighbours."""
    target = values[i]
    for j in range(i - window, i + window + 1):
        if j == i:
            continue
        v = values[j]
        if math.isnan(v):
            return False
        if v >= target:
            return False
    return True


def _strictly_less(values, i: int, window: int) -> bool:
    target = values[i]
    for j in range(i - window, i + window + 1):
        if j == i:
            continue
        v = values[j]
        if math.isnan(v):
            return False
        if v <= target:
            return False
    return True


def _resolve_atr(df: pd.DataFrame):
    """Return the ATR series for strength scoring, or None.

    The Phase 2 indicator pipeline emits ``atr_14``. Spec §2 allows
    ``atr_m5`` as the column name; we accept either.
    """
    for col in ("atr_14", "atr_m5", "atr"):
        if col in df.columns:
            return df[col].to_numpy(dtype=float)
    return None


def _build_swing(
    *,
    type: str,
    bar_index: int,
    price: float,
    timestamp,
    timeframe: Timeframe,
    highs,
    lows,
    closes,
    opens,
    atr_at_i,
    total_bars: int,
) -> Swing:
    strength = _compute_strength(
        type=type,
        i=bar_index,
        price=price,
        highs=highs,
        lows=lows,
        closes=closes,
        opens=opens,
        atr_at_i=atr_at_i,
        total_bars=total_bars,
    )
    return Swing(
        type="HIGH" if type == "HIGH" else "LOW",
        price=price,
        timestamp=timestamp,
        timeframe=timeframe,
        strength=strength,
        bar_index=bar_index,
    )


def _compute_strength(
    *,
    type: str,
    i: int,
    price: float,
    highs,
    lows,
    closes,
    opens,
    atr_at_i,
    total_bars: int,
) -> float:
    """Return a 0–1 strength score.

    Three components, equal weight:

    1. Post-swing displacement — how far price travelled away from the
       swing in the bars after it, in ATR units.
    2. Rejection wick — how much of the swing bar's range is wick vs
       body, on the relevant side.
    3. Recency — newer swings score higher (rolling decay).
    """
    displacement = _displacement_atr(
        type=type, i=i, price=price, closes=closes, atr_at_i=atr_at_i
    )
    wick = _wick_ratio(
        type=type, i=i, highs=highs, lows=lows, opens=opens, closes=closes
    )
    recency = _recency(i=i, total_bars=total_bars)

    raw = (displacement + wick + recency) / 3.0
    return max(0.0, min(1.0, raw))


def _displacement_atr(
    *, type: str, i: int, price: float, closes, atr_at_i
) -> float:
    """Distance closed beyond the swing in the next 3 bars, in ATR units."""
    if atr_at_i is None or math.isnan(atr_at_i) or atr_at_i <= 0:
        return 0.5  # neutral when ATR is unavailable
    end = min(len(closes), i + 4)
    samples: list[float] = []
    for j in range(i + 1, end):
        c = closes[j]
        if math.isnan(c):
            continue
        if type == "HIGH":
            samples.append(price - c)  # close pulled away below the swing high
        else:
            samples.append(c - price)
    if not samples:
        return 0.5
    travel = max(samples)
    if travel <= 0:
        return 0.0
    # Normalise: 1.0 ATR of travel = 1.0 score.
    return min(1.0, travel / atr_at_i)


def _wick_ratio(*, type: str, i: int, highs, lows, opens, closes) -> float:
    high = highs[i]
    low = lows[i]
    o = opens[i]
    c = closes[i]
    if any(math.isnan(v) for v in (high, low, o, c)):
        return 0.5
    bar_range = high - low
    if bar_range <= 0:
        return 0.0
    body_top = max(o, c)
    body_bottom = min(o, c)
    if type == "HIGH":
        wick = high - body_top
    else:
        wick = body_bottom - low
    return max(0.0, min(1.0, wick / bar_range))


def _recency(*, i: int, total_bars: int) -> float:
    """Decay by absolute distance from the latest bar.

    Earlier code divided ``i`` by ``total_bars - 1``, which was non-deterministic
    across buffer growth: a swing at fixed ``bar_index=50`` scored ``50/99``
    in a 100-bar call and ``50/199`` in a 200-bar call (M-2 review fix,
    2026-05-16).

    The new formula uses *absolute* bars-from-the-right: a swing 10 bars
    back scores the same whether the buffer holds 100 or 1000 bars. The
    decay window is :data:`_RECENCY_DECAY_BARS`; older swings score 0.0.
    """
    if total_bars <= 1:
        return 1.0
    distance = (total_bars - 1) - i
    if distance <= 0:
        return 1.0
    if distance >= _RECENCY_DECAY_BARS:
        return 0.0
    return 1.0 - distance / _RECENCY_DECAY_BARS


# Window over which post-swing recency decays from 1.0 to 0.0. Stays bounded
# so adding more bars to the buffer never changes the score of a fixed swing.
_RECENCY_DECAY_BARS: int = 60


__all__ = ["detect_swings"]
