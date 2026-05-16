"""Tests for the Phase 11 Liquidity Sweep strategy rewrite."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from regime.labels import Direction, RegimeLabel
from strategies.liquidity_sweep import detect_liquidity_sweep
from structure_engine import StructureLevel, StructureState


_PAIR = "GBPUSD"
# London session — 12:00 UTC.
_LONDON_NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
# Asia session — 22:00 UTC (London closed, NY closed).
_ASIA_NOW = datetime(2026, 5, 15, 22, 0, tzinfo=timezone.utc)


def _m5(*, ts: datetime, sweep_low: float = 1.29900, base: float = 1.30050) -> pd.DataFrame:
    idx = pd.DatetimeIndex(
        [ts - timedelta(minutes=5 * i) for i in range(2, -1, -1)]
    )
    return pd.DataFrame(
        [
            {"open": base, "high": base + 0.0005, "low": sweep_low,
             "close": base, "atr_14": 0.0020},
            {"open": base, "high": base + 0.0005, "low": base - 0.0005,
             "close": base, "atr_14": 0.0020},
            {"open": base, "high": base + 0.0005, "low": base - 0.0005,
             "close": base, "atr_14": 0.0020},
        ],
        index=idx,
    )


def _state() -> dict:
    return {
        "current_regime": "VOLATILE",
        "current_direction": None,
        "pending_regime": None,
        "m5_confirmation_count": 0,
        "last_regime_change_time": None,
        "reason": "test",
        "debug": {},
    }


def _level(side: str, price: float) -> StructureLevel:
    return StructureLevel(
        pair=_PAIR,
        level_type=("SUPPORT" if side == "LOW" else "RESISTANCE"),
        price=price,
        zone_low=price - 0.0004,
        zone_high=price + 0.0004,
        timeframe="H1",
        score=7.0,
        touch_count=2,
        last_touched_ts=None,
        source="swing_h1",
        debug={},
    )


def _structure(
    *,
    reaction: str = "SUPPORT_SWEEP_RECLAIM",
    htf_bias: str = "NEUTRAL",
    liquidity_below: bool = True,
    liquidity_above: bool = False,
    is_valid: bool = True,
) -> StructureState:
    lb = _level("LOW", 1.29800) if liquidity_below else None
    la = _level("HIGH", 1.30300) if liquidity_above else None
    return StructureState(
        pair=_PAIR,
        timestamp=_LONDON_NOW.isoformat(),
        is_valid=is_valid,
        htf_bias=htf_bias,  # type: ignore[arg-type]
        local_bias="NEUTRAL",
        nearest_support=_level("LOW", 1.30000),
        nearest_resistance=_level("HIGH", 1.30200),
        liquidity_above=la,
        liquidity_below=lb,
        current_reaction=reaction,  # type: ignore[arg-type]
        acceptance_state="INSIDE_RANGE",
        structure_mode="VOLATILE_SWEEP_ZONE",
        confidence=0.7,
        reason="test",
        levels=[],
        debug={},
    )


def test_support_sweep_reclaim_with_liquidity_below_emits_buy() -> None:
    sig = detect_liquidity_sweep(
        df_m5=_m5(ts=_LONDON_NOW),
        df_h1=pd.DataFrame(),
        regime_state=_state(),
        structure_state=_structure(),
        pair=_PAIR,
        current_time=_LONDON_NOW,
    )
    assert sig is not None
    assert sig.direction is Direction.BULLISH
    assert sig.regime is RegimeLabel.VOLATILE


def test_resistance_sweep_reclaim_with_liquidity_above_emits_sell() -> None:
    sig = detect_liquidity_sweep(
        df_m5=_m5(ts=_LONDON_NOW),
        df_h1=pd.DataFrame(),
        regime_state=_state(),
        structure_state=_structure(
            reaction="RESISTANCE_SWEEP_RECLAIM",
            htf_bias="BEARISH",
            liquidity_above=True,
            liquidity_below=False,
        ),
        pair=_PAIR,
        current_time=_LONDON_NOW,
    )
    assert sig is not None
    assert sig.direction is Direction.BEARISH


def test_asia_session_rejected() -> None:
    sig = detect_liquidity_sweep(
        df_m5=_m5(ts=_ASIA_NOW),
        df_h1=pd.DataFrame(),
        regime_state=_state(),
        structure_state=_structure(),
        pair=_PAIR,
        current_time=_ASIA_NOW,
    )
    assert sig is None


def test_wrong_regime_returns_none() -> None:
    state = _state()
    state["current_regime"] = "RANGE"
    sig = detect_liquidity_sweep(
        df_m5=_m5(ts=_LONDON_NOW),
        df_h1=pd.DataFrame(),
        regime_state=state,
        structure_state=_structure(),
        pair=_PAIR,
        current_time=_LONDON_NOW,
    )
    assert sig is None


def test_missing_liquidity_below_blocks_long() -> None:
    sig = detect_liquidity_sweep(
        df_m5=_m5(ts=_LONDON_NOW),
        df_h1=pd.DataFrame(),
        regime_state=_state(),
        structure_state=_structure(liquidity_below=False),
        pair=_PAIR,
        current_time=_LONDON_NOW,
    )
    assert sig is None


def test_bullish_htf_blocks_short() -> None:
    sig = detect_liquidity_sweep(
        df_m5=_m5(ts=_LONDON_NOW),
        df_h1=pd.DataFrame(),
        regime_state=_state(),
        structure_state=_structure(
            reaction="RESISTANCE_SWEEP_RECLAIM",
            htf_bias="BULLISH",  # disagrees with bearish setup
            liquidity_above=True,
            liquidity_below=False,
        ),
        pair=_PAIR,
        current_time=_LONDON_NOW,
    )
    assert sig is None


# --- H-1 regression: structure_mode gate ------------------------------------
# Every other test in this file leaves structure_mode at the default
# "VOLATILE_SWEEP_ZONE", so the gate was previously vacuous. These four
# inputs cover every non-matching mode and assert the gate rejects them.


@pytest.mark.parametrize(
    "wrong_mode",
    ["RANGE_BALANCE", "TREND_CONTINUATION", "TRANSITION", "UNKNOWN"],
)
def test_liquidity_sweep_rejects_non_volatile_sweep_zone_mode(wrong_mode: str) -> None:
    """Liquidity Sweep must NOT fire when structure_mode != VOLATILE_SWEEP_ZONE.

    All other gates pass; only structure_mode varies. Asserts the gate
    is wired and not vacuously satisfied by the fixture default.
    """
    state = StructureState(
        pair=_PAIR,
        timestamp=_LONDON_NOW.isoformat(),
        is_valid=True,
        htf_bias="BULLISH",
        local_bias="NEUTRAL",
        nearest_support=_level("LOW", 1.30000),
        nearest_resistance=_level("HIGH", 1.30200),
        liquidity_above=None,
        liquidity_below=_level("LOW", 1.29800),
        current_reaction="SUPPORT_SWEEP_RECLAIM",
        acceptance_state="INSIDE_RANGE",
        structure_mode=wrong_mode,  # type: ignore[arg-type]
        confidence=0.7,
        reason="test",
        levels=[],
        debug={},
    )
    sig = detect_liquidity_sweep(
        df_m5=_m5(ts=_LONDON_NOW),
        df_h1=pd.DataFrame(),
        regime_state=_state(),
        structure_state=state,
        pair=_PAIR,
        current_time=_LONDON_NOW,
    )
    assert sig is None, (
        f"Liquidity sweep fired on structure_mode={wrong_mode!r} — gate missing"
    )
