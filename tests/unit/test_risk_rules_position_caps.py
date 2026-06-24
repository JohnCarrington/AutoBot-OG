"""Tests for risk.rules.position_caps.

2c (B-4): per-regime cap → per-strategy cap. The rule now groups
positions by ``strategy_name``, not by ``day_type_at_entry``.
"""
from __future__ import annotations

from datetime import datetime, timezone

from day_type import DayType
from common import Direction

from risk.rules.position_caps import check_position_caps
from risk.types import CandidateTrade, OpenPosition


def _pos(
    *,
    pid: str = "p1",
    pair: str = "GBPUSD",
    direction: Direction = Direction.BULLISH,
    strategy_name: str = "ema_pullback",
) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        pair=pair,
        direction=direction,
        day_type_at_entry=DayType.NORMAL,
        strategy_name=strategy_name,
        entry_price=1.30,
        current_price=1.31,
        entry_time_utc=datetime(2025, 1, 1, tzinfo=timezone.utc),
        current_pnl_r=0.0,
    )


def _candidate(
    *, pair: str = "EURUSD", strategy_name: str = "bb_bounce"
) -> CandidateTrade:
    return CandidateTrade(
        pair=pair,
        intended_direction=Direction.BULLISH,
        intended_day_type=DayType.NORMAL,
        planned_entry_price=1.10,
        strategy_name=strategy_name,
    )


def test_allows_with_no_open_positions() -> None:
    result = check_position_caps(_candidate(), positions=[])
    assert result.allow is True
    assert result.rule == "position_caps"


def test_rejects_at_global_cap() -> None:
    # MAX_GLOBAL_POSITIONS = 2.
    positions = [
        _pos(pid="p1", pair="GBPUSD", strategy_name="ema_pullback"),
        _pos(pid="p2", pair="EURUSD", strategy_name="bb_bounce"),
    ]
    result = check_position_caps(
        _candidate(pair="USDJPY"), positions=positions
    )
    assert result.allow is False
    assert "global cap reached" in result.reason


def test_rejects_at_per_pair_cap() -> None:
    # Per-pair cap is 1: trying to add a second GBPUSD trade fails even with
    # only one position open globally.
    positions = [_pos(pid="p1", pair="GBPUSD", strategy_name="ema_pullback")]
    result = check_position_caps(
        _candidate(pair="GBPUSD", strategy_name="bb_bounce"),
        positions=positions,
    )
    assert result.allow is False
    assert "per-pair cap" in result.reason
    assert "GBPUSD" in result.reason


def test_rejects_at_per_strategy_cap() -> None:
    """B-4: two positions from the same strategy are disallowed even on
    different pairs."""
    positions = [_pos(pid="p1", pair="GBPUSD", strategy_name="bb_bounce")]
    result = check_position_caps(
        _candidate(pair="EURUSD", strategy_name="bb_bounce"),
        positions=positions,
    )
    assert result.allow is False
    assert "per-strategy cap" in result.reason
    assert "bb_bounce" in result.reason


def test_allows_different_strategy_on_different_pair() -> None:
    """B-4 positive case: distinct strategies on distinct pairs is fine."""
    positions = [_pos(pid="p1", pair="GBPUSD", strategy_name="ema_pullback")]
    result = check_position_caps(
        _candidate(pair="EURUSD", strategy_name="bb_bounce"),
        positions=positions,
    )
    assert result.allow is True


def test_global_cap_checked_before_per_pair() -> None:
    # 2 positions on different pairs; candidate is yet another pair.
    # Global cap is the binding rule.
    positions = [
        _pos(pid="p1", pair="GBPUSD", strategy_name="ema_pullback"),
        _pos(pid="p2", pair="EURUSD", strategy_name="bb_bounce"),
    ]
    result = check_position_caps(
        _candidate(pair="USDJPY", strategy_name="bb_bounce"),
        positions=positions,
    )
    assert result.allow is False
    assert "global cap" in result.reason


def test_position_id_does_not_affect_caps() -> None:
    # Caps are about COUNTS, not identifiers.
    positions = [_pos(pid="duplicate-id", pair="GBPUSD")]
    result = check_position_caps(
        _candidate(pair="EURUSD", strategy_name="bb_bounce"),
        positions=positions,
    )
    assert result.allow is True


def test_reason_quotes_actual_counts() -> None:
    positions = [_pos(pid="p1", pair="GBPUSD", strategy_name="bb_bounce")]
    result = check_position_caps(
        _candidate(pair="GBPUSD", strategy_name="ema_pullback"),
        positions=positions,
    )
    # Per-pair fires first because the strategy doesn't match.
    assert "1 open" in result.reason


def test_empty_positions_allows_any_candidate() -> None:
    for strategy in ("bb_bounce", "ema_pullback", "news", "structure_break"):
        result = check_position_caps(
            _candidate(pair="GBPUSD", strategy_name=strategy), positions=[]
        )
        assert result.allow is True
