"""Tests for the EMA Continuation strategy.

Structure swings are populated directly on the test DataFrames (no
indicator pipeline invoked); this isolates strategy logic from the
fractal detector.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from regime.labels import Direction, RegimeLabel
from strategies.ema_continuation import detect_ema_continuation


_PAIR = "GBPUSD"
_NOW = datetime(2025, 5, 14, 12, 0, tzinfo=timezone.utc)

# EMA50 price level used throughout — pattern bars are placed relative to it.
_EMA = 1.30000


def _index(n_bars: int) -> pd.DatetimeIndex:
    base = _NOW - timedelta(minutes=5 * (n_bars - 1))
    return pd.DatetimeIndex([base + timedelta(minutes=5 * i) for i in range(n_bars)])


def _m5_bullish_clean() -> pd.DataFrame:
    """15 M5 bars; final 3 bars form a clean LONG EMA continuation.

    Structure is hand-populated so ``get_structure_state(df, lookback_bars=10)``
    returns ``recent_pattern == "HL"`` (the most-recent swing low is higher
    than the previous one — uptrend).
    """
    n = 15
    rows = []
    # Default values for all bars; will overwrite key ones.
    for i in range(n):
        rows.append(
            {
                "open": _EMA + 0.0010,
                "high": _EMA + 0.0020,
                "low": _EMA + 0.0005,
                "close": _EMA + 0.0010,
                "ema_50": _EMA,
                "atr_14": 0.0020,
                "swing_high": False,
                "swing_low": False,
                "swing_high_price": float("nan"),
                "swing_low_price": float("nan"),
            }
        )

    # Structure swings inside the last-10-bars window (indices 5..14):
    # swing_high at idx 6 (high=1.30200), idx 10 (high=1.30300) → HH
    # swing_low  at idx 8 (low =1.29950), idx 11 (low =1.30050) → HL
    # Most recent event = swing_low at idx 11 → recent_pattern = "HL".
    rows[6]["swing_high"] = True
    rows[6]["high"] = 1.30200
    rows[6]["swing_high_price"] = 1.30200
    rows[10]["swing_high"] = True
    rows[10]["high"] = 1.30300
    rows[10]["swing_high_price"] = 1.30300
    rows[8]["swing_low"] = True
    rows[8]["low"] = 1.29950
    rows[8]["swing_low_price"] = 1.29950
    rows[11]["swing_low"] = True
    rows[11]["low"] = 1.30050
    rows[11]["swing_low_price"] = 1.30050

    # Last three bars: pullback (-3), reclaim (-2), confirmation (-1).
    # Pullback wick-penetrates EMA, closes above (within tolerance).
    rows[12].update(
        {
            "open": 1.30050,
            "high": 1.30070,
            "low": _EMA - 0.0010,  # wick crosses below EMA
            "close": _EMA + 0.0001,  # close above EMA
        }
    )
    # Reclaim closes firmly above EMA.
    rows[13].update(
        {"open": _EMA + 0.0001, "high": 1.30060, "low": _EMA - 0.0002, "close": 1.30040}
    )
    # Confirmation: bullish-bodied, close > reclaim close.
    rows[14].update(
        {"open": 1.30040, "high": 1.30100, "low": 1.30035, "close": 1.30080}
    )

    return pd.DataFrame(rows, index=_index(n))


def _m5_bearish_clean() -> pd.DataFrame:
    """Mirror of bullish_clean: recent_pattern = "LH" (downtrend)."""
    n = 15
    rows = []
    for i in range(n):
        rows.append(
            {
                "open": _EMA - 0.0010,
                "high": _EMA - 0.0005,
                "low": _EMA - 0.0020,
                "close": _EMA - 0.0010,
                "ema_50": _EMA,
                "atr_14": 0.0020,
                "swing_high": False,
                "swing_low": False,
                "swing_high_price": float("nan"),
                "swing_low_price": float("nan"),
            }
        )

    # LH structure: swing_high at idx 8 (1.30050), idx 11 (1.29950) → LH
    # swing_low at idx 6 (1.29800), idx 10 (1.29700) → LL
    # Most recent event = swing_high at idx 11 → recent_pattern = "LH".
    rows[8]["swing_high"] = True
    rows[8]["high"] = 1.30050
    rows[8]["swing_high_price"] = 1.30050
    rows[11]["swing_high"] = True
    rows[11]["high"] = 1.29950
    rows[11]["swing_high_price"] = 1.29950
    rows[6]["swing_low"] = True
    rows[6]["low"] = 1.29800
    rows[6]["swing_low_price"] = 1.29800
    rows[10]["swing_low"] = True
    rows[10]["low"] = 1.29700
    rows[10]["swing_low_price"] = 1.29700

    # Pullback wick-penetrates EMA from below, closes below.
    rows[12].update(
        {
            "open": _EMA - 0.0005,
            "high": _EMA + 0.0010,  # wick above EMA
            "low": 1.29940,
            "close": _EMA - 0.0001,
        }
    )
    # Reclaim closes firmly below EMA.
    rows[13].update(
        {"open": _EMA - 0.0001, "high": _EMA + 0.0002, "low": 1.29920, "close": 1.29960}
    )
    # Confirmation: bearish-bodied, close < reclaim close.
    rows[14].update(
        {"open": 1.29960, "high": 1.29965, "low": 1.29900, "close": 1.29920}
    )

    return pd.DataFrame(rows, index=_index(n))


def _h1(*, slope: float = 0.45, macd_hist: float = 0.10) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ema_slope_norm_50_10": slope,
                "macd_hist_12_26_9": macd_hist,
                "bb_width_norm_20_2": 2.0,
            }
        ]
    )


def _state(
    *,
    regime: str = "TREND",
    direction: str | None = "BULLISH",
) -> dict:
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


def test_bullish_clean_setup_returns_signal() -> None:
    sig = detect_ema_continuation(
        df_m5=_m5_bullish_clean(),
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is not None
    assert sig.direction is Direction.BULLISH
    assert sig.regime is RegimeLabel.TREND
    assert sig.strategy_name == "ema_continuation"
    assert sig.suggested_tp_price is None  # trend → trail only


def test_bearish_clean_setup_returns_signal() -> None:
    sig = detect_ema_continuation(
        df_m5=_m5_bearish_clean(),
        df_h1=_h1(slope=-0.45, macd_hist=-0.10),
        regime_state=_state(direction="BEARISH"),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is not None
    assert sig.direction is Direction.BEARISH
    assert sig.confidence_score == pytest.approx(0.80)


# --- Regime gates -----------------------------------------------------------


def test_wrong_regime_returns_none() -> None:
    sig = detect_ema_continuation(
        df_m5=_m5_bullish_clean(),
        df_h1=_h1(),
        regime_state=_state(regime="RANGE"),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_direction_none_returns_none() -> None:
    sig = detect_ema_continuation(
        df_m5=_m5_bullish_clean(),
        df_h1=_h1(),
        regime_state=_state(direction=None),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_slope_too_weak_returns_none() -> None:
    sig = detect_ema_continuation(
        df_m5=_m5_bullish_clean(),
        df_h1=_h1(slope=0.20),  # below 0.35
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


# --- Structure --------------------------------------------------------------


def test_structure_misaligned_returns_none() -> None:
    """Bearish structure (LH/LL) with bullish TREND direction → reject."""
    df = _m5_bearish_clean()
    sig = detect_ema_continuation(
        df_m5=df,
        df_h1=_h1(),
        regime_state=_state(direction="BULLISH"),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


# --- Pattern misses ---------------------------------------------------------


def test_pullback_did_not_touch_ema_returns_none() -> None:
    df = _m5_bullish_clean()
    # Pullback low above EMA — no wick penetration.
    df.iloc[12, df.columns.get_loc("low")] = _EMA + 0.0005
    sig = detect_ema_continuation(
        df_m5=df,
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_pullback_close_too_far_below_ema_returns_none() -> None:
    df = _m5_bullish_clean()
    # Close pulled 10 pips below EMA — outside tolerance of 5 pips.
    df.iloc[12, df.columns.get_loc("close")] = _EMA - 0.0010
    sig = detect_ema_continuation(
        df_m5=df,
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_pullback_close_within_tolerance_accepted() -> None:
    df = _m5_bullish_clean()
    # 3 pips below EMA — inside default 5-pip tolerance.
    df.iloc[12, df.columns.get_loc("close")] = _EMA - 0.0003
    sig = detect_ema_continuation(
        df_m5=df,
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is not None


def test_reclaim_did_not_close_above_ema_returns_none() -> None:
    df = _m5_bullish_clean()
    df.iloc[13, df.columns.get_loc("close")] = _EMA - 0.0001
    sig = detect_ema_continuation(
        df_m5=df,
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_confirmation_not_bullish_bodied_returns_none() -> None:
    df = _m5_bullish_clean()
    df.iloc[14, df.columns.get_loc("close")] = df.iloc[14]["open"] - 0.0001
    sig = detect_ema_continuation(
        df_m5=df,
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


# --- Confidence -------------------------------------------------------------


def test_macd_aligned_yields_high_confidence() -> None:
    sig = detect_ema_continuation(
        df_m5=_m5_bullish_clean(),
        df_h1=_h1(macd_hist=0.10),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is not None and sig.confidence_score == pytest.approx(0.80)


def test_macd_opposite_yields_low_confidence() -> None:
    sig = detect_ema_continuation(
        df_m5=_m5_bullish_clean(),
        df_h1=_h1(macd_hist=-0.10),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is not None and sig.confidence_score == pytest.approx(0.60)


# --- SL --------------------------------------------------------------------


def test_sl_uses_atr_multiplier_when_larger() -> None:
    sig = detect_ema_continuation(
        df_m5=_m5_bullish_clean(),
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    # ATR=0.0020 → 20p × 1.2 = 24p. Floor=15p. SL distance=24p=0.0024.
    # Pullback low = _EMA - 0.0010 = 1.29900.
    assert sig is not None
    assert sig.suggested_sl_price == pytest.approx(1.29900 - 0.0024)


def test_sl_respects_min_pip_floor() -> None:
    df = _m5_bullish_clean()
    df["atr_14"] = 0.0005  # 5p × 1.2 = 6p < 15p floor
    sig = detect_ema_continuation(
        df_m5=df,
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is not None
    # Floor wins: 15 pips = 0.0015.
    assert sig.suggested_sl_price == pytest.approx(1.29900 - 0.0015)


def test_signal_carries_source_and_invalid_after() -> None:
    sig = detect_ema_continuation(
        df_m5=_m5_bullish_clean(),
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is not None
    assert sig.source_candle_ts == _NOW
    assert sig.invalid_after_candle_ts == _NOW + timedelta(minutes=5)
