"""Unit tests for :mod:`structure_engine.bias_detector`.

Refinement A: EMA200 → EMA100 → EMA50 graceful degradation.
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from structure_engine.bias_detector import detect_htf_bias

from .conftest import make_ohlc, warmup_rows


def _h1_row(*, close: float, **emas) -> dict:
    row = {"close": close, "open": close, "high": close, "low": close}
    row.update(emas)
    return row


def test_warmup_only_ema50_falls_back_to_ema50() -> None:
    rows = warmup_rows(60, close=1.30000) + [
        _h1_row(close=1.31000, ema_50=1.30500),  # EMA200/100 still NaN
    ]
    df = make_ohlc(rows)
    bias, debug = detect_htf_bias(df)
    assert debug["htf_ema_used"] == "EMA50"
    assert bias == "BULLISH"


def test_warmup_with_ema100_uses_ema100() -> None:
    rows = warmup_rows(60, close=1.30000) + [
        _h1_row(close=1.31000, ema_50=1.30500, ema_100=1.30200),
    ]
    df = make_ohlc(rows)
    bias, debug = detect_htf_bias(df)
    assert debug["htf_ema_used"] == "EMA100"
    assert bias == "BULLISH"


def test_fully_warmed_uses_ema200() -> None:
    rows = warmup_rows(60, close=1.30000) + [
        _h1_row(
            close=1.31000,
            ema_50=1.30500,
            ema_100=1.30200,
            ema_200=1.29800,
        ),
    ]
    df = make_ohlc(rows)
    bias, debug = detect_htf_bias(df)
    assert debug["htf_ema_used"] == "EMA200"
    assert bias == "BULLISH"


def test_no_ema_data_returns_neutral_with_reason() -> None:
    df = make_ohlc([
        _h1_row(close=1.31000, ema_50=float("nan")),  # all EMAs NaN
    ])
    bias, debug = detect_htf_bias(df)
    assert bias == "NEUTRAL"
    assert debug["reason"] == "insufficient_ema_data"
    assert debug["htf_ema_used"] is None


def test_empty_h1_returns_neutral() -> None:
    bias, debug = detect_htf_bias(pd.DataFrame())
    assert bias == "NEUTRAL"
    assert debug["reason"] == "no_h1_data"


def test_price_below_ema_returns_bearish() -> None:
    rows = warmup_rows(60, close=1.30000) + [
        _h1_row(close=1.29000, ema_50=1.30500, ema_100=1.30200, ema_200=1.29800),
    ]
    df = make_ohlc(rows)
    bias, _ = detect_htf_bias(df)
    assert bias == "BEARISH"
