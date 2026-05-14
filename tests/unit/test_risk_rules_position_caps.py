"""Tests for risk.rules.position_caps."""
from __future__ import annotations

from datetime import datetime, timezone

from regime.labels import Direction, RegimeLabel

from risk.rules.position_caps import check_position_caps
from risk.types import CandidateTrade, OpenPosition


def _pos(
    *,
    pid: str = "p1",
    pair: str = "GBPUSD",
    direction: Direction = Direction.BULLISH,
    regime: RegimeLabel = RegimeLabel.TREND,
) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        pair=pair,
        direction=direction,
        regime_at_entry=regime,
        entry_price=1.30,
        current_price=1.31,
        entry_time_utc=datetime(2025, 1, 1, tzinfo=timezone.utc),
        current_pnl_r=0.0,
    )


def _candidate(
    *, pair: str = "EURUSD", regime: RegimeLabel = RegimeLabel.RANGE
) -> CandidateTrade:
    return CandidateTrade(
        pair=pair,
        intended_direction=Direction.BULLISH,
        intended_regime=regime,
        planned_entry_price=1.10,
    )


def test_allows_with_no_open_positions() -> None:
    result = check_position_caps(_candidate(), positions=[])
    assert result.allow is True
    assert result.rule == "position_caps"


def test_rejects_at_global_cap() -> None:
    # MAX_GLOBAL_POSITIONS = 2.
    positions = [
        _pos(pid="p1", pair="GBPUSD", regime=RegimeLabel.TREND),
        _pos(pid="p2", pair="EURUSD", regime=RegimeLabel.RANGE),
    ]
    result = check_position_caps(_candidate(pair="USDJPY"), positions=positions)
    assert result.allow is False
    assert "global cap reached" in result.reason


def test_rejects_at_per_pair_cap() -> None:
    # Per-pair cap is 1: trying to add a second GBPUSD trade fails even with
    # only one position open globally.
    positions = [_pos(pid="p1", pair="GBPUSD", regime=RegimeLabel.TREND)]
    result = check_position_caps(
        _candidate(pair="GBPUSD", regime=RegimeLabel.RANGE),
        positions=positions,
    )
    assert result.allow is False
    assert "per-pair cap" in result.reason
    assert "GBPUSD" in result.reason


def test_rejects_at_per_regime_cap() -> None:
    # One TREND position on GBPUSD; candidate is a TREND on a different pair.
    positions = [_pos(pid="p1", pair="GBPUSD", regime=RegimeLabel.TREND)]
    result = check_position_caps(
        _candidate(pair="EURUSD", regime=RegimeLabel.TREND),
        positions=positions,
    )
    assert result.allow is False
    assert "per-regime cap" in result.reason
    assert "TREND" in result.reason


def test_allows_distinct_pair_and_regime_below_global_cap() -> None:
    # One TREND/GBPUSD open; candidate is RANGE/EURUSD. All caps fine.
    positions = [_pos(pid="p1", pair="GBPUSD", regime=RegimeLabel.TREND)]
    result = check_position_caps(
        _candidate(pair="EURUSD", regime=RegimeLabel.RANGE),
        positions=positions,
    )
    assert result.allow is True


def test_global_cap_checked_before_per_pair() -> None:
    # 2 positions on different pairs/regimes; candidate is yet another pair.
    # Global cap is the binding rule.
    positions = [
        _pos(pid="p1", pair="GBPUSD", regime=RegimeLabel.TREND),
        _pos(pid="p2", pair="EURUSD", regime=RegimeLabel.RANGE),
    ]
    result = check_position_caps(
        _candidate(pair="USDJPY", regime=RegimeLabel.VOLATILE),
        positions=positions,
    )
    assert result.allow is False
    assert "global cap" in result.reason


def test_volatile_position_blocks_volatile_candidate() -> None:
    positions = [
        _pos(pid="p1", pair="GBPUSD", regime=RegimeLabel.VOLATILE)
    ]
    result = check_position_caps(
        _candidate(pair="EURUSD", regime=RegimeLabel.VOLATILE),
        positions=positions,
    )
    assert result.allow is False
    assert "per-regime cap" in result.reason


def test_position_id_does_not_affect_caps() -> None:
    # Caps are about COUNTS, not identifiers.
    positions = [_pos(pid="duplicate-id", pair="GBPUSD")]
    result = check_position_caps(
        _candidate(pair="EURUSD", regime=RegimeLabel.RANGE),
        positions=positions,
    )
    assert result.allow is True


def test_reason_quotes_actual_counts() -> None:
    positions = [_pos(pid="p1", pair="GBPUSD", regime=RegimeLabel.TREND)]
    result = check_position_caps(
        _candidate(pair="GBPUSD", regime=RegimeLabel.RANGE),
        positions=positions,
    )
    assert "1 open" in result.reason


def test_empty_positions_allows_any_candidate() -> None:
    for regime in (
        RegimeLabel.TREND, RegimeLabel.RANGE,
        RegimeLabel.VOLATILE, RegimeLabel.TRANSITION,
    ):
        result = check_position_caps(
            _candidate(pair="GBPUSD", regime=regime), positions=[]
        )
        assert result.allow is True
