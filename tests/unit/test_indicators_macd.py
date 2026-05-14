"""Unit tests for src.indicators.macd."""
from __future__ import annotations

import numpy as np
import pandas as pd

from indicators.macd import add_macd


def _ohlc_from_close(close: list[float]) -> pd.DataFrame:
    s = pd.Series(close, dtype=float)
    return pd.DataFrame({"open": s, "high": s, "low": s, "close": s})


def _ema_recursive(values: list[float], span: int) -> list[float]:
    """Reference EMA matching pandas ewm(adjust=False, min_periods=span).

    The recursion is seeded by the first non-NaN input. The output is NaN
    until ``span`` non-NaN observations have accumulated. NaN inputs produce
    NaN outputs and leave the state unchanged (suitable for the leading
    NaNs in the MACD line; this test does not exercise interspersed NaNs).
    """
    alpha = 2.0 / (span + 1)
    out: list[float] = []
    prev: float | None = None
    non_nan_count = 0
    for v in values:
        if np.isnan(v):
            out.append(float("nan"))
            continue
        non_nan_count += 1
        if prev is None:
            prev = float(v)
        else:
            prev = alpha * float(v) + (1.0 - alpha) * prev
        out.append(prev if non_nan_count >= span else float("nan"))
    return out


def test_macd_constant_input() -> None:
    n = 50
    df = _ohlc_from_close([100.0] * n)
    out = add_macd(df, fast=12, slow=26, signal=9)

    macd = out["macd_12_26_9"]
    signal = out["macd_signal_12_26_9"]
    hist = out["macd_hist_12_26_9"]

    # macd: NaN until index slow - 1 = 25.
    assert macd.iloc[:25].isna().all()
    assert np.allclose(macd.iloc[25:].to_numpy(), 0.0)

    # signal: NaN until 9 valid macd observations exist -> index 25 + 8 = 33.
    assert signal.iloc[:33].isna().all()
    assert np.allclose(signal.iloc[33:].to_numpy(), 0.0)

    # hist follows signal's NaN extent and is zero thereafter.
    assert hist.iloc[:33].isna().all()
    assert np.allclose(hist.iloc[33:].to_numpy(), 0.0)


def test_macd_known_values() -> None:
    # Small spans so we can compute MACD with an independent reference.
    fast, slow, signal = 3, 6, 3
    closes = [10.0, 11.0, 9.0, 12.0, 13.0, 11.0, 14.0, 15.0, 12.0, 16.0,
              17.0, 15.0, 18.0, 19.0, 17.0, 20.0, 21.0, 19.0, 22.0, 23.0]
    df = _ohlc_from_close(closes)
    out = add_macd(df, fast=fast, slow=slow, signal=signal)

    fast_ref = _ema_recursive(closes, fast)
    slow_ref = _ema_recursive(closes, slow)
    macd_ref = [
        f - s if not (np.isnan(f) or np.isnan(s)) else float("nan")
        for f, s in zip(fast_ref, slow_ref)
    ]
    signal_ref = _ema_recursive(macd_ref, signal)
    hist_ref = [
        m - s if not (np.isnan(m) or np.isnan(s)) else float("nan")
        for m, s in zip(macd_ref, signal_ref)
    ]

    np.testing.assert_allclose(
        out[f"macd_{fast}_{slow}_{signal}"].to_numpy(),
        np.array(macd_ref, dtype=float),
        rtol=1e-12,
        equal_nan=True,
    )
    np.testing.assert_allclose(
        out[f"macd_signal_{fast}_{slow}_{signal}"].to_numpy(),
        np.array(signal_ref, dtype=float),
        rtol=1e-12,
        equal_nan=True,
    )
    np.testing.assert_allclose(
        out[f"macd_hist_{fast}_{slow}_{signal}"].to_numpy(),
        np.array(hist_ref, dtype=float),
        rtol=1e-12,
        equal_nan=True,
    )
