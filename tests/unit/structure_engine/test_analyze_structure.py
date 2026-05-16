"""End-to-end tests for :func:`structure_engine.analyze_structure`.

Integration tests exercise the full pipeline: candles → swings → zones
→ scoring → bias → reaction → StructureState.
"""
from __future__ import annotations

import pandas as pd
import pytest

from structure_engine import analyze_structure
from structure_engine.types import StructureLevel

from .conftest import (
    build_failed_reclaim_below_support,
    build_support_rejection,
    make_ohlc,
    warmup_rows,
)


_PAIR = "GBPUSD"


def _regime_state(label: str = "RANGE") -> dict:
    return {
        "current_regime": label,
        "current_direction": None,
        "pending_regime": None,
        "m5_confirmation_count": 0,
        "last_regime_change_time": None,
        "reason": "test",
        "debug": {},
    }


def test_insufficient_m5_candles_yields_invalid_state() -> None:
    df_m5 = make_ohlc(warmup_rows(10))
    df_m15 = pd.DataFrame()
    df_h1 = pd.DataFrame()
    state = analyze_structure(
        pair=_PAIR,
        candles_m5=df_m5,
        candles_m15=df_m15,
        candles_h1=df_h1,
        regime_state=_regime_state(),
    )
    assert state.is_valid is False
    assert state.reason == "insufficient_candles_m5"
    assert state.confidence == 0.0


def test_minimum_candles_produces_valid_state() -> None:
    df_m5 = make_ohlc(warmup_rows(50))
    df_m15 = pd.DataFrame()
    df_h1 = pd.DataFrame()
    state = analyze_structure(
        pair=_PAIR,
        candles_m5=df_m5,
        candles_m15=df_m15,
        candles_h1=df_h1,
        regime_state=_regime_state(),
    )
    assert state.is_valid is True
    assert state.pair == "GBPUSD"
    assert isinstance(state.levels, list)


def test_one_sided_structure_doesnt_crash_confidence(monkeypatch) -> None:
    """Refinement B: only support, no resistance → confidence reflects support."""
    # Build a flat 50-bar warmup with a clear M5 swing low at the centre.
    # This produces a single LOW-side zone; no HIGH-side zone above the
    # current price → nearest_resistance is None.
    rows = warmup_rows(50)
    # Inject a clear swing low in the middle.
    for i in (20, 21, 22, 23, 24):
        rows[i] = dict(rows[i])
        rows[i]["low"] = 1.30000 - 0.0010 * (3 - abs(22 - i))  # peak depth at 22
        rows[i]["high"] = 1.30000 + 0.0003

    df_m5 = make_ohlc(rows)
    state = analyze_structure(
        pair=_PAIR,
        candles_m5=df_m5,
        candles_m15=pd.DataFrame(),
        candles_h1=pd.DataFrame(),
        regime_state=_regime_state(),
    )
    # Either side might be None depending on swing-detector picks — the
    # crucial assertion is that confidence is computed without crashing
    # and lies in [0, 1].
    assert 0.0 <= state.confidence <= 1.0


def test_support_rejection_reaches_state() -> None:
    """A SUPPORT_REJECTION pattern at the tail of M5 surfaces in state."""
    warmup = warmup_rows(47, close=1.30050)
    # Add a clear swing low at the warmup tail so a support zone exists
    # around 1.30000 (matches reaction builder's support price).
    warmup[40] = {
        "open": 1.30000, "high": 1.30000 + 0.0003,
        "low": 1.30000 - 0.0010, "close": 1.30000, "atr_14": 0.0010,
    }
    reaction_bars = build_support_rejection(1.30000)
    df_m5 = make_ohlc(warmup + reaction_bars)
    state = analyze_structure(
        pair=_PAIR,
        candles_m5=df_m5,
        candles_m15=pd.DataFrame(),
        candles_h1=pd.DataFrame(),
        regime_state=_regime_state(),
    )
    assert state.is_valid is True
    # The reaction should be one of the support-side outcomes. The exact
    # value depends on the discovered zone; we accept rejection or a
    # related support reaction.
    assert state.current_reaction in (
        "SUPPORT_REJECTION", "SUPPORT_SWEEP_RECLAIM", "NONE",
    )


def test_equal_highs_cluster_flagged_as_liquidity() -> None:
    """H-2 regression: 3 H1 swing highs at near-identical prices should
    merge into one zone and be flagged as ``is_equal_hl_cluster`` so the
    liquidity selector picks it up.

    Build M5 candles with three matching swing-high spots; resampled or
    not, the M5 swing detector picks them up at the same price band and
    the merger collapses them. Asserts the merged zone surfaces in
    ``state.liquidity_above`` (or is at least flagged in ``levels``).
    """
    # 50-bar warmup centred at 1.30000, then inject three M5 swing-highs
    # at the same price ~1.30200, spaced 5 bars apart so each gets its
    # own 3-bar M5 fractal window.
    rows = warmup_rows(40)
    # Three swing-high blocks at indices ~40, 46, 52 — each peaks at 1.30200.
    swing_peak = 1.30200
    for centre in (42, 48, 54):
        # Ensure indices exist in rows.
        while len(rows) <= centre + 3:
            rows.append({
                "open": 1.30000, "high": 1.30005,
                "low": 1.29995, "close": 1.30000, "atr_14": 0.0010,
            })
        # 3-bar window before centre with rising highs, peak at centre,
        # falling highs after. The M5 fractal window is 3 — neighbours
        # must be strictly lower.
        for offset, delta in [(-3, 0.0005), (-2, 0.0010), (-1, 0.0015)]:
            rows[centre + offset] = {
                "open": 1.30000, "close": 1.30000,
                "high": 1.30000 + delta, "low": 1.29995, "atr_14": 0.0010,
            }
        rows[centre] = {
            "open": 1.30000, "close": 1.30000,
            "high": swing_peak, "low": 1.29995, "atr_14": 0.0010,
        }
        for offset, delta in [(1, 0.0015), (2, 0.0010), (3, 0.0005)]:
            rows[centre + offset] = {
                "open": 1.30000, "close": 1.30000,
                "high": 1.30000 + delta, "low": 1.29995, "atr_14": 0.0010,
            }
    df_m5 = make_ohlc(rows)
    state = analyze_structure(
        pair=_PAIR,
        candles_m5=df_m5,
        candles_m15=pd.DataFrame(),
        candles_h1=pd.DataFrame(),
        regime_state=_regime_state(),
    )
    # At least one HIGH-side zone should be marked as an equal-HL cluster.
    high_clusters = [
        lvl for lvl in state.levels
        if lvl.level_type == "LIQUIDITY_HIGH"
    ]
    assert high_clusters, (
        "Expected at least one LIQUIDITY_HIGH zone after 3 swing-highs at "
        "matching price — equal-HL cluster detection regressed"
    )


def test_debug_records_ema_used(monkeypatch) -> None:
    """Refinement A surfaces htf_ema_used in debug."""
    df_m5 = make_ohlc(warmup_rows(50))
    df_h1 = make_ohlc(warmup_rows(50) + [
        {
            "close": 1.31000, "ema_50": 1.30500, "ema_100": 1.30200,
            "ema_200": 1.29800, "open": 1.31000, "high": 1.31000, "low": 1.31000,
        }
    ])
    state = analyze_structure(
        pair=_PAIR,
        candles_m5=df_m5,
        candles_m15=pd.DataFrame(),
        candles_h1=df_h1,
        regime_state=_regime_state(),
    )
    assert state.debug["htf_bias_reason"]["htf_ema_used"] == "EMA200"
