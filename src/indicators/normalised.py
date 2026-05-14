"""ATR-normalised forms of other indicators.

These functions read previously computed indicator columns from the input
DataFrame; they do not recompute the underlying EMA / BB / ATR. Each raises
``ValueError`` with a clear message if a required input column is missing.
"""
from __future__ import annotations

import pandas as pd


def _require_columns(df: pd.DataFrame, columns: list[str], caller: str) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ValueError(
            f"{caller}: input DataFrame is missing required column(s) "
            f"{missing}. Run the appropriate prerequisite indicator first."
        )


def add_ema_slope_normalised(
    df: pd.DataFrame,
    period: int = 50,
    lookback: int = 10,
    atr_period: int = 14,
) -> pd.DataFrame:
    """Append the ATR-normalised EMA slope to a copy of ``df``.

    The slope is the change in EMA over ``lookback`` bars, scaled by the
    current ATR so the result is dimensionless and pair-agnostic::

        slope_norm[t] = (ema[t] - ema[t - lookback]) / atr[t]

    The first ``lookback`` rows of the slope are NaN by construction, plus
    any rows where the underlying EMA or ATR are themselves NaN.

    Parameters
    ----------
    df : DataFrame
        Must contain pre-computed ``f"ema_{period}"`` and
        ``f"atr_{atr_period}"`` columns.
    period : int, default 50
        EMA period (used only to look up the column name).
    lookback : int, default 10
        Number of bars over which to measure the EMA change.
    atr_period : int, default 14
        ATR period (used only to look up the column name).

    Returns
    -------
    DataFrame
        Copy of ``df`` with a new column ``f"ema_slope_norm_{period}_{lookback}"``.

    Raises
    ------
    ValueError
        If the required EMA or ATR columns are not present.
    """
    if lookback < 1:
        raise ValueError(f"lookback must be >= 1, got {lookback}")
    ema_col = f"ema_{period}"
    atr_col = f"atr_{atr_period}"
    _require_columns(df, [ema_col, atr_col], "add_ema_slope_normalised")

    out = df.copy()
    ema = df[ema_col]
    atr = df[atr_col]
    slope = ema - ema.shift(lookback)
    out[f"ema_slope_norm_{period}_{lookback}"] = slope / atr
    return out


def add_bb_width_normalised(
    df: pd.DataFrame,
    bb_period: int = 20,
    bb_std: float = 2.0,
    atr_period: int = 14,
) -> pd.DataFrame:
    """Append the ATR-normalised Bollinger Band width to a copy of ``df``.

    Computed as::

        bb_width_norm[t] = bb_width[t] / atr[t]

    Parameters
    ----------
    df : DataFrame
        Must contain pre-computed ``f"bb_width_{bb_period}_{int(bb_std)}"``
        and ``f"atr_{atr_period}"`` columns.
    bb_period : int, default 20
        Bollinger period (used only to look up the column name).
    bb_std : float, default 2.0
        Bollinger std multiplier (column suffix uses ``int(bb_std)``).
    atr_period : int, default 14
        ATR period (used only to look up the column name).

    Returns
    -------
    DataFrame
        Copy of ``df`` with a new column ``f"bb_width_norm_{bb_period}_{int(bb_std)}"``.

    Raises
    ------
    ValueError
        If the required BB-width or ATR columns are not present.
    """
    s = int(bb_std)
    width_col = f"bb_width_{bb_period}_{s}"
    atr_col = f"atr_{atr_period}"
    _require_columns(df, [width_col, atr_col], "add_bb_width_normalised")

    out = df.copy()
    out[f"bb_width_norm_{bb_period}_{s}"] = df[width_col] / df[atr_col]
    return out
