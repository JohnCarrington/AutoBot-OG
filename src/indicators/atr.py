"""Average True Range (Wilder's smoothing)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Append Wilder's ATR to a copy of ``df``.

    True range for bar ``t``::

        TR[t] = max(H[t] - L[t], |H[t] - C[t-1]|, |L[t] - C[t-1]|)

    ``TR[0]`` is NaN (no prior close). Wilder's smoothing seeds the ATR at
    index ``period`` with the simple mean of ``TR[1..period]`` (i.e. the
    first ``period`` valid true ranges) and then recurses::

        ATR[t] = ATR[t-1] * (1 - 1/period) + TR[t] * (1/period)

    The first valid ATR is therefore at index ``period``; indices
    ``0..period-1`` remain NaN.

    Parameters
    ----------
    df : DataFrame
        Must contain ``high``, ``low``, and ``close`` columns.
    period : int, default 14
        Wilder period (``alpha = 1 / period``). Must be ``>= 1``.

    Returns
    -------
    DataFrame
        Copy of ``df`` with a new column ``f"atr_{period}"``.
    """
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    required = {"high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"input DataFrame missing required column(s): {sorted(missing)}"
        )

    out = df.copy()
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    n = len(tr)
    atr = np.full(n, np.nan, dtype=float)
    # Need at least one prior close (index 0 NaN in TR) + `period` valid TRs
    # to compute the SMA seed at index `period`.
    if n >= period + 1:
        seed = tr.iloc[1 : period + 1].mean()
        atr[period] = seed
        alpha = 1.0 / period
        one_minus_alpha = 1.0 - alpha
        tr_arr = tr.to_numpy()
        prev = seed
        for i in range(period + 1, n):
            prev = prev * one_minus_alpha + tr_arr[i] * alpha
            atr[i] = prev

    out[f"atr_{period}"] = pd.Series(atr, index=df.index)
    return out
