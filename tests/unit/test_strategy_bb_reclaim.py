"""Tests for the Phase 11 BB Reclaim strategy rewrite.

Strategies now read :class:`StructureState` instead of doing 3-bar
pattern detection. Tests inject hand-built StructureState objects
and assert gate behaviour.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from regime.labels import Direction, RegimeLabel
from strategies.bb_reclaim import detect_bb_reclaim
from structure_engine import StructureLevel, StructureState


_PAIR = "GBPUSD"
_NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)


def _m5(close: float = 1.30050, atr: float = 0.0020) -> pd.DataFrame:
    idx = pd.DatetimeIndex(
        [_NOW - timedelta(minutes=5 * i) for i in range(2, -1, -1)]
    )
    return pd.DataFrame(
        [
            {"open": close, "high": close + 0.0005, "low": close - 0.0005,
             "close": close, "atr_14": atr},
            {"open": close, "high": close + 0.0005, "low": close - 0.0005,
             "close": close, "atr_14": atr},
            {"open": close, "high": close + 0.0005, "low": close - 0.0005,
             "close": close, "atr_14": atr},
        ],
        index=idx,
    )


def _h1(*, macd_hist: float = 0.10) -> pd.DataFrame:
    return pd.DataFrame([{"macd_hist_12_26_9": macd_hist}])


def _state(regime: str = "RANGE") -> dict:
    return {
        "current_regime": regime,
        "current_direction": None,
        "pending_regime": None,
        "m5_confirmation_count": 0,
        "last_regime_change_time": None,
        "reason": "test",
        "debug": {},
    }


def _level(
    *,
    side: str,
    price: float,
    score: float,
    level_type: str | None = None,
) -> StructureLevel:
    return StructureLevel(
        pair=_PAIR,
        level_type=(level_type or ("SUPPORT" if side == "LOW" else "RESISTANCE")),
        price=price,
        zone_low=price - 0.0004,
        zone_high=price + 0.0004,
        timeframe="H1",
        score=score,
        touch_count=2,
        last_touched_ts=None,
        source="swing_h1",
        debug={},
    )


_DEFAULT = object()


def _structure_state(
    *,
    is_valid: bool = True,
    mode: str = "RANGE_BALANCE",
    reaction: str = "SUPPORT_REJECTION",
    nearest_support=_DEFAULT,
    nearest_resistance=_DEFAULT,
) -> StructureState:
    support = (
        _level(side="LOW", price=1.30000, score=8.0)
        if nearest_support is _DEFAULT
        else nearest_support
    )
    resistance = (
        _level(side="HIGH", price=1.30200, score=7.0)
        if nearest_resistance is _DEFAULT
        else nearest_resistance
    )
    return StructureState(
        pair=_PAIR,
        timestamp=_NOW.isoformat(),
        is_valid=is_valid,
        htf_bias="NEUTRAL",
        local_bias="NEUTRAL",
        nearest_support=support,
        nearest_resistance=resistance,
        liquidity_above=None,
        liquidity_below=None,
        current_reaction=reaction,  # type: ignore[arg-type]
        acceptance_state="INSIDE_RANGE",
        structure_mode=mode,  # type: ignore[arg-type]
        confidence=0.7,
        reason="test",
        levels=[support, resistance],
        debug={},
    )


def test_support_rejection_emits_bullish_signal() -> None:
    sig = detect_bb_reclaim(
        df_m5=_m5(close=1.30050),
        df_h1=_h1(macd_hist=0.10),
        regime_state=_state(),
        structure_state=_structure_state(reaction="SUPPORT_REJECTION"),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is not None
    assert sig.direction is Direction.BULLISH
    assert sig.regime is RegimeLabel.RANGE
    assert sig.strategy_name == "bb_reclaim"
    assert sig.confidence_score == pytest.approx(0.85)  # MACD aligned


def test_resistance_rejection_emits_bearish_signal() -> None:
    sig = detect_bb_reclaim(
        df_m5=_m5(),
        df_h1=_h1(macd_hist=-0.10),
        regime_state=_state(),
        structure_state=_structure_state(reaction="RESISTANCE_REJECTION"),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is not None
    assert sig.direction is Direction.BEARISH


def test_wrong_regime_returns_none() -> None:
    sig = detect_bb_reclaim(
        df_m5=_m5(),
        df_h1=_h1(),
        regime_state=_state(regime="TREND"),
        structure_state=_structure_state(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_non_range_mode_returns_none() -> None:
    sig = detect_bb_reclaim(
        df_m5=_m5(),
        df_h1=_h1(),
        regime_state=_state(),
        structure_state=_structure_state(mode="TREND_CONTINUATION"),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_weak_support_score_blocks_long() -> None:
    weak = _level(side="LOW", price=1.30000, score=5.5)  # < 6.0 threshold
    sig = detect_bb_reclaim(
        df_m5=_m5(),
        df_h1=_h1(),
        regime_state=_state(),
        structure_state=_structure_state(
            reaction="SUPPORT_REJECTION", nearest_support=weak
        ),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_invalid_structure_state_returns_none() -> None:
    sig = detect_bb_reclaim(
        df_m5=_m5(),
        df_h1=_h1(),
        regime_state=_state(),
        structure_state=_structure_state(is_valid=False),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_no_resistance_target_leaves_tp_none() -> None:
    """One-sided support → TP is None (execution falls back to structure trail)."""
    sig = detect_bb_reclaim(
        df_m5=_m5(),
        df_h1=_h1(),
        regime_state=_state(),
        structure_state=_structure_state(
            reaction="SUPPORT_REJECTION", nearest_resistance=None
        ),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is not None
    assert sig.suggested_tp_price is None


def test_non_setup_reaction_returns_none() -> None:
    sig = detect_bb_reclaim(
        df_m5=_m5(),
        df_h1=_h1(),
        regime_state=_state(),
        structure_state=_structure_state(reaction="NONE"),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None
