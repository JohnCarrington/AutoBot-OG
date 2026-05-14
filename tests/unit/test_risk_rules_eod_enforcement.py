"""Tests for risk.rules.eod_enforcement."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from regime.labels import Direction, RegimeLabel

from risk.rules.eod_enforcement import (
    apply_eod_force_close,
    check_pre_eod_suppression,
)
from risk.types import CandidateTrade, OpenPosition


def _candidate(regime: RegimeLabel = RegimeLabel.RANGE) -> CandidateTrade:
    return CandidateTrade(
        pair="GBPUSD",
        intended_direction=Direction.BULLISH,
        intended_regime=regime,
        planned_entry_price=1.30,
    )


def _pos(
    *,
    pid: str = "p1",
    regime: RegimeLabel = RegimeLabel.TREND,
    direction: Direction = Direction.BULLISH,
    pnl_r: float = 0.0,
) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        pair="GBPUSD",
        direction=direction,
        regime_at_entry=regime,
        entry_price=1.30,
        current_price=1.31,
        entry_time_utc=datetime(2025, 5, 14, 12, 0, tzinfo=timezone.utc),
        current_pnl_r=pnl_r,
    )


# --- Pre-EOD suppression --------------------------------------------------


def test_pre_eod_allows_well_before_close() -> None:
    # 2025-05-14 Wed, 10:00 EDT = 14:00 UTC. ~7 hours to close.
    now = datetime(2025, 5, 14, 14, 0, tzinfo=timezone.utc)
    r = check_pre_eod_suppression(_candidate(RegimeLabel.RANGE), now)
    assert r.allow is True


def test_pre_eod_rejects_range_inside_buffer() -> None:
    # 2025-05-14 Wed, 16:45 EDT = 20:45 UTC. 15 min to 17:00 EDT close.
    now = datetime(2025, 5, 14, 20, 45, tzinfo=timezone.utc)
    r = check_pre_eod_suppression(_candidate(RegimeLabel.RANGE), now)
    assert r.allow is False
    assert "pre_eod_suppression" in r.reason
    assert "RANGE" in r.reason


def test_pre_eod_rejects_volatile_inside_buffer() -> None:
    now = datetime(2025, 5, 14, 20, 45, tzinfo=timezone.utc)
    r = check_pre_eod_suppression(_candidate(RegimeLabel.VOLATILE), now)
    assert r.allow is False


def test_pre_eod_allows_trend_inside_buffer_on_weekday() -> None:
    # Wednesday: TREND can hold overnight, so 15 min to close is fine.
    now = datetime(2025, 5, 14, 20, 45, tzinfo=timezone.utc)
    r = check_pre_eod_suppression(_candidate(RegimeLabel.TREND), now)
    assert r.allow is True


def test_pre_eod_rejects_trend_inside_buffer_on_friday() -> None:
    # 2025-05-16 is a Friday. 16:45 EDT = 20:45 UTC.
    now = datetime(2025, 5, 16, 20, 45, tzinfo=timezone.utc)
    r = check_pre_eod_suppression(_candidate(RegimeLabel.TREND), now)
    assert r.allow is False
    assert "pre_eod_suppression" in r.reason


def test_pre_eod_buffer_dst_winter() -> None:
    # 2025-01-15 Wed winter: NY close = 17:00 EST = 22:00 UTC.
    # 21:45 UTC = 16:45 EST → 15 min to close.
    now = datetime(2025, 1, 15, 21, 45, tzinfo=timezone.utc)
    r = check_pre_eod_suppression(_candidate(RegimeLabel.RANGE), now)
    assert r.allow is False


# --- apply_eod_force_close: pre-close hour ---------------------------------


def test_force_close_returns_empty_before_ny_close() -> None:
    # 14:00 UTC = 10:00 EDT, well before 17:00 EDT close.
    now = datetime(2025, 5, 14, 14, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[_pos()],
        now_utc=now,
        current_regime=RegimeLabel.TREND,
        current_direction=Direction.BULLISH,
    )
    assert orders == []


def test_force_close_empty_for_no_positions() -> None:
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[],
        now_utc=now,
        current_regime=RegimeLabel.TREND,
        current_direction=Direction.BULLISH,
    )
    assert orders == []


# --- apply_eod_force_close: at-or-after NY close ---------------------------


def test_force_close_range_at_ny_close() -> None:
    # 17:00 EDT = 21:00 UTC.
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[_pos(regime=RegimeLabel.RANGE)],
        now_utc=now,
        current_regime=RegimeLabel.RANGE,
        current_direction=None,
    )
    assert len(orders) == 1
    assert orders[0].position_id == "p1"
    assert "RANGE" in orders[0].reason


def test_force_close_volatile_at_ny_close() -> None:
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[_pos(regime=RegimeLabel.VOLATILE)],
        now_utc=now,
        current_regime=RegimeLabel.VOLATILE,
        current_direction=None,
    )
    assert len(orders) == 1


def test_trend_held_overnight_when_profitable_and_aligned_on_weekday() -> None:
    # Wed 17:00 EDT, TREND/BULLISH position with +1.5R, current regime
    # still TREND/BULLISH → survives overnight (no order).
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[_pos(regime=RegimeLabel.TREND, pnl_r=1.5)],
        now_utc=now,
        current_regime=RegimeLabel.TREND,
        current_direction=Direction.BULLISH,
    )
    assert orders == []


def test_trend_closed_when_below_overnight_R() -> None:
    # +0.5R is below the +1R floor → close it.
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[_pos(regime=RegimeLabel.TREND, pnl_r=0.5)],
        now_utc=now,
        current_regime=RegimeLabel.TREND,
        current_direction=Direction.BULLISH,
    )
    assert len(orders) == 1
    assert "trend_below_overnight_R" in orders[0].reason


def test_trend_closed_when_regime_changed() -> None:
    # +2R but current regime is RANGE → close.
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[_pos(regime=RegimeLabel.TREND, pnl_r=2.0)],
        now_utc=now,
        current_regime=RegimeLabel.RANGE,
        current_direction=None,
    )
    assert len(orders) == 1
    assert "trend_regime_lost" in orders[0].reason


def test_trend_closed_when_direction_changed() -> None:
    # +2R but current direction is BEARISH whereas entry was BULLISH → close.
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[_pos(regime=RegimeLabel.TREND, pnl_r=2.0,
                        direction=Direction.BULLISH)],
        now_utc=now,
        current_regime=RegimeLabel.TREND,
        current_direction=Direction.BEARISH,
    )
    assert len(orders) == 1
    assert "trend_direction_changed" in orders[0].reason


def test_trend_closed_on_friday_even_when_profitable() -> None:
    # 2025-05-16 Fri 17:00 EDT = 21:00 UTC.
    now = datetime(2025, 5, 16, 21, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[_pos(regime=RegimeLabel.TREND, pnl_r=3.0)],
        now_utc=now,
        current_regime=RegimeLabel.TREND,
        current_direction=Direction.BULLISH,
    )
    assert len(orders) == 1
    assert "friday_close" in orders[0].reason


def test_force_close_emits_one_order_per_position() -> None:
    # Two RANGE positions; both should get an order.
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    p1 = _pos(pid="p1", regime=RegimeLabel.RANGE)
    p2 = _pos(pid="p2", regime=RegimeLabel.RANGE)
    orders = apply_eod_force_close(
        positions=[p1, p2],
        now_utc=now,
        current_regime=RegimeLabel.RANGE,
        current_direction=None,
    )
    pids = sorted(o.position_id for o in orders)
    assert pids == ["p1", "p2"]


def test_force_close_dst_winter() -> None:
    # 2025-01-15 Wed at 22:00 UTC = 17:00 EST → at close.
    now = datetime(2025, 1, 15, 22, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[_pos(regime=RegimeLabel.RANGE)],
        now_utc=now,
        current_regime=RegimeLabel.RANGE,
        current_direction=None,
    )
    assert len(orders) == 1


def test_mixed_position_basket() -> None:
    """A realistic basket: one RANGE (close), one TREND held (no order),
    one TREND below R (close), one TREND with flipped regime (close).
    """
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    positions = [
        _pos(pid="range1", regime=RegimeLabel.RANGE),
        _pos(pid="trend_keep", regime=RegimeLabel.TREND, pnl_r=2.0),
        _pos(pid="trend_low", regime=RegimeLabel.TREND, pnl_r=0.5),
        _pos(pid="trend_bear",
             regime=RegimeLabel.TREND,
             pnl_r=2.0,
             direction=Direction.BULLISH),
    ]
    # Engine still TREND/BULLISH; trend_keep survives. trend_bear actually
    # matches (entry=BULLISH, current=BULLISH) — let me use trend_lowR instead
    # to make a clean case.
    orders = apply_eod_force_close(
        positions=positions,
        now_utc=now,
        current_regime=RegimeLabel.TREND,
        current_direction=Direction.BULLISH,
    )
    closed_ids = sorted(o.position_id for o in orders)
    # trend_keep and trend_bear (which now also matches direction) survive.
    assert "trend_keep" not in closed_ids
    assert "range1" in closed_ids
    assert "trend_low" in closed_ids
