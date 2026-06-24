"""Tests for risk.rules.eod_enforcement (2c B-1 rewrite).

The overnight-hold carve-out is now structure-driven (htf_bias must
match the position's direction) rather than regime-driven. The
pre-EOD suppression rule no longer has a TREND carve-out — every
candidate inside the buffer is rejected.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from day_type import DayType
from regime.labels import Direction

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


def test_position_closed_when_below_overnight_R() -> None:
    """+0.5R is below the +1R floor → close even if htf_bias agrees."""
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[_pos(pnl_r=0.5, direction=Direction.BULLISH)],
        now_utc=now,
        htf_bias_for_pair={"GBPUSD": "BULLISH"},
    )
    assert len(orders) == 1
    assert "below_overnight_R" in orders[0].reason


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


def test_force_close_emits_one_order_per_position() -> None:
    """Two losing positions; both should get an order."""
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    p1 = _pos(pid="p1", pnl_r=0.5)
    p2 = _pos(pid="p2", pnl_r=0.5)
    orders = apply_eod_force_close(
        positions=[p1, p2],
        now_utc=now,
        htf_bias_for_pair={"GBPUSD": "BULLISH"},
    )
    pids = sorted(o.position_id for o in orders)
    assert pids == ["p1", "p2"]


def test_force_close_dst_winter() -> None:
    # 2025-01-15 Wed at 22:00 UTC = 17:00 EST → at close.
    now = datetime(2025, 1, 15, 22, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[_pos(pnl_r=0.5)],
        now_utc=now,
        htf_bias_for_pair={"GBPUSD": "BULLISH"},
    )
    assert len(orders) == 1


# --- H4 reason note: entry inside buffer ----------------------------------


def test_below_R_inside_buffer_flags_wasted_entry_in_reason() -> None:
    """H4: an entry made inside the pre-EOD buffer that hasn't
    reached +1R surfaces the inside-buffer note in the reason."""
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    entry = datetime(2025, 5, 14, 20, 45, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[_pos(pnl_r=0.3, entry_time_utc=entry)],
        now_utc=now,
        htf_bias_for_pair={"GBPUSD": "BULLISH"},
    )
    assert len(orders) == 1
    reason = orders[0].reason
    assert "below_overnight_R" in reason
    assert "inside" in reason
    assert "buffer" in reason
    assert "no path" in reason


def test_below_R_outside_buffer_omits_wasted_entry_note() -> None:
    """H4 negative: an entry made hours before close gets no buffer note."""
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    entry = datetime(2025, 5, 14, 12, 0, tzinfo=timezone.utc)
    orders = apply_eod_force_close(
        positions=[_pos(pnl_r=0.5, entry_time_utc=entry)],
        now_utc=now,
        htf_bias_for_pair={"GBPUSD": "BULLISH"},
    )
    assert len(orders) == 1
    reason = orders[0].reason
    assert "below_overnight_R" in reason
    assert "inside" not in reason
    assert "no path" not in reason


def test_mixed_position_basket() -> None:
    """A realistic basket: one held (htf-aligned + profitable), one closed
    (htf flipped), one closed (below R), one closed (structure missing
    on its pair)."""
    now = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    positions = [
        _pos(pid="held", pair="GBPUSD", pnl_r=2.0, direction=Direction.BULLISH),
        _pos(pid="flipped", pair="EURUSD", pnl_r=2.0,
             direction=Direction.BULLISH),
        _pos(pid="low_r", pair="GBPUSD", pnl_r=0.5,
             direction=Direction.BULLISH),
        _pos(pid="no_struct", pair="USDJPY", pnl_r=2.0,
             direction=Direction.BULLISH),
    ]
    orders = apply_eod_force_close(
        positions=positions,
        now_utc=now,
        htf_bias_for_pair={
            "GBPUSD": "BULLISH",
            "EURUSD": "BEARISH",
            # USDJPY missing → fail-closed.
        },
    )
    closed_ids = sorted(o.position_id for o in orders)
    assert closed_ids == ["flipped", "low_r", "no_struct"]
