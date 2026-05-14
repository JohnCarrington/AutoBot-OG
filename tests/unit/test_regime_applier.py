"""Unit tests for src.regime.applier."""
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
from regime.applier import apply_regime_to_candles
from regime.engine import RegimeEngine
from structure import add_fractal_swings


# --- Helpers ----------------------------------------------------------------


def _enrich_h1(df: pd.DataFrame) -> pd.DataFrame:
    df = add_ema(df, period=50)
    df = add_atr(df, period=14)
    df = add_bollinger(df, period=20, std_mult=2.0)
    df = add_macd(df, fast=12, slow=26, signal=9)
    df = add_ema_slope_normalised(df, period=50, lookback=10, atr_period=14)
    df = add_bb_width_normalised(df, bb_period=20, bb_std=2.0, atr_period=14)
    df = add_fractal_swings(df)
    return df


def _enrich_m5(df: pd.DataFrame) -> pd.DataFrame:
    df = add_ema(df, period=50)
    df = add_atr(df, period=14)
    df = add_bollinger(df, period=20, std_mult=2.0)
    df = add_ema_slope_normalised(df, period=50, lookback=10, atr_period=14)
    df = add_bb_width_normalised(df, bb_period=20, bb_std=2.0, atr_period=14)
    return df


def _build_trending_h1(n: int) -> pd.DataFrame:
    """Synthetic uptrend H1 candles with monotonically rising closes."""
    close = np.linspace(1.30, 1.40, n)
    high = close + 0.0010
    low = close - 0.0010
    index = pd.date_range(
        start="2025-01-01 00:00", periods=n, freq="1h"
    )
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close},
        index=index,
    )


def _build_trending_m5(n: int) -> pd.DataFrame:
    close = np.linspace(1.30, 1.40, n)
    high = close + 0.0002
    low = close - 0.0002
    index = pd.date_range(
        start="2025-01-01 00:00", periods=n, freq="5min"
    )
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close},
        index=index,
    )


# --- Tests ------------------------------------------------------------------


def test_apply_to_minimal_data_produces_columns() -> None:
    h1 = _enrich_h1(_build_trending_h1(120))
    m5 = _enrich_m5(_build_trending_m5(720))
    engine = RegimeEngine()
    out = apply_regime_to_candles(h1, m5, engine)
    expected = {
        "regime",
        "regime_direction",
        "regime_confidence",
        "regime_reason",
        "regime_live",
    }
    assert expected.issubset(set(out.columns))
    # Original OHLC columns survive untouched.
    for col in ("open", "high", "low", "close"):
        pd.testing.assert_series_equal(
            out[col], m5[col], check_names=False
        )


def test_apply_regime_column_dtypes() -> None:
    h1 = _enrich_h1(_build_trending_h1(120))
    m5 = _enrich_m5(_build_trending_m5(720))
    out = apply_regime_to_candles(h1, m5, RegimeEngine())
    assert str(out["regime"].dtype) == "string"
    assert str(out["regime_direction"].dtype) == "string"
    assert out["regime_live"].dtype == bool


def test_apply_emits_trend_for_strong_uptrend() -> None:
    h1 = _enrich_h1(_build_trending_h1(120))
    m5 = _enrich_m5(_build_trending_m5(720))
    out = apply_regime_to_candles(h1, m5, RegimeEngine())
    # By the tail of the dataset, the engine should have observed enough
    # rising bars to commit TREND bullish. We check the final state.
    tail = out.iloc[-1]
    assert tail["regime"] in {"TREND", "VOLATILE"}
    if tail["regime"] == "TREND":
        assert tail["regime_direction"] == "BULLISH"
        assert tail["regime_live"]


def test_apply_pre_h1_bars_are_initial() -> None:
    # M5 timestamps that fall before the first H1 close get the initial
    # state. Use an H1 index starting after the M5 series.
    m5 = _enrich_m5(_build_trending_m5(60))
    h1 = _build_trending_h1(24)
    h1.index = pd.date_range(
        start=m5.index[-1] + pd.Timedelta(hours=1), periods=24, freq="1h"
    )
    h1 = _enrich_h1(h1)
    out = apply_regime_to_candles(h1, m5, RegimeEngine())
    # Every M5 row precedes every H1 event -> all initial.
    assert (out["regime"] == "TRANSITION").all()
    assert (out["regime_reason"] == "initial").all()
    assert not out["regime_live"].any()


def test_apply_input_not_mutated() -> None:
    h1 = _enrich_h1(_build_trending_h1(60))
    m5 = _enrich_m5(_build_trending_m5(360))
    h1_snapshot = h1.copy(deep=True)
    m5_snapshot = m5.copy(deep=True)
    _ = apply_regime_to_candles(h1, m5, RegimeEngine())
    pd.testing.assert_frame_equal(h1, h1_snapshot)
    pd.testing.assert_frame_equal(m5, m5_snapshot)


def test_apply_empty_m5_returns_empty_with_columns() -> None:
    h1 = _enrich_h1(_build_trending_h1(60))
    m5 = pd.DataFrame(
        {"open": [], "high": [], "low": [], "close": []},
        index=pd.DatetimeIndex([], name="ts"),
    )
    out = apply_regime_to_candles(h1, m5, RegimeEngine())
    assert len(out) == 0
    for col in (
        "regime",
        "regime_direction",
        "regime_confidence",
        "regime_reason",
        "regime_live",
    ):
        assert col in out.columns


def test_apply_empty_h1_emits_initial_for_all_m5_bars() -> None:
    m5 = _enrich_m5(_build_trending_m5(20))
    h1 = pd.DataFrame(
        {"open": [], "high": [], "low": [], "close": []},
        index=pd.DatetimeIndex([], name="ts"),
    )
    out = apply_regime_to_candles(h1, m5, RegimeEngine())
    assert (out["regime"] == "TRANSITION").all()


def test_apply_cold_start_replay_idempotent() -> None:
    # Running through twice with fresh engines must produce identical results.
    h1 = _enrich_h1(_build_trending_h1(60))
    m5 = _enrich_m5(_build_trending_m5(360))
    a = apply_regime_to_candles(h1, m5, RegimeEngine())
    b = apply_regime_to_candles(h1, m5, RegimeEngine())
    pd.testing.assert_frame_equal(a, b)


def test_apply_rejects_unsupported_h1_anchor() -> None:
    """H4: open-anchored timestamps are rejected loudly.

    v1 only supports bar-close anchoring; the caller must say so
    explicitly via the keyword-only ``h1_anchor`` parameter.
    """
    import pytest

    h1 = _enrich_h1(_build_trending_h1(60))
    m5 = _enrich_m5(_build_trending_m5(60))
    with pytest.raises(NotImplementedError, match="h1_anchor"):
        # Type-checking would catch this at static-analysis time; the
        # runtime check is the last-mile safety net.
        apply_regime_to_candles(h1, m5, RegimeEngine(), h1_anchor="open")  # type: ignore[arg-type]


def test_apply_regime_can_change_within_dataset() -> None:
    # A series that flattens then trends — we expect the regime column to
    # contain more than one distinct label across the whole frame.
    n_h1 = 200
    closes = np.concatenate(
        [
            np.full(80, 1.30),  # flat
            np.linspace(1.30, 1.40, n_h1 - 80),  # steady uptrend
        ]
    )
    rng = np.random.default_rng(seed=2)
    closes = closes + 0.0002 * rng.standard_normal(n_h1)
    highs = closes + 0.0010
    lows = closes - 0.0010
    h1 = pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes},
        index=pd.date_range("2025-01-01", periods=n_h1, freq="1h"),
    )
    h1 = _enrich_h1(h1)

    n_m5 = n_h1 * 12
    m5_closes = np.interp(
        np.arange(n_m5),
        np.arange(0, n_m5, 12),
        closes,
    )
    m5 = pd.DataFrame(
        {
            "open": m5_closes,
            "high": m5_closes + 0.0002,
            "low": m5_closes - 0.0002,
            "close": m5_closes,
        },
        index=pd.date_range("2025-01-01", periods=n_m5, freq="5min"),
    )
    m5 = _enrich_m5(m5)
    out = apply_regime_to_candles(h1, m5, RegimeEngine())
    distinct = set(out["regime"].dropna().unique().tolist())
    # The frame must transit at least two regime states across its life.
    assert len(distinct) >= 2
