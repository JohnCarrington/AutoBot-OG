"""Unit tests for :mod:`structure_engine.reaction_detector`."""
from __future__ import annotations

import pandas as pd
import pytest

from structure_engine.reaction_detector import classify_reaction
from structure_engine.zone_builder import CandidateZone, make_zone

from .conftest import (
    build_failed_reclaim_above_resistance,
    build_failed_reclaim_below_support,
    build_resistance_acceptance_break,
    build_resistance_rejection,
    build_resistance_sweep_reclaim,
    build_support_acceptance_break,
    build_support_rejection,
    build_support_sweep_reclaim,
    make_ohlc,
)


_SUPPORT_PRICE = 1.30000
_RESISTANCE_PRICE = 1.30500
_HALF = 0.0004  # 4-pip GBPUSD floor


def _support_zone() -> CandidateZone:
    return make_zone(
        pair="GBPUSD", side="LOW", price=_SUPPORT_PRICE, timeframe="H1",
        half_width=_HALF, source="swing_h1",
    )


def _resistance_zone() -> CandidateZone:
    return make_zone(
        pair="GBPUSD", side="HIGH", price=_RESISTANCE_PRICE, timeframe="H1",
        half_width=_HALF, source="swing_h1",
    )


def test_support_rejection() -> None:
    df = make_ohlc(build_support_rejection(_SUPPORT_PRICE))
    reaction, accept, _ = classify_reaction(
        df_m5=df,
        nearest_support=_support_zone(),
        nearest_resistance=None,
    )
    assert reaction == "SUPPORT_REJECTION"
    assert accept == "INSIDE_RANGE"


def test_resistance_rejection() -> None:
    df = make_ohlc(build_resistance_rejection(_RESISTANCE_PRICE))
    reaction, _, _ = classify_reaction(
        df_m5=df,
        nearest_support=None,
        nearest_resistance=_resistance_zone(),
    )
    assert reaction == "RESISTANCE_REJECTION"


def test_support_sweep_reclaim() -> None:
    df = make_ohlc(build_support_sweep_reclaim(_SUPPORT_PRICE))
    reaction, _, _ = classify_reaction(
        df_m5=df,
        nearest_support=_support_zone(),
        nearest_resistance=None,
    )
    assert reaction == "SUPPORT_SWEEP_RECLAIM"


def test_resistance_sweep_reclaim() -> None:
    df = make_ohlc(build_resistance_sweep_reclaim(_RESISTANCE_PRICE))
    reaction, _, _ = classify_reaction(
        df_m5=df,
        nearest_support=None,
        nearest_resistance=_resistance_zone(),
    )
    assert reaction == "RESISTANCE_SWEEP_RECLAIM"


def test_support_acceptance_break() -> None:
    df = make_ohlc(build_support_acceptance_break(_SUPPORT_PRICE))
    reaction, accept, _ = classify_reaction(
        df_m5=df,
        nearest_support=_support_zone(),
        nearest_resistance=None,
    )
    assert reaction == "SUPPORT_ACCEPTANCE_BREAK"
    assert accept == "ACCEPTED_BELOW_SUPPORT"


def test_resistance_acceptance_break() -> None:
    df = make_ohlc(build_resistance_acceptance_break(_RESISTANCE_PRICE))
    reaction, accept, _ = classify_reaction(
        df_m5=df,
        nearest_support=None,
        nearest_resistance=_resistance_zone(),
    )
    assert reaction == "RESISTANCE_ACCEPTANCE_BREAK"
    assert accept == "ACCEPTED_ABOVE_RESISTANCE"


def test_failed_reclaim_below_support() -> None:
    df = make_ohlc(build_failed_reclaim_below_support(_SUPPORT_PRICE))
    reaction, accept, _ = classify_reaction(
        df_m5=df,
        nearest_support=_support_zone(),
        nearest_resistance=None,
    )
    assert reaction == "FAILED_RECLAIM_BELOW_SUPPORT"
    assert accept == "REJECTED_BELOW_SUPPORT"


def test_failed_reclaim_above_resistance() -> None:
    df = make_ohlc(build_failed_reclaim_above_resistance(_RESISTANCE_PRICE))
    reaction, accept, _ = classify_reaction(
        df_m5=df,
        nearest_support=None,
        nearest_resistance=_resistance_zone(),
    )
    assert reaction == "FAILED_RECLAIM_ABOVE_RESISTANCE"
    assert accept == "REJECTED_ABOVE_RESISTANCE"


def test_no_reaction_returns_none() -> None:
    """Flat bars far above support should return NONE."""
    df = make_ohlc([
        {"close": 1.30200, "high": 1.30210, "low": 1.30190, "open": 1.30200},
        {"close": 1.30205, "high": 1.30215, "low": 1.30195, "open": 1.30200},
        {"close": 1.30210, "high": 1.30220, "low": 1.30200, "open": 1.30205},
    ])
    reaction, _, _ = classify_reaction(
        df_m5=df,
        nearest_support=_support_zone(),
        nearest_resistance=_resistance_zone(),
    )
    assert reaction == "NONE"


def test_insufficient_bars_returns_none() -> None:
    df = make_ohlc([
        {"close": 1.30000}, {"close": 1.30000},  # only 2 bars
    ])
    reaction, _, reason = classify_reaction(
        df_m5=df, nearest_support=_support_zone(), nearest_resistance=None,
    )
    assert reaction == "NONE"
    assert reason == "insufficient_bars"
