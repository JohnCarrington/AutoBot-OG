"""Unit tests for src.indicators.atr."""
from __future__ import annotations

import numpy as np
import pandas as pd

from indicators.atr import add_atr


def test_atr_constant_range() -> None:
    n = 30
    df = pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0] * n,
        }
    )
    out = add_atr(df, period=14)
    col = out["atr_14"]
    # First `period` rows (0..13) are NaN because TR[0] is NaN and Wilder
    # needs `period` valid TRs to seed.
    assert col.iloc[:14].isna().all()
    # From index 14 onward, every TR is exactly 2.0, so ATR = 2.0.
    assert np.allclose(col.iloc[14:].to_numpy(), 2.0)


def test_atr_zero_range() -> None:
    n = 20
    df = pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [100.0] * n,
            "low": [100.0] * n,
            "close": [100.0] * n,
        }
    )
    out = add_atr(df, period=14)
    col = out["atr_14"]
    assert col.iloc[:14].isna().all()
    assert np.allclose(col.iloc[14:].to_numpy(), 0.0)


def test_atr_period_14_known_values() -> None:
    # 20 bars, period=14. Engineer OHLC to give a known TR sequence and
    # then verify against an independent recursive reference.
    #
    # Setup: close = 100 everywhere (so prev_close = 100 for all t >= 1),
    # which makes |H - prev_close| and |L - prev_close| straightforward.
    # We vary H and L so that TR = H - L (the dominant term) is known.
    #
    # We choose TR values that probe both the SMA seed and the recursion:
    #   TR[0]       = NaN (no prior close)
    #   TR[1..13]   = 1.0
    #   TR[14]      = 2.0  (included in the SMA-seed window TR[1..14])
    #   TR[15]      = 3.0  (first purely recursive step)
    #   TR[16..19]  = 1.0
    n = 20
    high = [100.5] * n
    low = [99.5] * n  # H - L = 1.0
    high[14] = 101.0
    low[14] = 99.0  # H - L = 2.0
    high[15] = 101.5
    low[15] = 98.5  # H - L = 3.0
    close = [100.0] * n
    df = pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close}
    )

    out = add_atr(df, period=14)
    col = out["atr_14"]

    # First 14 rows NaN.
    assert col.iloc[:14].isna().all()

    # Independent reference computation.
    expected_tr = [np.nan] + [1.0] * 13 + [2.0, 3.0] + [1.0] * 4
    period = 14
    alpha = 1.0 / period
    seed = float(np.mean(expected_tr[1 : period + 1]))  # mean of TR[1..14]
    expected = [np.nan] * n
    expected[period] = seed
    for i in range(period + 1, n):
        expected[i] = expected[i - 1] * (1 - alpha) + expected_tr[i] * alpha

    np.testing.assert_allclose(
        col.iloc[14:].to_numpy(), expected[14:], rtol=1e-12
    )

    # Sanity check on the seed value itself.
    assert col.iloc[14] == seed
    assert seed == (13 * 1.0 + 2.0) / 14  # 15/14
