"""MACD (Moving Average Convergence Divergence)."""
from __future__ import annotations

import pandas as pd


def add_macd(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """Append MACD line, signal, and histogram to a copy of ``df``.

    All three component EMAs use ``adjust=False`` (recursive form) with
    ``min_periods`` equal to the span, matching the convention for MACD::

        fast_ema   = EMA(close,  fast,  adjust=False, min_periods=fast)
        slow_ema   = EMA(close,  slow,  adjust=False, min_periods=slow)
        macd       = fast_ema - slow_ema
        signal_ln  = EMA(macd,   signal, adjust=False, min_periods=signal)
        histogram  = macd - signal_ln

    Internal EMAs are computed inside this function — no pre-existing EMA
    columns are required or used, and the input ``df`` is not mutated.

    NaN extent (assuming default 12 / 26 / 9):
    - macd: NaN until index ``slow - 1`` (= 25), valid thereafter.
    - signal: NaN until ``signal`` valid macd observations exist, i.e.
      first non-NaN at index ``slow - 1 + signal - 1`` (= 33).
    - hist: same NaN extent as signal.

    Columns appended::

        macd_{fast}_{slow}_{signal}
        macd_signal_{fast}_{slow}_{signal}
        macd_hist_{fast}_{slow}_{signal}

    Parameters
    ----------
    df : DataFrame
        Must contain a ``close`` column.
    fast, slow, signal : int
        EMA spans. All must be ``>= 1`` and ``fast < slow`` by convention
        (not enforced — passing ``fast >= slow`` produces an inverted MACD).

    Returns
    -------
    DataFrame
        Copy of ``df`` with three new MACD columns.
    """
    for name, value in (("fast", fast), ("slow", slow), ("signal", signal)):
        if value < 1:
            raise ValueError(f"{name} must be >= 1, got {value}")
    if "close" not in df.columns:
        raise ValueError("input DataFrame must contain a 'close' column")

    out = df.copy()
    close = df["close"]
    fast_ema = close.ewm(span=fast, adjust=False, min_periods=fast).mean()
    slow_ema = close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(
        span=signal, adjust=False, min_periods=signal
    ).mean()
    histogram = macd_line - signal_line

    suffix = f"{fast}_{slow}_{signal}"
    out[f"macd_{suffix}"] = macd_line
    out[f"macd_signal_{suffix}"] = signal_line
    out[f"macd_hist_{suffix}"] = histogram
    return out
