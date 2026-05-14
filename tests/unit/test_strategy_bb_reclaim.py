"""Tests for the Bollinger Reclaim strategy.

Synthetic M5/H1 candles are constructed bar-by-bar with hand-picked
indicator values. Indicator computation is *not* invoked — these tests
pin pattern detection, gating, SL/TP, and confidence; integration with
the indicator pipeline is covered separately.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from regime.labels import Direction, RegimeLabel
from strategies.bb_reclaim import detect_bb_reclaim


_PAIR = "GBPUSD"
_NOW = datetime(2025, 5, 14, 12, 0, tzinfo=timezone.utc)


def _m5_long_clean() -> pd.DataFrame:
    """Three M5 bars that form a clean LONG BB reclaim around 1.30."""
    idx = [
        _NOW - timedelta(minutes=10),
        _NOW - timedelta(minutes=5),
        _NOW,
    ]
    rows = [
        # Pierce: closes BELOW bb_lower (1.2980)
        {
            "open": 1.30000, "high": 1.30010, "low": 1.29700, "close": 1.29750,
            "bb_lower_20_2": 1.29800, "bb_mid_20_2": 1.30000,
            "bb_upper_20_2": 1.30200, "atr_14": 0.0020,
        },
        # Rejection: closes BACK INSIDE the bands
        {
            "open": 1.29750, "high": 1.29980, "low": 1.29720, "close": 1.29950,
            "bb_lower_20_2": 1.29800, "bb_mid_20_2": 1.30000,
            "bb_upper_20_2": 1.30200, "atr_14": 0.0020,
        },
        # Confirmation: close > rejection close AND > bb_lower
        {
            "open": 1.29950, "high": 1.30050, "low": 1.29940, "close": 1.30020,
            "bb_lower_20_2": 1.29800, "bb_mid_20_2": 1.30000,
            "bb_upper_20_2": 1.30200, "atr_14": 0.0020,
        },
    ]
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def _m5_short_clean() -> pd.DataFrame:
    idx = [
        _NOW - timedelta(minutes=10),
        _NOW - timedelta(minutes=5),
        _NOW,
    ]
    rows = [
        # Pierce above bb_upper
        {
            "open": 1.30000, "high": 1.30300, "low": 1.29980, "close": 1.30250,
            "bb_lower_20_2": 1.29800, "bb_mid_20_2": 1.30000,
            "bb_upper_20_2": 1.30200, "atr_14": 0.0020,
        },
        # Rejection inside
        {
            "open": 1.30250, "high": 1.30280, "low": 1.30020, "close": 1.30050,
            "bb_lower_20_2": 1.29800, "bb_mid_20_2": 1.30000,
            "bb_upper_20_2": 1.30200, "atr_14": 0.0020,
        },
        # Confirmation: close < rejection AND < bb_upper
        {
            "open": 1.30050, "high": 1.30060, "low": 1.29950, "close": 1.29980,
            "bb_lower_20_2": 1.29800, "bb_mid_20_2": 1.30000,
            "bb_upper_20_2": 1.30200, "atr_14": 0.0020,
        },
    ]
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def _h1(
    *, bb_width: float = 1.5, slope: float = 0.05, macd_hist: float = 0.10
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "bb_width_norm_20_2": bb_width,
                "ema_slope_norm_50_10": slope,
                "macd_hist_12_26_9": macd_hist,
            }
        ]
    )


def _state(regime: str = "RANGE", direction: str | None = None) -> dict:
    return {
        "current_regime": regime,
        "current_direction": direction,
        "pending_regime": None,
        "m5_confirmation_count": 0,
        "last_regime_change_time": None,
        "reason": "test",
        "debug": {},
    }


# --- Happy path -------------------------------------------------------------


def test_long_clean_setup_returns_signal() -> None:
    sig = detect_bb_reclaim(
        df_m5=_m5_long_clean(),
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is not None
    assert sig.direction is Direction.BULLISH
    assert sig.regime is RegimeLabel.RANGE
    assert sig.strategy_name == "bb_reclaim"
    assert sig.suggested_entry_price == pytest.approx(1.30020)
    assert sig.suggested_tp_price == pytest.approx(1.30000)  # bb_mid at pierce


def test_short_clean_setup_returns_signal() -> None:
    sig = detect_bb_reclaim(
        df_m5=_m5_short_clean(),
        df_h1=_h1(macd_hist=-0.10),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is not None
    assert sig.direction is Direction.BEARISH
    assert sig.confidence_score == pytest.approx(0.85)  # MACD aligned bearish


# --- Regime gate ------------------------------------------------------------


def test_wrong_regime_returns_none() -> None:
    sig = detect_bb_reclaim(
        df_m5=_m5_long_clean(),
        df_h1=_h1(),
        regime_state=_state(regime="TREND"),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_bb_width_too_wide_blocks_setup() -> None:
    sig = detect_bb_reclaim(
        df_m5=_m5_long_clean(),
        df_h1=_h1(bb_width=2.0),  # ≥ 1.8 threshold
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_slope_too_steep_blocks_setup() -> None:
    sig = detect_bb_reclaim(
        df_m5=_m5_long_clean(),
        df_h1=_h1(slope=0.25),  # > 0.15
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


# --- Pattern misses ---------------------------------------------------------


def test_pierce_missing_returns_none() -> None:
    df = _m5_long_clean()
    # Pierce close inside the band — no setup.
    df.iloc[0, df.columns.get_loc("close")] = 1.29900
    sig = detect_bb_reclaim(
        df_m5=df,
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_rejection_still_outside_returns_none() -> None:
    df = _m5_long_clean()
    # Rejection bar still closes below bb_lower.
    df.iloc[1, df.columns.get_loc("close")] = 1.29750
    sig = detect_bb_reclaim(
        df_m5=df,
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_confirmation_below_rejection_returns_none() -> None:
    df = _m5_long_clean()
    # Confirmation closes BELOW rejection — not a continuation.
    df.iloc[2, df.columns.get_loc("close")] = 1.29900
    sig = detect_bb_reclaim(
        df_m5=df,
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_insufficient_m5_data_returns_none() -> None:
    df = _m5_long_clean().iloc[-2:]  # only 2 bars
    sig = detect_bb_reclaim(
        df_m5=df,
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


# --- Confidence -------------------------------------------------------------


def test_macd_aligned_yields_high_confidence_long() -> None:
    sig = detect_bb_reclaim(
        df_m5=_m5_long_clean(),
        df_h1=_h1(macd_hist=0.10),  # positive aligns with BULLISH
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is not None and sig.confidence_score == pytest.approx(0.85)


def test_macd_opposite_yields_low_confidence_long() -> None:
    sig = detect_bb_reclaim(
        df_m5=_m5_long_clean(),
        df_h1=_h1(macd_hist=-0.10),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is not None and sig.confidence_score == pytest.approx(0.65)


def test_macd_zero_yields_low_confidence() -> None:
    sig = detect_bb_reclaim(
        df_m5=_m5_long_clean(),
        df_h1=_h1(macd_hist=0.0),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is not None and sig.confidence_score == pytest.approx(0.65)


# --- SL anchoring & floor ---------------------------------------------------


def test_sl_anchors_to_pierce_wick_long() -> None:
    sig = detect_bb_reclaim(
        df_m5=_m5_long_clean(),
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    # MIN_SL_PIPS[GBPUSD]=15 → 0.0015. ATR_M5=0.0020 → 20 pips × 0.8 = 16 pips.
    # max(15, 16) = 16 pips = 0.0016. Anchor=pierce.low=1.29700.
    assert sig is not None
    assert sig.suggested_sl_price == pytest.approx(1.29700 - 0.0016)


def test_sl_respects_min_pip_floor_when_atr_small() -> None:
    df = _m5_long_clean()
    # Shrink ATR so 0.8 × ATR = 8 pips < 15 pip floor.
    df["atr_14"] = 0.0010  # 10 pips
    sig = detect_bb_reclaim(
        df_m5=df,
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is not None
    # Floor wins: SL = pierce.low − 15 pips.
    assert sig.suggested_sl_price == pytest.approx(1.29700 - 0.0015)


def test_sl_anchors_to_pierce_wick_short() -> None:
    sig = detect_bb_reclaim(
        df_m5=_m5_short_clean(),
        df_h1=_h1(macd_hist=-0.10),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    # ATR 0.0020 → 20 pips × 0.8 = 16 pips. Anchor = pierce.high = 1.30300.
    assert sig is not None
    assert sig.suggested_sl_price == pytest.approx(1.30300 + 0.0016)


# --- Signal metadata --------------------------------------------------------


def test_signal_carries_source_and_invalid_after() -> None:
    sig = detect_bb_reclaim(
        df_m5=_m5_long_clean(),
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is not None
    assert sig.source_candle_ts == _NOW
    assert sig.invalid_after_candle_ts == _NOW + timedelta(minutes=5)


def test_nan_atr_returns_none() -> None:
    df = _m5_long_clean()
    df.iloc[2, df.columns.get_loc("atr_14")] = float("nan")
    sig = detect_bb_reclaim(
        df_m5=df,
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None
