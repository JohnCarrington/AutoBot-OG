"""Unit tests for :mod:`structure_engine.mode_classifier`."""
from __future__ import annotations

import pandas as pd
import pytest

from structure_engine.mode_classifier import classify_mode

from .conftest import make_ohlc, warmup_rows


def _m5_with_atr(rows: list[dict]) -> pd.DataFrame:
    return make_ohlc(rows)


def _h1(*, bb_width: float = 1.0, slope: float = 0.0) -> pd.DataFrame:
    return make_ohlc([
        {
            "close": 1.30000,
            "bb_width_norm_20_2": bb_width,
            "ema_slope_norm_50_10": slope,
        }
    ])


def test_range_balance_compressed_bb() -> None:
    """Spec §16 test 9: BB width compressed, no acceptance break → RANGE_BALANCE."""
    df_m5 = _m5_with_atr(warmup_rows(50))
    df_h1 = _h1(bb_width=1.0, slope=0.05)
    mode, _ = classify_mode(
        df_m5=df_m5,
        df_h1=df_h1,
        htf_bias="NEUTRAL",
        local_bias="NEUTRAL",
        current_reaction="NONE",
        near_liquidity=False,
    )
    assert mode == "RANGE_BALANCE"


def test_trend_continuation_with_directional_slope() -> None:
    """Spec §16 test 10: directional slope + acceptance break → TREND_CONTINUATION."""
    df_m5 = _m5_with_atr(warmup_rows(50))
    df_h1 = _h1(bb_width=2.5, slope=-0.50)  # steep bearish slope (negative)
    mode, _ = classify_mode(
        df_m5=df_m5,
        df_h1=df_h1,
        htf_bias="BEARISH",
        local_bias="BEARISH",
        current_reaction="SUPPORT_ACCEPTANCE_BREAK",
        near_liquidity=False,
    )
    assert mode == "TREND_CONTINUATION"


def test_volatile_sweep_zone_elevated_atr_plus_liquidity() -> None:
    rows = warmup_rows(50, atr=0.0010)
    # Force latest ATR to be 2× the median.
    rows[-1] = dict(rows[-1])
    rows[-1]["atr_14"] = 0.0025
    df_m5 = _m5_with_atr(rows)
    df_h1 = _h1(bb_width=2.0, slope=0.10)
    mode, _ = classify_mode(
        df_m5=df_m5,
        df_h1=df_h1,
        htf_bias="NEUTRAL",
        local_bias="NEUTRAL",
        current_reaction="NONE",
        near_liquidity=True,
    )
    assert mode == "VOLATILE_SWEEP_ZONE"


def test_transition_when_htf_local_disagree() -> None:
    df_m5 = _m5_with_atr(warmup_rows(50))
    df_h1 = _h1(bb_width=2.0, slope=0.20)  # not range, not trend
    mode, _ = classify_mode(
        df_m5=df_m5,
        df_h1=df_h1,
        htf_bias="BEARISH",
        local_bias="BULLISH",
        current_reaction="NONE",
        near_liquidity=False,
    )
    assert mode == "TRANSITION"


def test_unknown_when_inputs_missing() -> None:
    mode, reason = classify_mode(
        df_m5=pd.DataFrame(),
        df_h1=pd.DataFrame(),
        htf_bias="NEUTRAL",
        local_bias="NEUTRAL",
        current_reaction="NONE",
        near_liquidity=False,
    )
    assert mode == "UNKNOWN"
    assert reason == "insufficient_data"
