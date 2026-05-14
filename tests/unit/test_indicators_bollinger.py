"""Unit tests for src.indicators.bollinger."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from indicators.bollinger import add_bollinger


def _ohlc_from_close(close: list[float]) -> pd.DataFrame:
    s = pd.Series(close, dtype=float)
    return pd.DataFrame({"open": s, "high": s, "low": s, "close": s})


def test_bb_constant_input() -> None:
    n = 30
    df = _ohlc_from_close([100.0] * n)
    out = add_bollinger(df, period=20, std_mult=2.0)
    # First 19 rows NaN; from index 19 onward stdev is 0 so all bands collapse.
    for col in ("bb_mid_20_2", "bb_upper_20_2", "bb_lower_20_2", "bb_width_20_2"):
        assert out[col].iloc[:19].isna().all(), f"{col} should be NaN before warmup"

    assert np.allclose(out["bb_mid_20_2"].iloc[19:].to_numpy(), 100.0)
    assert np.allclose(out["bb_upper_20_2"].iloc[19:].to_numpy(), 100.0)
    assert np.allclose(out["bb_lower_20_2"].iloc[19:].to_numpy(), 100.0)
    assert np.allclose(out["bb_width_20_2"].iloc[19:].to_numpy(), 0.0)


def test_bb_linear_input() -> None:
    # close = 1..30 inclusive (30 rows). period=20, std_mult=2.0.
    closes = [float(x) for x in range(1, 31)]
    df = _ohlc_from_close(closes)
    out = add_bollinger(df, period=20, std_mult=2.0)

    # At index 19, the window is closes[0..19] = 1..20.
    # SMA(1..20) = 10.5
    # Population variance = sum((x - 10.5)^2 for x in 1..20) / 20 = 33.25
    # Population std = sqrt(33.25)
    sigma = math.sqrt(33.25)
    assert math.isclose(out["bb_mid_20_2"].iloc[19], 10.5, rel_tol=1e-12)
    assert math.isclose(
        out["bb_upper_20_2"].iloc[19], 10.5 + 2.0 * sigma, rel_tol=1e-12
    )
    assert math.isclose(
        out["bb_lower_20_2"].iloc[19], 10.5 - 2.0 * sigma, rel_tol=1e-12
    )
    assert math.isclose(
        out["bb_width_20_2"].iloc[19], 4.0 * sigma, rel_tol=1e-12
    )

    # At index 29 the window is closes[10..29] = 11..30. By symmetry the
    # population std is the same; only the midpoint shifts to 20.5.
    assert math.isclose(out["bb_mid_20_2"].iloc[29], 20.5, rel_tol=1e-12)
    assert math.isclose(
        out["bb_width_20_2"].iloc[29], 4.0 * sigma, rel_tol=1e-12
    )


def test_bb_width_positive() -> None:
    rng = np.random.default_rng(seed=42)
    closes = (100.0 + rng.standard_normal(200).cumsum()).tolist()
    df = _ohlc_from_close(closes)
    out = add_bollinger(df, period=20, std_mult=2.0)
    width = out["bb_width_20_2"].dropna()
    assert (width >= 0).all()


def test_bb_upper_above_mid_above_lower() -> None:
    rng = np.random.default_rng(seed=7)
    closes = (100.0 + rng.standard_normal(200).cumsum()).tolist()
    df = _ohlc_from_close(closes)
    out = add_bollinger(df, period=20, std_mult=2.0)

    valid = out.dropna(
        subset=["bb_upper_20_2", "bb_mid_20_2", "bb_lower_20_2"]
    )
    # std must be > 0 for the strict ordering to hold; with random walks the
    # 20-window stdev is essentially never exactly zero, but allow >=.
    assert (valid["bb_upper_20_2"] >= valid["bb_mid_20_2"]).all()
    assert (valid["bb_mid_20_2"] >= valid["bb_lower_20_2"]).all()
