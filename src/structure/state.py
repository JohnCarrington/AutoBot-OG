"""Most-recent-state summary derived from fractal swing columns."""
from __future__ import annotations

from typing import TypedDict

import numpy as np
import pandas as pd


class StructureState(TypedDict):
    """Snapshot of the most recent structure state at the tail of a DataFrame."""

    last_swing_high: float | None
    last_swing_low: float | None
    swing_high_age_bars: int | None
    swing_low_age_bars: int | None
    recent_pattern: str


_REQUIRED_COLUMNS = ("swing_high", "swing_low", "high", "low")


def get_structure_state(
    df: pd.DataFrame, lookback_bars: int = 10
) -> StructureState:
    """Summarise the most recent swing structure at the tail of ``df``.

    The function inspects the *current* (tail) state only — it does not
    iterate over historical states. Callers wanting a per-bar state history
    should slice ``df`` and call this repeatedly.

    Pattern semantics
    -----------------
    ``recent_pattern`` describes the latest confirmed structural event,
    chosen by which type of swing (high or low) appeared *last* within the
    lookback window:

    - ``"HH"`` — most recent event was a swing high, **higher** than the
      previous swing high (uptrend high print).
    - ``"LH"`` — most recent event was a swing high, **lower** than the
      previous swing high (downtrend high print).
    - ``"HL"`` — most recent event was a swing low, **higher** than the
      previous swing low (uptrend low print).
    - ``"LL"`` — most recent event was a swing low, **lower** than the
      previous swing low (downtrend low print).
    - ``"INSUFFICIENT_DATA"`` — fewer than 2 swing highs **or** fewer than
      2 swing lows within ``lookback_bars`` of the tail.

    A market can simultaneously be HH+HL (clean uptrend), HH+LL (volatile
    expansion), LH+HL (compression), etc.; only one of the four directional
    labels is returned here — the most recent event. Higher-level callers
    (e.g. the regime engine) decide how to combine highs and lows.

    Parameters
    ----------
    df : DataFrame
        Must contain ``swing_high``, ``swing_low``, ``high``, ``low``
        columns. Typically produced by ``add_fractal_swings``.
    lookback_bars : int, default 10
        Number of trailing bars considered when computing
        ``recent_pattern``. Must be ``>= 1``. The ``last_swing_*`` and
        ``*_age_bars`` fields ignore this window and always describe the
        most recent swing of each type across the whole DataFrame.

    Returns
    -------
    dict
        Keys: ``last_swing_high``, ``last_swing_low``,
        ``swing_high_age_bars``, ``swing_low_age_bars``, ``recent_pattern``.

    Raises
    ------
    ValueError
        If ``lookback_bars < 1`` or a required column is missing.
    """
    if lookback_bars < 1:
        raise ValueError(f"lookback_bars must be >= 1, got {lookback_bars}")
    missing = [c for c in _REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"input DataFrame missing required column(s): {missing}. "
            "Run add_fractal_swings first."
        )

    n = len(df)
    if n == 0:
        return {
            "last_swing_high": None,
            "last_swing_low": None,
            "swing_high_age_bars": None,
            "swing_low_age_bars": None,
            "recent_pattern": "INSUFFICIENT_DATA",
        }

    high_arr = df["high"].to_numpy()
    low_arr = df["low"].to_numpy()
    high_positions = np.flatnonzero(df["swing_high"].to_numpy())
    low_positions = np.flatnonzero(df["swing_low"].to_numpy())

    last_high = high_arr[high_positions[-1]] if high_positions.size else None
    last_low = low_arr[low_positions[-1]] if low_positions.size else None
    high_age = (
        int(n - 1 - high_positions[-1]) if high_positions.size else None
    )
    low_age = (
        int(n - 1 - low_positions[-1]) if low_positions.size else None
    )

    cutoff = n - lookback_bars  # inclusive lower bound (positions >= cutoff)
    recent_highs = high_positions[high_positions >= cutoff]
    recent_lows = low_positions[low_positions >= cutoff]

    if recent_highs.size >= 2 and recent_lows.size >= 2:
        most_recent_high_pos = int(recent_highs[-1])
        most_recent_low_pos = int(recent_lows[-1])

        if most_recent_high_pos >= most_recent_low_pos:
            prev_pos = int(recent_highs[-2])
            recent_pattern = (
                "HH"
                if high_arr[most_recent_high_pos] > high_arr[prev_pos]
                else "LH"
            )
        else:
            prev_pos = int(recent_lows[-2])
            recent_pattern = (
                "HL"
                if low_arr[most_recent_low_pos] > low_arr[prev_pos]
                else "LL"
            )
    else:
        recent_pattern = "INSUFFICIENT_DATA"

    return {
        "last_swing_high": float(last_high) if last_high is not None else None,
        "last_swing_low": float(last_low) if last_low is not None else None,
        "swing_high_age_bars": high_age,
        "swing_low_age_bars": low_age,
        "recent_pattern": recent_pattern,
    }
