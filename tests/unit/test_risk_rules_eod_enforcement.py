"""Tests for risk.rules.eod_enforcement (2c B-1 / 2d strip).

The overnight-hold carve-out is structure-driven: htf_bias must match
the position's direction (Mon-Thu only). 2d strip: the +1R PnL floor
that 2c added is gone — pnl level no longer affects the hold decision,
only htf_bias alignment + weekday do. The pre-EOD suppression rule has
no TREND carve-out; every candidate inside the buffer is rejected.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from day_type import DayType
from common import Direction

from risk.rules.eod_enforcement import (
    apply_eod_force_close,
    check_pre_eod_suppression,
)
from risk.types import CandidateTrade, OpenPosition


def _candidate(day_type: DayType = DayType.NORMAL) -> CandidateTrade:
    return CandidateTrade(
        pair="GBPUSD",
        intended_direction=Direction.BULLISH,
        intended_day_type=day_type,
        planned_entry_price=1.30,
        strategy_name="bb_bounce",
    )


def _pos(
    *,
    pid: str = "p1",
    pair: str = "GBPUSD",
    direction: Direction = Direction.BULLISH,
    pnl_r: float = 0.0,
    entry_time_utc: datetime | None = None,
) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        pair=pair,
        direction=direction,
        day_type_at_entry=DayType.NORMAL,
        strategy_name="ema_pullback",
        entry_price=1.30,
        current_price=1.31,
        entry_time_utc=(
            entry_time_utc
            if entry_time_utc is not None
            else datetime(2025, 5, 14, 12, 0, tzinfo=timezone.utc)
        ),
        current_pnl_r=pnl_r,
    )


# --- Pre-EOD suppression --------------------------------------------------


def test_pre_eod_allows_well_before_close() -> None:
    # 2025-05-14 Wed, 10:00 EDT = 14:00 UTC. ~7 hours to close.
    now = datetime(2025, 5, 14, 14, 0, tzinfo=timezone.utc)
    r = check_pre_eod_suppression(_candidate(DayType.NORMAL), now)
    assert r.allow is True


def test_pre_eod_rejects_normal_day_inside_buffer() -> None:
    # 2025-05-14 Wed, 16:45 EDT = 20:45 UTC. 15 min to 17:00 EDT close.
    now = datetime(2025, 5, 14, 20, 45, tzinfo=timezone.utc)
    r = check_pre_eod_suppression(_candidate(DayType.NORMAL), now)
    assert r.allow is False
    assert "pre_eod_suppression" in r.reason
    assert "NORMAL" in r.reason


def test_pre_eod_rejects_big_news_day_inside_buffer() -> None:
    """B-1: no day-type carve-out. BIG_NEWS_DAY is also rejected
    inside the buffer."""
    now = datetime(2025, 5, 14, 20, 45, tzinfo=timezone.utc)
    r = check_pre_eod_suppression(_candidate(DayType.BIG_NEWS_DAY), now)
    assert r.allow is False


def test_pre_eod_rejects_pre_big_news_inside_buffer() -> None:
    """B-1: PRE_BIG_NEWS also rejected — no day-type carve-out."""
    now = datetime(2025, 5, 14, 20, 45, tzinfo=timezone.utc)
    r = check_pre_eod_suppression(_candidate(DayType.PRE_BIG_NEWS), now)
    assert r.allow is False


def test_pre_eod_rejects_on_friday_inside_buffer() -> None:
    # 2025-05-16 is a Friday. 16:45 EDT = 20:45 UTC.
    now = datetime(2025, 5, 16, 20, 45, tzinfo=timezone.utc)
    r = check_pre_eod_suppression(_candidate(DayType.NORMAL), now)
    assert r.allow is False
    assert "pre_eod_suppression" in r.reason


def test_pre_eod_buffer_dst_winter() -> None:
    # 2025-01-15 Wed winter: NY close = 17:00 EST = 22:00 UTC.
    # 21:45 UTC = 16:45 EST → 15 min to close.
    now = datetime(2025, 1, 15, 21, 45, tzinfo=timezone.utc)
    r = check_pre_eod_suppression(_candidate(DayType.NORMAL), now)
    assert r.allow is False


# --- apply_eod_force_close: pre-close hour ---------------------------------


def test_force_close_returns_empty_before_ny_close() -> None:
    # 14:00 UTC = 10:00 EDT, well before 17:00 EDT close.
    now = datetime(2025, 5, 14, 14, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[_pos()],
        now_utc=now,
        htf_bias_for_pair={"GBPUSD": "BULLISH"},
    )
    assert orders == []


def test_force_close_empty_for_no_positions() -> None:
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[],
        now_utc=now,
        htf_bias_for_pair={},
    )
    assert orders == []


# --- apply_eod_force_close: at-or-after NY close ---------------------------


def test_position_survives_when_htf_bias_aligned_and_profitable_weekday() -> None:
    """B-1: a profitable BULLISH position whose htf_bias is still
    BULLISH survives the Mon-Thu NY close."""
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)  # Wed 17:00 EDT
    orders = apply_eod_force_close(
        positions=[_pos(pnl_r=1.5, direction=Direction.BULLISH)],
        now_utc=now,
        htf_bias_for_pair={"GBPUSD": "BULLISH"},
    )
    assert orders == []


def test_position_closed_when_htf_bias_flipped() -> None:
    """B-1: a profitable BULLISH position whose htf_bias has gone
    BEARISH must close."""
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[_pos(pnl_r=2.0, direction=Direction.BULLISH)],
        now_utc=now,
        htf_bias_for_pair={"GBPUSD": "BEARISH"},
    )
    assert len(orders) == 1
    assert "htf_bias_misaligned" in orders[0].reason


def test_position_closed_when_htf_bias_neutral() -> None:
    """NEUTRAL ≠ BULLISH or BEARISH; position closes."""
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[_pos(pnl_r=2.0, direction=Direction.BULLISH)],
        now_utc=now,
        htf_bias_for_pair={"GBPUSD": "NEUTRAL"},
    )
    assert len(orders) == 1
    assert "htf_bias_misaligned" in orders[0].reason


def test_position_closed_when_structure_unavailable() -> None:
    """B-1 fail-closed: missing htf_bias entry → close."""
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[_pos(pnl_r=2.0)],
        now_utc=now,
        htf_bias_for_pair={},  # nothing for the pair
    )
    assert len(orders) == 1
    assert "structure_unavailable" in orders[0].reason


def test_position_closed_when_htf_bias_explicit_none() -> None:
    """B-1: explicit None htf_bias is treated identically to absent."""
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[_pos(pnl_r=2.0)],
        now_utc=now,
        htf_bias_for_pair={"GBPUSD": None},
    )
    assert len(orders) == 1
    assert "structure_unavailable" in orders[0].reason


def test_position_holds_below_R_when_htf_bias_aligned() -> None:
    """2d: pnl_r no longer affects the hold decision. A BULLISH
    position with htf_bias BULLISH at +0.5R survives Mon-Thu."""
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[_pos(pnl_r=0.5, direction=Direction.BULLISH)],
        now_utc=now,
        htf_bias_for_pair={"GBPUSD": "BULLISH"},
    )
    assert orders == []


def test_position_holds_at_negative_pnl_when_htf_bias_aligned() -> None:
    """2d: even an underwater position holds overnight if htf_bias
    still agrees. The decision is purely structural — daily-DD is the
    rule that caps the bleed, not the EOD gate."""
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[_pos(pnl_r=-0.3, direction=Direction.BULLISH)],
        now_utc=now,
        htf_bias_for_pair={"GBPUSD": "BULLISH"},
    )
    assert orders == []


def test_bearish_position_survives_when_htf_bias_is_bearish() -> None:
    """B-1 mirror: BEARISH position needs BEARISH htf_bias to hold."""
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[_pos(pnl_r=1.5, direction=Direction.BEARISH)],
        now_utc=now,
        htf_bias_for_pair={"GBPUSD": "BEARISH"},
    )
    assert orders == []


def test_friday_closes_everything_even_when_htf_bias_aligned() -> None:
    # 2025-05-16 Fri 17:00 EDT = 21:00 UTC.
    now = datetime(2025, 5, 16, 21, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[_pos(pnl_r=3.0, direction=Direction.BULLISH)],
        now_utc=now,
        htf_bias_for_pair={"GBPUSD": "BULLISH"},
    )
    assert len(orders) == 1
    assert "friday_close" in orders[0].reason


def test_force_close_emits_one_order_per_misaligned_position() -> None:
    """Two positions, both with htf_bias misaligned → both close."""
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    p1 = _pos(pid="p1", pnl_r=0.5, direction=Direction.BULLISH)
    p2 = _pos(pid="p2", pnl_r=0.5, direction=Direction.BULLISH)
    orders = apply_eod_force_close(
        positions=[p1, p2],
        now_utc=now,
        htf_bias_for_pair={"GBPUSD": "BEARISH"},
    )
    pids = sorted(o.position_id for o in orders)
    assert pids == ["p1", "p2"]


def test_force_close_dst_winter_holds_when_htf_aligned() -> None:
    """2d: at 17:00 EST (winter close) a htf-aligned position holds
    regardless of pnl level."""
    # 2025-01-15 Wed at 22:00 UTC = 17:00 EST → at close.
    now = datetime(2025, 1, 15, 22, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[_pos(pnl_r=0.5, direction=Direction.BULLISH)],
        now_utc=now,
        htf_bias_for_pair={"GBPUSD": "BULLISH"},
    )
    assert orders == []


def test_mixed_position_basket() -> None:
    """A realistic 2d basket: htf-aligned positions HOLD (pnl level
    irrelevant); only flipped-bias and missing-structure positions
    close."""
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    positions = [
        _pos(pid="held_high", pair="GBPUSD", pnl_r=2.0,
             direction=Direction.BULLISH),
        _pos(pid="held_low", pair="EURUSD", pnl_r=0.2,
             direction=Direction.BULLISH),
        _pos(pid="flipped", pair="USDCAD", pnl_r=2.0,
             direction=Direction.BULLISH),
        _pos(pid="no_struct", pair="USDJPY", pnl_r=2.0,
             direction=Direction.BULLISH),
    ]
    orders = apply_eod_force_close(
        positions=positions,
        now_utc=now,
        htf_bias_for_pair={
            "GBPUSD": "BULLISH",
            "EURUSD": "BULLISH",
            "USDCAD": "BEARISH",
            # USDJPY missing → fail-closed.
        },
    )
    closed_ids = sorted(o.position_id for o in orders)
    assert closed_ids == ["flipped", "no_struct"]
