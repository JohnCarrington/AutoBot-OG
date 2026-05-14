"""End-to-end pipeline tests for src.indicators."""
from __future__ import annotations

import numpy as np
import pandas as pd

from indicators import (
    add_atr,
    add_bb_width_normalised,
    add_bollinger,
    add_ema,
    add_ema_slope_normalised,
    add_macd,
)


def _toy_ohlc(n: int = 200, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed=seed)
    closes = 1.30 + 0.001 * rng.standard_normal(n).cumsum()
    highs = closes + 0.0005
    lows = closes - 0.0005
    opens = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes}
    )


def _full_pipeline(df: pd.DataFrame) -> pd.DataFrame:
    out = add_ema(df, period=50)
    out = add_atr(out, period=14)
    out = add_bollinger(out, period=20, std_mult=2.0)
    out = add_macd(out, fast=12, slow=26, signal=9)
    out = add_ema_slope_normalised(out, period=50, lookback=10, atr_period=14)
    out = add_bb_width_normalised(
        out, bb_period=20, bb_std=2.0, atr_period=14
    )
    return out


def test_chain_all_indicators() -> None:
    df = _toy_ohlc(n=200)
    out = _full_pipeline(df)
    expected_added = {
        "ema_50",
        "atr_14",
        "bb_mid_20_2",
        "bb_upper_20_2",
        "bb_lower_20_2",
        "bb_width_20_2",
        "macd_12_26_9",
        "macd_signal_12_26_9",
        "macd_hist_12_26_9",
        "ema_slope_norm_50_10",
        "bb_width_norm_20_2",
    }
    assert expected_added.issubset(set(out.columns))
    # Original OHLC columns still present.
    assert {"open", "high", "low", "close"}.issubset(set(out.columns))


def test_original_columns_preserved() -> None:
    df = _toy_ohlc(n=200)
    before = df.copy(deep=True)
    out = _full_pipeline(df)

    # Input df itself must be byte-for-byte unchanged.
    pd.testing.assert_frame_equal(df, before)

    # And the OHLC columns inside the output match the input exactly.
    for col in ("open", "high", "low", "close"):
        pd.testing.assert_series_equal(
            out[col], df[col], check_names=False
        )


def test_no_silent_nan_fill() -> None:
    df = _toy_ohlc(n=200)
    out = _full_pipeline(df)

    # Each indicator has a deterministic warmup; the first valid row must
    # be at the documented index, and rows before it must be NaN.
    cases = [
        # (column, first valid index)
        ("ema_50", 49),
        ("atr_14", 14),
        ("bb_mid_20_2", 19),
        ("bb_upper_20_2", 19),
        ("bb_lower_20_2", 19),
        ("bb_width_20_2", 19),
        ("macd_12_26_9", 25),
        ("macd_signal_12_26_9", 33),
        ("macd_hist_12_26_9", 33),
        # ema slope norm: limiting factor = max(ema_warm, atr_warm) + lookback step.
        # ema_50 warm at 49; need ema[t - 10] too -> first valid at 49 + 10 = 59.
        # atr_14 warm at 14 (well before 59). So slope first valid at index 59.
        ("ema_slope_norm_50_10", 59),
        # bb_width_norm: max(bb_width warm=19, atr warm=14) -> 19.
        ("bb_width_norm_20_2", 19),
    ]
    for col, first_valid in cases:
        series = out[col]
        assert series.iloc[:first_valid].isna().all(), (
            f"{col}: expected NaN before index {first_valid}"
        )
        assert series.iloc[first_valid:].notna().all(), (
            f"{col}: unexpected NaN at or after index {first_valid}"
        )


def test_dataframe_copied() -> None:
    df = _toy_ohlc(n=50)
    out = add_ema(df, period=10)
    assert out is not df
    assert id(out) != id(df)
    # Mutating the output must not affect the input.
    out.iloc[0, out.columns.get_loc("close")] = -999.0
    assert df["close"].iloc[0] != -999.0
