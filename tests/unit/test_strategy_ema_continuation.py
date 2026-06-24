"""Tests for the Phase 11 EMA Continuation strategy rewrite."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from day_type import DayType
from regime.labels import Direction, RegimeLabel
from strategies.ema_continuation import detect_ema_continuation
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


def _h1(*, macd_hist: float = -0.10) -> pd.DataFrame:
    return pd.DataFrame([{"macd_hist_12_26_9": macd_hist}])


def _state(regime: str = "TREND", direction: str | None = "BEARISH") -> dict:
    return {
        "current_regime": regime,
        "current_direction": direction,
        "pending_regime": None,
        "m5_confirmation_count": 0,
        "last_regime_change_time": None,
        "reason": "test",
        "debug": {},
    }


def _level(side: str, price: float, score: float = 7.0) -> StructureLevel:
    return StructureLevel(
        pair=_PAIR,
        level_type=("SUPPORT" if side == "LOW" else "RESISTANCE"),
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


def _structure(
    *,
    htf_bias: str = "BEARISH",
    reaction: str = "SUPPORT_ACCEPTANCE_BREAK",
    mode: str = "TREND_CONTINUATION",
    nearest_support: StructureLevel | None = None,
    nearest_resistance: StructureLevel | None = None,
    is_valid: bool = True,
) -> StructureState:
    return StructureState(
        pair=_PAIR,
        timestamp=_NOW.isoformat(),
        is_valid=is_valid,
        htf_bias=htf_bias,  # type: ignore[arg-type]
        local_bias=htf_bias,  # type: ignore[arg-type]
        nearest_support=nearest_support or _level("LOW", 1.30000),
        nearest_resistance=nearest_resistance or _level("HIGH", 1.30200),
        liquidity_above=None,
        liquidity_below=None,
        current_reaction=reaction,  # type: ignore[arg-type]
        acceptance_state="ACCEPTED_BELOW_SUPPORT",
        structure_mode=mode,  # type: ignore[arg-type]
        confidence=0.7,
        reason="test",
        levels=[],
        debug={},
    )


def test_bearish_acceptance_break_emits_sell() -> None:
    sig = detect_ema_continuation(
        df_m5=_m5(),
        df_h1=_h1(macd_hist=-0.10),
        regime_state=_state(),
        structure_state=_structure(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is not None
    assert sig.direction is Direction.BEARISH
    assert sig.day_type is DayType.NORMAL
    assert sig.suggested_tp_price is None


def test_bullish_failed_reclaim_above_resistance_emits_buy() -> None:
    sig = detect_ema_continuation(
        df_m5=_m5(),
        df_h1=_h1(macd_hist=0.10),
        regime_state=_state(direction="BULLISH"),
        structure_state=_structure(
            htf_bias="BULLISH",
            reaction="FAILED_RECLAIM_ABOVE_RESISTANCE",
        ),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is not None
    assert sig.direction is Direction.BULLISH


def test_wrong_regime_returns_none() -> None:
    sig = detect_ema_continuation(
        df_m5=_m5(),
        df_h1=_h1(),
        regime_state=_state(regime="RANGE"),
        structure_state=_structure(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_non_trend_continuation_mode_returns_none() -> None:
    sig = detect_ema_continuation(
        df_m5=_m5(),
        df_h1=_h1(),
        regime_state=_state(),
        structure_state=_structure(mode="RANGE_BALANCE"),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_htf_neutral_returns_none() -> None:
    sig = detect_ema_continuation(
        df_m5=_m5(),
        df_h1=_h1(),
        regime_state=_state(),
        structure_state=_structure(htf_bias="NEUTRAL"),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_wrong_reaction_for_bias_returns_none() -> None:
    """BEARISH HTF + RESISTANCE_ACCEPTANCE_BREAK is a mismatch."""
    sig = detect_ema_continuation(
        df_m5=_m5(),
        df_h1=_h1(),
        regime_state=_state(),
        structure_state=_structure(
            htf_bias="BEARISH",
            reaction="RESISTANCE_ACCEPTANCE_BREAK",
        ),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_invalid_state_returns_none() -> None:
    sig = detect_ema_continuation(
        df_m5=_m5(),
        df_h1=_h1(),
        regime_state=_state(),
        structure_state=_structure(is_valid=False),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None
