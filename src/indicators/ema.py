"""Exponential moving average."""
from __future__ import annotations

import pandas as pd


def add_ema(df: pd.DataFrame, period: int) -> pd.DataFrame:
    """Append an EMA of the ``close`` column to a copy of ``df``.

    Uses pandas' recursive form (``adjust=False``):

        ema[0] = close[0]                         (used as seed, then masked NaN)
        ema[t] = alpha * close[t] + (1 - alpha) * ema[t-1]
        alpha  = 2 / (period + 1)

    With ``min_periods=period`` the first ``period - 1`` rows are NaN; the
    output at index ``period - 1`` is the recursive value seeded from
    ``close[0]``. This is the industry convention used when ``adjust=False``
    is specified (e.g. for MACD components).

    Parameters
    ----------
    df : DataFrame
        Must contain a ``close`` column.
    period : int
        EMA span. Must be ``>= 1``.

    Returns
    -------
    DataFrame
        Copy of ``df`` with a new column ``f"ema_{period}"``. Original
        columns are preserved unchanged.
    """
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    if "close" not in df.columns:
        raise ValueError("input DataFrame must contain a 'close' column")

    out = df.copy()
    out[f"ema_{period}"] = (
        df["close"].ewm(span=period, adjust=False, min_periods=period).mean()
    )
    return out
