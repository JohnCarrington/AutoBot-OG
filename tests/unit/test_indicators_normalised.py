"""Unit tests for src.indicators.normalised."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from indicators.atr import add_atr
from indicators.bollinger import add_bollinger
from indicators.ema import add_ema
from indicators.normalised import (
    add_bb_width_normalised,
    add_ema_slope_normalised,
)


def _ohlc_constant(n: int, value: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [value] * n,
            "high": [value + 0.5] * n,
            "low": [value - 0.5] * n,
            "close": [value] * n,
        }
    )


def test_ema_slope_norm_requires_ema() -> None:
    df = _ohlc_constant(100)
    df = add_atr(df, period=14)  # ATR present, but no EMA column.
    with pytest.raises(ValueError, match="ema_50"):
        add_ema_slope_normalised(df, period=50, lookback=10, atr_period=14)


def test_ema_slope_norm_requires_atr() -> None:
    df = _ohlc_constant(100)
    df = add_ema(df, period=50)  # EMA present, but no ATR column.
    with pytest.raises(ValueError, match="atr_14"):
        add_ema_slope_normalised(df, period=50, lookback=10, atr_period=14)


def test_bb_width_norm_requires_bb_width() -> None:
    df = _ohlc_constant(100)
    df = add_atr(df, period=14)
    with pytest.raises(ValueError, match="bb_width_20_2"):
        add_bb_width_normalised(df, bb_period=20, bb_std=2.0, atr_period=14)


def test_bb_width_norm_requires_atr() -> None:
    df = _ohlc_constant(100)
    df = add_bollinger(df, period=20, std_mult=2.0)
    with pytest.raises(ValueError, match="atr_14"):
        add_bb_width_normalised(df, bb_period=20, bb_std=2.0, atr_period=14)


def test_ema_slope_norm_zero_slope() -> None:
    # Constant close -> EMA is constant once warmed -> slope is 0 -> norm is 0.
    df = _ohlc_constant(100)
    df = add_ema(df, period=50)
    df = add_atr(df, period=14)
    out = add_ema_slope_normalised(
        df, period=50, lookback=10, atr_period=14
    )
    col = out["ema_slope_norm_50_10"]

    # First values are NaN until both EMA and ATR are warm AND the lookback
    # window of EMA is filled. EMA warm at index 49, lookback=10 -> index 59.
    # ATR warm at index 14. Limiting factor: 59.
    assert col.iloc[:59].isna().all()
    # ATR is 1.0 for a constant H-L=1.0 series with constant close, so slope/atr
    # is exactly 0.0 (numerator is identically zero).
    np.testing.assert_allclose(col.iloc[59:].to_numpy(), 0.0, atol=0.0)


def test_bb_width_norm_known_values() -> None:
    # Build a DataFrame and inject known bb_width and atr columns directly,
    # bypassing the underlying indicator computations. This isolates the
    # ratio formula under test.
    n = 5
    df = pd.DataFrame(
        {
            "open": [1.0] * n,
            "high": [1.0] * n,
            "low": [1.0] * n,
            "close": [1.0] * n,
            "bb_width_20_2": [2.0, 4.0, 6.0, 8.0, 10.0],
            "atr_14": [1.0, 2.0, 2.0, 4.0, 5.0],
        }
    )
    out = add_bb_width_normalised(
        df, bb_period=20, bb_std=2.0, atr_period=14
    )
    np.testing.assert_allclose(
        out["bb_width_norm_20_2"].to_numpy(),
        [2.0, 2.0, 3.0, 2.0, 2.0],
        rtol=1e-12,
    )


def test_bb_width_norm_real_pipeline() -> None:
    # Sanity check that wiring through add_bollinger + add_atr produces a
    # non-negative, finite normalised width on a random walk.
    rng = np.random.default_rng(seed=3)
    closes = 100.0 + rng.standard_normal(200).cumsum()
    highs = closes + 0.5
    lows = closes - 0.5
    df = pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes}
    )
    df = add_bollinger(df, period=20, std_mult=2.0)
    df = add_atr(df, period=14)
    out = add_bb_width_normalised(df, bb_period=20, bb_std=2.0, atr_period=14)
    valid = out["bb_width_norm_20_2"].dropna()
    assert (valid >= 0).all()
    assert np.isfinite(valid).all()
    # Spot-check one row against the manual ratio.
    idx = valid.index[-1]
    expected = out["bb_width_20_2"].loc[idx] / out["atr_14"].loc[idx]
    assert math.isclose(out["bb_width_norm_20_2"].loc[idx], expected, rel_tol=1e-12)
