"""5-bar fractal swing-high / swing-low detection."""
from __future__ import annotations

import numpy as np
import pandas as pd


def add_fractal_swings(df: pd.DataFrame) -> pd.DataFrame:
    """Append 5-bar fractal swing columns to a copy of ``df``.

    Definitions
    -----------
    A 5-bar fractal **swing high** at index ``i`` requires::

        high[i] > high[i - 2]
        high[i] > high[i - 1]
        high[i] > high[i + 1]
        high[i] > high[i + 2]

    Comparisons are *strict*: ties with any neighbour disqualify the bar.
    A swing **low** uses the mirror condition on ``low``. Because the test
    references ``i + 1`` and ``i + 2``, a swing at index ``i`` cannot be
    *known* until bar ``i + 2`` closes — there is an inherent **2-bar
    confirmation lag**. The columns produced here, however, are *static
    labels*: they reflect the full DataFrame's state and contain no lag.
    Callers running in a real-time setting must apply the 2-bar lag
    themselves (e.g. only consume ``swing_high[i]`` once bar ``i + 2`` has
    closed) to avoid lookahead bias.

    Columns appended
    ----------------
    swing_high : bool
        ``True`` at the bar that *is* the swing high. ``False`` elsewhere,
        including the first two and last two rows (which can never be
        evaluated against four neighbours).
    swing_low : bool
        Mirror of ``swing_high`` on the ``low`` series.
    swing_high_price : float
        ``high[i]`` where ``swing_high[i]`` is True, else NaN. A sparse
        marker column — **not** forward-filled.
    swing_low_price : float
        Mirror of ``swing_high_price`` on ``low``.
    last_swing_high_price : float
        Forward-fill of ``swing_high_price``. NaN until the first swing
        high; thereafter holds the most recent swing high's price.
    last_swing_low_price : float
        Mirror of ``last_swing_high_price``.
    bars_since_swing_high : Int64 (nullable)
        ``current_position - most_recent_swing_high_position``. ``0`` at
        the swing bar itself, then increments by one per bar. ``<NA>``
        before the first swing high.
    bars_since_swing_low : Int64 (nullable)
        Mirror of ``bars_since_swing_high``.

    Parameters
    ----------
    df : DataFrame
        Must contain ``high`` and ``low`` columns.

    Returns
    -------
    DataFrame
        Copy of ``df`` with eight new columns.

    Raises
    ------
    ValueError
        If ``high`` or ``low`` is missing from ``df``.
    """
    if "high" not in df.columns:
        raise ValueError("input DataFrame must contain a 'high' column")
    if "low" not in df.columns:
        raise ValueError("input DataFrame must contain a 'low' column")

    out = df.copy()
    high = df["high"]
    low = df["low"]

    # Strict 5-bar fractal: shift(-k) values are NaN at the tail and shift(+k)
    # values are NaN at the head; NaN comparisons evaluate to False, so the
    # first two and last two rows are automatically False.
    swing_high_raw = (
        (high > high.shift(2))
        & (high > high.shift(1))
        & (high > high.shift(-1))
        & (high > high.shift(-2))
    )
    swing_low_raw = (
        (low < low.shift(2))
        & (low < low.shift(1))
        & (low < low.shift(-1))
        & (low < low.shift(-2))
    )
    swing_high = swing_high_raw.fillna(False).astype(bool)
    swing_low = swing_low_raw.fillna(False).astype(bool)

    out["swing_high"] = swing_high
    out["swing_low"] = swing_low

    # Sparse price markers (NaN where not a swing).
    out["swing_high_price"] = high.where(swing_high)
    out["swing_low_price"] = low.where(swing_low)

    # Forward-filled levels (most recent swing visible at each bar).
    out["last_swing_high_price"] = out["swing_high_price"].ffill()
    out["last_swing_low_price"] = out["swing_low_price"].ffill()

    # Positional age: current row index minus row index of most recent swing.
    n = len(df)
    pos = pd.Series(np.arange(n, dtype=float), index=df.index)
    last_high_pos = pos.where(swing_high).ffill()
    last_low_pos = pos.where(swing_low).ffill()
    out["bars_since_swing_high"] = (pos - last_high_pos).astype("Int64")
    out["bars_since_swing_low"] = (pos - last_low_pos).astype("Int64")

    return out
