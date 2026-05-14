"""Unit tests for src.indicators.ema."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from indicators.ema import add_ema


def _ohlc_from_close(close: list[float]) -> pd.DataFrame:
    s = pd.Series(close, dtype=float)
    return pd.DataFrame({"open": s, "high": s, "low": s, "close": s})


def test_ema_constant_input() -> None:
    df = _ohlc_from_close([100.0] * 50)
    out = add_ema(df, period=50)
    col = out["ema_50"]
    # First (period - 1) rows are NaN.
    assert col.iloc[:49].isna().all()
    # From index 49 onward the EMA of a constant series equals the constant.
    assert np.allclose(col.iloc[49:].to_numpy(), 100.0)


def test_ema_linear_input() -> None:
    closes = list(range(1, 51))  # 1..50 inclusive
    df = _ohlc_from_close([float(x) for x in closes])
    period = 50
    out = add_ema(df, period=period)

    # Hand-rolled reference: y_0 = close_0; y_t = a*x_t + (1-a)*y_{t-1}.
    # First (period - 1) rows must be NaN.
    alpha = 2.0 / (period + 1)
    y = float(closes[0])
    for i in range(1, len(closes)):
        y = alpha * closes[i] + (1.0 - alpha) * y
    expected_last = y

    col = out["ema_50"]
    assert col.iloc[:49].isna().all()
    assert pytest.approx(expected_last, rel=1e-12) == float(col.iloc[49])


def test_ema_preserves_input() -> None:
    df = _ohlc_from_close([1.0, 2.0, 3.0, 4.0, 5.0])
    snapshot = df.copy(deep=True)
    _ = add_ema(df, period=3)
    pd.testing.assert_frame_equal(df, snapshot)
    assert "ema_3" not in df.columns


def test_ema_returns_new_df() -> None:
    df = _ohlc_from_close([1.0, 2.0, 3.0, 4.0, 5.0])
    out = add_ema(df, period=3)
    assert out is not df
    assert id(out) != id(df)


def test_ema_period_3_known_values() -> None:
    # period = 3 -> alpha = 2/4 = 0.5, an integer-friendly choice.
    # closes = 10, 11, ..., 19 (10 rows)
    # y_0 = 10 (masked NaN since min_periods=3)
    # y_1 = 0.5*11 + 0.5*10 = 10.5 (still NaN: only 2 obs so far)
    # y_2 = 0.5*12 + 0.5*10.5 = 11.25  <- first non-NaN
    # y_3 = 0.5*13 + 0.5*11.25 = 12.125
    # y_4 = 0.5*14 + 0.5*12.125 = 13.0625
    # y_5 = 0.5*15 + 0.5*13.0625 = 14.03125
    # y_6 = 0.5*16 + 0.5*14.03125 = 15.015625
    # y_7 = 0.5*17 + 0.5*15.015625 = 16.0078125
    # y_8 = 0.5*18 + 0.5*16.0078125 = 17.00390625
    # y_9 = 0.5*19 + 0.5*17.00390625 = 18.001953125
    df = _ohlc_from_close([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0])
    out = add_ema(df, period=3)
    col = out["ema_3"]

    assert col.iloc[0:2].isna().all()
    expected = [
        11.25,
        12.125,
        13.0625,
        14.03125,
        15.015625,
        16.0078125,
        17.00390625,
        18.001953125,
    ]
    np.testing.assert_allclose(col.iloc[2:].to_numpy(), expected, rtol=1e-12)
