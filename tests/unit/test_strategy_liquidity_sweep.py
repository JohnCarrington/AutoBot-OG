"""Tests for the Liquidity Sweep strategy.

Structure swings are populated directly; session predicates are exercised
via NY-window timestamps. Asia rejected via early-morning UTC times.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from regime.labels import Direction, RegimeLabel
from strategies.liquidity_sweep import detect_liquidity_sweep


_PAIR = "GBPUSD"
# 13:00 UTC Wed = 09:00 EDT — NY session is open.
_NY_NOW = datetime(2025, 5, 14, 13, 0, tzinfo=timezone.utc)
# 03:00 UTC = Asia/Tokyo session — outside London + NY.
_ASIA_NOW = datetime(2025, 5, 14, 3, 0, tzinfo=timezone.utc)


def _index(n_bars: int, end: datetime) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(
        [end - timedelta(minutes=5 * (n_bars - 1 - i)) for i in range(n_bars)]
    )


def _m5_long_clean(end: datetime = _NY_NOW) -> pd.DataFrame:
    """15 M5 bars with a recent swing low at 1.29800 and a clean LONG sweep."""
    n = 15
    rows = []
    for _ in range(n):
        rows.append(
            {
                "open": 1.30000,
                "high": 1.30050,
                "low": 1.29950,
                "close": 1.30000,
                "atr_14": 0.0020,
                "swing_high": False,
                "swing_low": False,
                "swing_high_price": float("nan"),
                "swing_low_price": float("nan"),
            }
        )
    # Recent swing structure: low at idx 9 (1.29800), high at idx 7 (1.30050)
    # → last_swing_low = 1.29800, age = 14-9 = 5 bars.
    rows[9].update(
        {"swing_low": True, "low": 1.29800, "swing_low_price": 1.29800}
    )
    rows[7].update(
        {"swing_high": True, "high": 1.30200, "swing_high_price": 1.30200}
    )
    # Also need a 2nd swing high & low for the lookback recent_pattern logic
    # (the strategy itself doesn't gate on recent_pattern, but get_structure_state
    # still needs ≥1 of each type within the dataframe — and it has them above).

    # Sweep bar (idx 12): low pierces 1.29800 by 12 pips → strong sweep
    # (must exceed 0.5 × ATR_M5 = 0.5 × 0.0020 = 0.0010 in price units).
    rows[12].update(
        {
            "open": 1.29870,
            "high": 1.29880,
            "low": 1.29680,
            "close": 1.29860,
        }
    )
    # Reclaim bar (idx 13): close back above swing low.
    rows[13].update(
        {"open": 1.29860, "high": 1.29910, "low": 1.29830, "close": 1.29890}
    )
    # Confirmation bar (idx 14): bullish, close > reclaim.
    rows[14].update(
        {"open": 1.29890, "high": 1.29960, "low": 1.29885, "close": 1.29940}
    )
    return pd.DataFrame(rows, index=_index(n, end))


def _m5_short_clean(end: datetime = _NY_NOW) -> pd.DataFrame:
    """Mirror: recent swing high at 1.30200 with a clean SHORT sweep."""
    n = 15
    rows = []
    for _ in range(n):
        rows.append(
            {
                "open": 1.30000,
                "high": 1.30050,
                "low": 1.29950,
                "close": 1.30000,
                "atr_14": 0.0020,
                "swing_high": False,
                "swing_low": False,
                "swing_high_price": float("nan"),
                "swing_low_price": float("nan"),
            }
        )
    rows[9].update(
        {"swing_high": True, "high": 1.30200, "swing_high_price": 1.30200}
    )
    rows[7].update(
        {"swing_low": True, "low": 1.29800, "swing_low_price": 1.29800}
    )
    # Sweep above the swing high.
    rows[12].update(
        {"open": 1.30150, "high": 1.30250, "low": 1.30140, "close": 1.30160}
    )
    rows[13].update(
        {"open": 1.30160, "high": 1.30180, "low": 1.30100, "close": 1.30120}
    )
    # Bearish confirmation.
    rows[14].update(
        {"open": 1.30120, "high": 1.30125, "low": 1.30060, "close": 1.30080}
    )
    return pd.DataFrame(rows, index=_index(n, end))


def _h1() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ema_slope_norm_50_10": 0.20,
                "bb_width_norm_20_2": 3.0,
                "macd_hist_12_26_9": 0.0,
            }
        ]
    )


def _state(*, regime: str = "VOLATILE") -> dict:
    return {
        "current_regime": regime,
        "current_direction": None,
        "pending_regime": None,
        "m5_confirmation_count": 0,
        "last_regime_change_time": None,
        "reason": "test",
        "debug": {},
    }


# --- Happy path -------------------------------------------------------------


def test_long_clean_setup_returns_signal() -> None:
    sig = detect_liquidity_sweep(
        df_m5=_m5_long_clean(),
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NY_NOW,
    )
    assert sig is not None
    assert sig.direction is Direction.BULLISH
    assert sig.regime is RegimeLabel.VOLATILE
    assert sig.strategy_name == "liquidity_sweep"
    assert sig.suggested_tp_price is None


def test_short_clean_setup_returns_signal() -> None:
    sig = detect_liquidity_sweep(
        df_m5=_m5_short_clean(),
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NY_NOW,
    )
    assert sig is not None
    assert sig.direction is Direction.BEARISH


# --- Regime + session gates ------------------------------------------------


def test_wrong_regime_returns_none() -> None:
    sig = detect_liquidity_sweep(
        df_m5=_m5_long_clean(),
        df_h1=_h1(),
        regime_state=_state(regime="TREND"),
        pair=_PAIR,
        current_time=_NY_NOW,
    )
    assert sig is None


def test_asia_session_rejected() -> None:
    sig = detect_liquidity_sweep(
        df_m5=_m5_long_clean(end=_ASIA_NOW),
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_ASIA_NOW,
    )
    assert sig is None


def test_london_session_accepted() -> None:
    london_now = datetime(2025, 5, 14, 8, 0, tzinfo=timezone.utc)  # 09:00 BST
    sig = detect_liquidity_sweep(
        df_m5=_m5_long_clean(end=london_now),
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=london_now,
    )
    assert sig is not None


# --- Pattern gates ----------------------------------------------------------


def test_sweep_did_not_pierce_swing_returns_none() -> None:
    df = _m5_long_clean()
    df.iloc[12, df.columns.get_loc("low")] = 1.29820  # above the 1.29800 swing
    sig = detect_liquidity_sweep(
        df_m5=df,
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NY_NOW,
    )
    assert sig is None


def test_reclaim_did_not_close_above_swing_returns_none() -> None:
    df = _m5_long_clean()
    df.iloc[13, df.columns.get_loc("close")] = 1.29790
    sig = detect_liquidity_sweep(
        df_m5=df,
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NY_NOW,
    )
    assert sig is None


def test_confirmation_not_bullish_returns_none() -> None:
    df = _m5_long_clean()
    # Close below open → bearish-bodied.
    df.iloc[14, df.columns.get_loc("close")] = df.iloc[14]["open"] - 0.00010
    sig = detect_liquidity_sweep(
        df_m5=df,
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NY_NOW,
    )
    assert sig is None


def test_confirmation_close_below_reclaim_returns_none() -> None:
    df = _m5_long_clean()
    df.iloc[14, df.columns.get_loc("close")] = 1.29870  # below reclaim 1.29890
    sig = detect_liquidity_sweep(
        df_m5=df,
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NY_NOW,
    )
    assert sig is None


def test_no_recent_swing_returns_none() -> None:
    df = _m5_long_clean()
    # Wipe swing markers — no structural swing to fade.
    df["swing_low"] = False
    df["swing_low_price"] = float("nan")
    sig = detect_liquidity_sweep(
        df_m5=df,
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NY_NOW,
    )
    assert sig is None


# --- Confidence -------------------------------------------------------------


def test_strong_sweep_yields_high_confidence() -> None:
    """Sweep extends > 0.5 × ATR (= 0.0010) beyond the swing → 0.75."""
    df = _m5_long_clean()
    # Default sweep low 1.29680 → 12 pips below 1.29800 (0.0012 > 0.0010).
    sig = detect_liquidity_sweep(
        df_m5=df,
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NY_NOW,
    )
    assert sig is not None and sig.confidence_score == pytest.approx(0.75)


def test_shallow_sweep_yields_low_confidence() -> None:
    df = _m5_long_clean()
    # Sweep wick only 2 pips below swing low (< 0.5×ATR = 10 pips).
    df.iloc[12, df.columns.get_loc("low")] = 1.29798
    sig = detect_liquidity_sweep(
        df_m5=df,
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NY_NOW,
    )
    assert sig is not None and sig.confidence_score == pytest.approx(0.55)


# --- SL --------------------------------------------------------------------


def test_sl_anchors_to_sweep_extreme_long() -> None:
    sig = detect_liquidity_sweep(
        df_m5=_m5_long_clean(),
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NY_NOW,
    )
    # ATR=0.0020 → 20p × 1.0 = 20p; floor=15p; max(15,20)=20p=0.0020.
    # Anchor = sweep.low = 1.29680.
    assert sig is not None
    assert sig.suggested_sl_price == pytest.approx(1.29680 - 0.0020)


def test_sl_respects_min_pip_floor() -> None:
    df = _m5_long_clean()
    df["atr_14"] = 0.0010  # 10p × 1.0 = 10p < 15p floor
    sig = detect_liquidity_sweep(
        df_m5=df,
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NY_NOW,
    )
    assert sig is not None
    assert sig.suggested_sl_price == pytest.approx(1.29680 - 0.0015)


# --- Metadata ---------------------------------------------------------------


def test_signal_metadata() -> None:
    sig = detect_liquidity_sweep(
        df_m5=_m5_long_clean(),
        df_h1=_h1(),
        regime_state=_state(),
        pair=_PAIR,
        current_time=_NY_NOW,
    )
    assert sig is not None
    assert sig.source_candle_ts == _NY_NOW
    assert sig.invalid_after_candle_ts == _NY_NOW + timedelta(minutes=5)
    assert "sweep_magnitude_price" in sig.debug
