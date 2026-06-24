"""Tests for the Structure-Break (ACCEPTANCE_BREAK) strategy — step 3.

Mirrors the fixture style of ``test_strategy_ema_pullback.py``: a
minimal M5 frame with ATR, an H1 macd-hist row, and a parametric
``StructureState`` builder.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from common import Direction
from day_type import DayType
from strategies.structure_break import detect_structure_break
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
    acceptance_state: str = "ACCEPTED_BELOW_SUPPORT",
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
        acceptance_state=acceptance_state,  # type: ignore[arg-type]
        structure_mode=mode,  # type: ignore[arg-type]
        confidence=0.7,
        reason="test",
        levels=[],
        debug={},
    )


# --- Happy paths ----------------------------------------------------------


def test_bearish_acceptance_break_emits_sell_on_big_news_day() -> None:
    """BEARISH htf_bias + SUPPORT_ACCEPTANCE_BREAK in TREND_CONTINUATION
    on a BIG_NEWS_DAY → SELL signal."""
    sig = detect_structure_break(
        df_m5=_m5(),
        df_h1=_h1(macd_hist=-0.10),
        day_type=DayType.BIG_NEWS_DAY,
        structure_state=_structure(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is not None
    assert sig.direction is Direction.BEARISH
    assert sig.day_type is DayType.BIG_NEWS_DAY
    assert sig.strategy_name == "structure_break"
    assert sig.suggested_tp_price is None
    # SL is on the wrong side of the entry — above for a SELL.
    assert sig.suggested_sl_price > sig.suggested_entry_price


def test_bullish_acceptance_break_emits_buy_on_pre_big_news() -> None:
    """BULLISH htf_bias + RESISTANCE_ACCEPTANCE_BREAK on PRE_BIG_NEWS
    → BUY signal carrying day_type=PRE_BIG_NEWS."""
    sig = detect_structure_break(
        df_m5=_m5(),
        df_h1=_h1(macd_hist=0.10),
        day_type=DayType.PRE_BIG_NEWS,
        structure_state=_structure(
            htf_bias="BULLISH",
            reaction="RESISTANCE_ACCEPTANCE_BREAK",
            acceptance_state="ACCEPTED_ABOVE_RESISTANCE",
        ),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is not None
    assert sig.direction is Direction.BULLISH
    assert sig.day_type is DayType.PRE_BIG_NEWS
    assert sig.strategy_name == "structure_break"
    assert sig.suggested_sl_price < sig.suggested_entry_price


# --- Negative cases -------------------------------------------------------


def test_failed_reclaim_reaction_returns_none() -> None:
    """B-5 split: structure_break ONLY consumes ACCEPTANCE_BREAK
    reactions. FAILED_RECLAIM is ema_pullback's domain."""
    sig = detect_structure_break(
        df_m5=_m5(),
        df_h1=_h1(),
        day_type=DayType.BIG_NEWS_DAY,
        structure_state=_structure(reaction="FAILED_RECLAIM_BELOW_SUPPORT"),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_rejection_reaction_returns_none() -> None:
    """A wick-rejection (no acceptance) → no structure_break signal."""
    sig = detect_structure_break(
        df_m5=_m5(),
        df_h1=_h1(),
        day_type=DayType.BIG_NEWS_DAY,
        structure_state=_structure(
            reaction="SUPPORT_REJECTION",
            acceptance_state="INSIDE_RANGE",
        ),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_htf_bias_mismatch_returns_none() -> None:
    """BEARISH htf_bias + RESISTANCE_ACCEPTANCE_BREAK is a directional
    contradiction (the level being broken is in the wrong direction
    for the trend)."""
    sig = detect_structure_break(
        df_m5=_m5(),
        df_h1=_h1(),
        day_type=DayType.BIG_NEWS_DAY,
        structure_state=_structure(
            htf_bias="BEARISH",
            reaction="RESISTANCE_ACCEPTANCE_BREAK",
            acceptance_state="ACCEPTED_ABOVE_RESISTANCE",
        ),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_neutral_htf_bias_returns_none() -> None:
    sig = detect_structure_break(
        df_m5=_m5(),
        df_h1=_h1(),
        day_type=DayType.BIG_NEWS_DAY,
        structure_state=_structure(htf_bias="NEUTRAL"),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_non_trend_continuation_mode_returns_none() -> None:
    """RANGE_BALANCE / TRANSITION etc. — acceptance can occur there but
    structure_break only fires on TREND_CONTINUATION (continuation
    semantics; mirror of ema_pullback's gate)."""
    sig = detect_structure_break(
        df_m5=_m5(),
        df_h1=_h1(),
        day_type=DayType.BIG_NEWS_DAY,
        structure_state=_structure(mode="RANGE_BALANCE"),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_invalid_structure_state_returns_none() -> None:
    sig = detect_structure_break(
        df_m5=_m5(),
        df_h1=_h1(),
        day_type=DayType.BIG_NEWS_DAY,
        structure_state=_structure(is_valid=False),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


def test_missing_anchor_level_returns_none() -> None:
    """SUPPORT_ACCEPTANCE_BREAK with nearest_support absent — defensive
    guard, the engine shouldn't emit this but if it does we fail safely."""
    sig = detect_structure_break(
        df_m5=_m5(),
        df_h1=_h1(),
        day_type=DayType.BIG_NEWS_DAY,
        structure_state=StructureState(
            pair=_PAIR,
            timestamp=_NOW.isoformat(),
            is_valid=True,
            htf_bias="BEARISH",
            local_bias="BEARISH",
            nearest_support=None,
            nearest_resistance=_level("HIGH", 1.30200),
            liquidity_above=None,
            liquidity_below=None,
            current_reaction="SUPPORT_ACCEPTANCE_BREAK",
            acceptance_state="ACCEPTED_BELOW_SUPPORT",
            structure_mode="TREND_CONTINUATION",
            confidence=0.7,
            reason="test",
            levels=[],
            debug={},
        ),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None


# --- day_type passthrough -------------------------------------------------


def test_day_type_passes_through_for_big_news_day() -> None:
    sig = detect_structure_break(
        df_m5=_m5(),
        df_h1=_h1(),
        day_type=DayType.BIG_NEWS_DAY,
        structure_state=_structure(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is not None
    assert sig.day_type is DayType.BIG_NEWS_DAY


def test_day_type_passes_through_for_pre_big_news() -> None:
    sig = detect_structure_break(
        df_m5=_m5(),
        df_h1=_h1(),
        day_type=DayType.PRE_BIG_NEWS,
        structure_state=_structure(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is not None
    assert sig.day_type is DayType.PRE_BIG_NEWS


# --- Edge cases (data hygiene) --------------------------------------------


def test_zero_atr_returns_none() -> None:
    """ATR == 0 can't compute a meaningful SL → no signal."""
    sig = detect_structure_break(
        df_m5=_m5(atr=0.0),
        df_h1=_h1(),
        day_type=DayType.BIG_NEWS_DAY,
        structure_state=_structure(),
        pair=_PAIR,
        current_time=_NOW,
    )
    assert sig is None
