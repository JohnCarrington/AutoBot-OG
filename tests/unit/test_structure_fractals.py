"""Unit tests for src.structure.fractals."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from structure.fractals import add_fractal_swings


def _ohlc(highs: list[float], lows: list[float]) -> pd.DataFrame:
    n = len(highs)
    assert len(lows) == n
    # Synthesise plausible open / close inside [low, high].
    closes = [(h + lo) / 2 for h, lo in zip(highs, lows)]
    return pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes}
    )


def test_no_swing_in_constant_data() -> None:
    n = 20
    df = _ohlc([100.0] * n, [99.0] * n)
    out = add_fractal_swings(df)
    assert not out["swing_high"].any()
    assert not out["swing_low"].any()
    # Sparse markers must be entirely NaN.
    assert out["swing_high_price"].isna().all()
    assert out["swing_low_price"].isna().all()
    # ffilled levels also NaN (never set).
    assert out["last_swing_high_price"].isna().all()
    assert out["last_swing_low_price"].isna().all()
    # Age columns must be entirely <NA> for nullable Int64.
    assert out["bars_since_swing_high"].isna().all()
    assert out["bars_since_swing_low"].isna().all()


def test_classic_swing_high() -> None:
    # One clear swing high at index 4. Highs rise to 15 at i=4 then fall.
    highs = [10.0, 11.0, 12.0, 13.0, 15.0, 13.0, 12.0, 11.0, 10.0, 9.0]
    lows = [h - 1.0 for h in highs]
    df = _ohlc(highs, lows)
    out = add_fractal_swings(df)
    expected = [False] * 10
    expected[4] = True
    assert out["swing_high"].tolist() == expected
    # Marker / level both reflect the swing.
    assert out["swing_high_price"].iloc[4] == 15.0
    assert out["last_swing_high_price"].iloc[4] == 15.0
    assert out["last_swing_high_price"].iloc[9] == 15.0


def test_classic_swing_low() -> None:
    # Mirror: one clear swing low at index 5.
    lows = [10.0, 9.0, 8.0, 7.0, 6.0, 4.0, 6.0, 7.0, 8.0, 9.0]
    highs = [lo + 1.0 for lo in lows]
    df = _ohlc(highs, lows)
    out = add_fractal_swings(df)
    expected = [False] * 10
    expected[5] = True
    assert out["swing_low"].tolist() == expected
    assert out["swing_low_price"].iloc[5] == 4.0
    assert out["last_swing_low_price"].iloc[5] == 4.0
    assert out["last_swing_low_price"].iloc[9] == 4.0


def test_edge_rows_never_swing() -> None:
    # Construct data where index 0, 1, N-2, N-1 are local extrema. Even
    # though they look like spikes, they cannot be 5-bar fractals because
    # they lack neighbours on one side.
    n = 12
    highs = [1.0] * n
    lows = [0.0] * n
    highs[0] = 100.0  # would-be swing high with no left neighbours
    highs[1] = 99.0
    highs[-2] = 99.0
    highs[-1] = 100.0  # would-be swing high with no right neighbours
    lows[0] = -100.0
    lows[1] = -99.0
    lows[-2] = -99.0
    lows[-1] = -100.0
    df = _ohlc(highs, lows)
    out = add_fractal_swings(df)
    for idx in (0, 1, n - 2, n - 1):
        assert not bool(out["swing_high"].iloc[idx])
        assert not bool(out["swing_low"].iloc[idx])


def test_tied_highs_no_swing() -> None:
    # Centre bar's high equals its immediate neighbours -> strict > fails.
    highs = [10.0, 11.0, 12.0, 12.0, 12.0, 11.0, 10.0, 9.0, 8.0, 7.0]
    lows = [h - 1.0 for h in highs]
    df = _ohlc(highs, lows)
    out = add_fractal_swings(df)
    assert not out["swing_high"].any()


def test_last_swing_price_ffilled() -> None:
    # Swing high at index 5 (highs peak at 20 there). After bar 5 the
    # ffilled level must hold at 20 until a later swing replaces it.
    highs = [10.0, 11.0, 12.0, 13.0, 14.0, 20.0, 14.0, 13.0, 12.0, 11.0,
             10.0, 9.0, 8.0, 7.0, 6.0]
    lows = [h - 1.0 for h in highs]
    df = _ohlc(highs, lows)
    out = add_fractal_swings(df)
    assert bool(out["swing_high"].iloc[5])
    # Before the swing, last_swing_high_price is NaN.
    assert out["last_swing_high_price"].iloc[:5].isna().all()
    # From bar 5 onward, holds at 20 (no later swing high in this series).
    assert (out["last_swing_high_price"].iloc[5:] == 20.0).all()


def test_bars_since_swing_counter() -> None:
    # Same series as above: swing high at index 5, no later swings.
    # bars_since_swing_high should be <NA> at 0..4, then 0, 1, 2, ...
    highs = [10.0, 11.0, 12.0, 13.0, 14.0, 20.0, 14.0, 13.0, 12.0, 11.0,
             10.0, 9.0, 8.0, 7.0, 6.0]
    lows = [h - 1.0 for h in highs]
    df = _ohlc(highs, lows)
    out = add_fractal_swings(df)
    age = out["bars_since_swing_high"]
    assert age.iloc[:5].isna().all()
    # After the swing: 0 at bar 5, 1 at bar 6, ..., 9 at bar 14.
    expected_tail = list(range(0, len(highs) - 5))
    assert age.iloc[5:].tolist() == expected_tail
    # Dtype is nullable Int64.
    assert str(age.dtype) == "Int64"


def test_input_preserved() -> None:
    highs = [10.0, 11.0, 12.0, 13.0, 14.0, 20.0, 14.0, 13.0, 12.0, 11.0]
    lows = [h - 1.0 for h in highs]
    df = _ohlc(highs, lows)
    snapshot = df.copy(deep=True)
    _ = add_fractal_swings(df)
    pd.testing.assert_frame_equal(df, snapshot)
    # No new columns leaked into the input.
    assert "swing_high" not in df.columns


def test_returns_new_df() -> None:
    df = _ohlc([1.0] * 6, [0.0] * 6)
    out = add_fractal_swings(df)
    assert out is not df
    assert id(out) != id(df)


def test_missing_high_column_raises() -> None:
    df = pd.DataFrame({"low": [1.0, 2.0, 3.0], "close": [1.5, 2.5, 3.5]})
    with pytest.raises(ValueError, match="'high'"):
        add_fractal_swings(df)


def test_missing_low_column_raises() -> None:
    df = pd.DataFrame({"high": [1.0, 2.0, 3.0], "close": [1.5, 2.5, 3.5]})
    with pytest.raises(ValueError, match="'low'"):
        add_fractal_swings(df)
