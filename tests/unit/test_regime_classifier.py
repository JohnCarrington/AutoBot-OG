"""Unit tests for src.regime.classifier."""
from __future__ import annotations

import numpy as np
import pandas as pd

from regime.classifier import (
    add_structural_pattern_column,
    classify_h1,
    compute_structural_pattern,
)
from regime.labels import Confidence, Direction, RegimeLabel


def _h1_row(
    *,
    structural_pattern: str = "INSUFFICIENT_DATA",
    slope: float = 0.0,
    bb_width: float = 2.0,
    macd_hist: float = 0.0,
) -> pd.Series:
    return pd.Series(
        {
            "structural_pattern": structural_pattern,
            "ema_slope_norm_50_10": slope,
            "bb_width_norm_20_2": bb_width,
            "macd_hist_12_26_9": macd_hist,
        }
    )


# --- Compound structural pattern ---------------------------------------------


def test_compute_structural_pattern_hh_hl() -> None:
    # Two ascending highs at positions 1, 4 and two ascending lows at 2, 5.
    high_positions = np.array([1, 4])
    low_positions = np.array([2, 5])
    highs = np.array([0, 10, 0, 0, 12, 0])
    lows = np.array([0, 0, 5, 0, 0, 7])
    assert (
        compute_structural_pattern(
            high_positions, low_positions, highs, lows, current_index=5, lookback_bars=10
        )
        == "HH+HL"
    )


def test_compute_structural_pattern_lh_ll() -> None:
    high_positions = np.array([1, 4])
    low_positions = np.array([2, 5])
    highs = np.array([0, 12, 0, 0, 10, 0])
    lows = np.array([0, 0, 7, 0, 0, 5])
    assert (
        compute_structural_pattern(
            high_positions, low_positions, highs, lows, current_index=5, lookback_bars=10
        )
        == "LH+LL"
    )


def test_compute_structural_pattern_insufficient() -> None:
    high_positions = np.array([1])
    low_positions = np.array([2, 5])
    highs = np.array([0, 10, 0, 0, 0, 0])
    lows = np.array([0, 0, 5, 0, 0, 7])
    assert (
        compute_structural_pattern(
            high_positions, low_positions, highs, lows, current_index=5, lookback_bars=10
        )
        == "INSUFFICIENT_DATA"
    )


def test_compute_structural_pattern_lookback_excludes_old() -> None:
    # Both swing highs are far in the past; a small lookback window
    # excludes them so the result is INSUFFICIENT_DATA.
    high_positions = np.array([1, 4])
    low_positions = np.array([2, 5])
    highs = np.array([0.0] * 20)
    highs[1] = 10
    highs[4] = 12
    lows = np.array([0.0] * 20)
    lows[2] = 5
    lows[5] = 7
    assert (
        compute_structural_pattern(
            high_positions, low_positions, highs, lows, current_index=19, lookback_bars=5
        )
        == "INSUFFICIENT_DATA"
    )


def test_add_structural_pattern_column_round_trip() -> None:
    n = 12
    df = pd.DataFrame(
        {
            "high": [1.0] * n,
            "low": [0.0] * n,
            "swing_high": [False] * n,
            "swing_low": [False] * n,
        }
    )
    df.loc[1, "swing_high"] = True
    df.loc[4, "swing_high"] = True
    df.loc[2, "swing_low"] = True
    df.loc[5, "swing_low"] = True
    df.loc[1, "high"] = 10
    df.loc[4, "high"] = 12
    df.loc[2, "low"] = 5
    df.loc[5, "low"] = 7
    out = add_structural_pattern_column(df, lookback_bars=10)
    assert "structural_pattern" in out.columns
    assert out["structural_pattern"].iloc[5] == "HH+HL"
    # The original frame is untouched.
    assert "structural_pattern" not in df.columns


# --- classify_h1 -------------------------------------------------------------


def test_classify_clean_uptrend() -> None:
    row = _h1_row(
        structural_pattern="HH+HL", slope=0.45, bb_width=2.2, macd_hist=0.5
    )
    label, direction, conf, reason = classify_h1(row)
    assert label == RegimeLabel.TREND
    assert direction == Direction.BULLISH
    assert conf == Confidence.HIGH
    assert reason == "classified"


def test_classify_clean_downtrend() -> None:
    row = _h1_row(
        structural_pattern="LH+LL", slope=-0.45, bb_width=2.2, macd_hist=-0.5
    )
    label, direction, conf, reason = classify_h1(row)
    assert label == RegimeLabel.TREND
    assert direction == Direction.BEARISH
    assert conf == Confidence.HIGH
    assert reason == "classified"


def test_classify_compressed_range() -> None:
    row = _h1_row(
        structural_pattern="INSUFFICIENT_DATA", slope=0.05, bb_width=1.5
    )
    label, direction, conf, reason = classify_h1(row)
    assert label == RegimeLabel.RANGE
    assert direction is None
    assert conf == Confidence.MEDIUM
    assert reason == "classified"


def test_classify_volatile_structure_conflict() -> None:
    row = _h1_row(structural_pattern="HH+LL", slope=0.5, bb_width=2.0)
    label, direction, conf, reason = classify_h1(row)
    assert label == RegimeLabel.VOLATILE
    assert direction is None
    assert conf == Confidence.LOW
    assert reason == "structure_conflict"


def test_classify_structure_slope_conflict() -> None:
    # Structure says bullish trend, but slope is -0.4 (bearish).
    row = _h1_row(structural_pattern="HH+HL", slope=-0.4, bb_width=2.0)
    label, direction, conf, reason = classify_h1(row)
    assert label == RegimeLabel.VOLATILE
    assert direction is None
    assert conf == Confidence.LOW
    assert reason == "structure_slope_conflict"


def test_classify_trend_without_expansion() -> None:
    # Strong slope but BBs compressed: downgrade to TRANSITION, keep direction.
    row = _h1_row(
        structural_pattern="INSUFFICIENT_DATA", slope=0.50, bb_width=1.5
    )
    label, direction, conf, reason = classify_h1(row)
    assert label == RegimeLabel.TRANSITION
    assert direction == Direction.BULLISH
    assert conf == Confidence.LOW
    assert reason == "trend_no_expansion"


def test_classify_range_with_expansion() -> None:
    # Flat slope but BBs already expanded: not actually ranging.
    row = _h1_row(
        structural_pattern="INSUFFICIENT_DATA", slope=0.05, bb_width=2.8
    )
    label, direction, conf, reason = classify_h1(row)
    assert label == RegimeLabel.VOLATILE
    assert direction is None
    assert conf == Confidence.LOW
    assert reason == "range_with_expansion"


def test_classify_volatility_expansion() -> None:
    prev = _h1_row(
        structural_pattern="HH+HL", slope=0.40, bb_width=1.5, macd_hist=0.2
    )
    row = _h1_row(
        structural_pattern="HH+HL", slope=0.40, bb_width=2.0, macd_hist=0.2
    )
    label, direction, conf, reason = classify_h1(row, prev)
    assert label == RegimeLabel.VOLATILE
    # Volatility out of a trending bias retains the direction.
    assert direction == Direction.BULLISH
    assert conf == Confidence.LOW
    assert reason == "volatility_expansion"


def test_classify_macd_aligns_high_confidence() -> None:
    row = _h1_row(
        structural_pattern="HH+HL", slope=0.40, bb_width=2.0, macd_hist=0.1
    )
    _, _, conf, _ = classify_h1(row)
    assert conf == Confidence.HIGH


def test_classify_macd_disagrees_medium_confidence() -> None:
    row = _h1_row(
        structural_pattern="HH+HL", slope=0.40, bb_width=2.0, macd_hist=-0.1
    )
    _, _, conf, _ = classify_h1(row)
    assert conf == Confidence.MEDIUM


def test_classify_transitional_slope() -> None:
    # Slope inside the 0.15 .. 0.35 grey band, no structure to pin it.
    row = _h1_row(
        structural_pattern="INSUFFICIENT_DATA", slope=0.25, bb_width=2.0
    )
    label, direction, conf, reason = classify_h1(row)
    assert label == RegimeLabel.TRANSITION
    assert direction is None
    assert conf == Confidence.LOW
    assert reason == "slope_transitional"


def test_classify_nan_indicators_emits_transition() -> None:
    row = _h1_row(slope=float("nan"), bb_width=float("nan"))
    label, direction, conf, reason = classify_h1(row)
    assert label == RegimeLabel.TRANSITION
    assert direction is None
    assert conf == Confidence.LOW
    assert reason == "insufficient_indicator_data"
