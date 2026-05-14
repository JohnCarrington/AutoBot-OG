"""Bollinger Bands."""
from __future__ import annotations

import pandas as pd


def add_bollinger(
    df: pd.DataFrame,
    period: int = 20,
    std_mult: float = 2.0,
) -> pd.DataFrame:
    """Append Bollinger Band columns to a copy of ``df``.

    Columns appended (``s = int(std_mult)``)::

        bb_mid_{period}_{s}    = SMA(close, period)
        bb_upper_{period}_{s}  = bb_mid + std_mult * stdev(close, period)
        bb_lower_{period}_{s}  = bb_mid - std_mult * stdev(close, period)
        bb_width_{period}_{s}  = bb_upper - bb_lower            (raw, NOT ATR-normalised)

    The rolling standard deviation uses **population std** (``ddof=0``) to
    match the convention used by TradingView, MetaTrader, and ta-lib for
    Bollinger Bands. The first ``period - 1`` rows are NaN.

    Note on column naming: the suffix is ``int(std_mult)``, so ``std_mult=2.0``
    and ``std_mult=2.5`` would collide on ``..._2``. Pass distinct integer-cast
    multipliers if both are needed in the same DataFrame.

    Parameters
    ----------
    df : DataFrame
        Must contain a ``close`` column.
    period : int, default 20
        Rolling window size. Must be ``>= 1``.
    std_mult : float, default 2.0
        Number of standard deviations for the upper / lower bands.

    Returns
    -------
    DataFrame
        Copy of ``df`` with four new BB columns.
    """
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    if "close" not in df.columns:
        raise ValueError("input DataFrame must contain a 'close' column")

    out = df.copy()
    close = df["close"]
    rolling = close.rolling(window=period, min_periods=period)
    mid = rolling.mean()
    std = rolling.std(ddof=0)
    upper = mid + std_mult * std
    lower = mid - std_mult * std

    s = int(std_mult)
    out[f"bb_mid_{period}_{s}"] = mid
    out[f"bb_upper_{period}_{s}"] = upper
    out[f"bb_lower_{period}_{s}"] = lower
    out[f"bb_width_{period}_{s}"] = upper - lower
    return out
