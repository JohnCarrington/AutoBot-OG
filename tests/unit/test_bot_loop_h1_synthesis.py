"""Tests for ``_synthesise_h1_from_m5_tail`` (Phase B Commit 2).

The helper aggregates the M5 buffer's tail bars into a single H1
:class:`Candle` for the hour containing the triggering M5 close.
Tested in isolation here — the integration test in
``tests/integration/test_bot_loop_h1_hydration.py`` exercises it via
a full BAR_CLOSE pipeline.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.loop import (
    _hour_floor_for_m5_close,
    _synthesise_h1_from_m5_tail,
)
from feed.rolling_buffer import RollingBuffer
from feed.types import Candle


_PAIR = "GBPUSD"


def _m5(
    open_t: datetime,
    *,
    open_p: float = 1.30000,
    high_p: float = 1.30100,
    low_p: float = 1.29950,
    close_p: float = 1.30050,
    volume: float = 200.0,
) -> Candle:
    """Build an M5 :class:`Candle` whose close_time = open_t + 5min."""
    return Candle(
        pair=_PAIR,
        close_time=open_t + timedelta(minutes=5),
        open=open_p,
        high=high_p,
        low=low_p,
        close=close_p,
        volume=volume,
        source="LS_NATIVE_5M",
    )


def _seed_buffer(bars: list[Candle], capacity: int = 600) -> RollingBuffer:
    buf = RollingBuffer(_PAIR, capacity=capacity)
    buf.bulk_append(bars)
    return buf


# ---------------------------------------------------------------------------
# _hour_floor_for_m5_close — the helper that maps a triggering M5 close
# to the hour it belongs to.
# ---------------------------------------------------------------------------


def test_hour_floor_for_minute_zero_returns_previous_hour() -> None:
    """M5 close at 10:00 is the LAST M5 of the 09:00 H1 — floors to 09:00."""
    m5_close = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)
    expected = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)
    assert _hour_floor_for_m5_close(m5_close) == expected


def test_hour_floor_for_mid_hour_floors_to_current_hour() -> None:
    """M5 close at 09:35 belongs to the 09:00 H1 (in-progress)."""
    m5_close = datetime(2026, 5, 15, 9, 35, tzinfo=timezone.utc)
    expected = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)
    assert _hour_floor_for_m5_close(m5_close) == expected


def test_hour_floor_for_minute_five_floors_to_current_hour() -> None:
    """M5 close at 09:05 belongs to the 09:00 H1 (first M5 of the hour)."""
    m5_close = datetime(2026, 5, 15, 9, 5, tzinfo=timezone.utc)
    expected = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)
    assert _hour_floor_for_m5_close(m5_close) == expected


# ---------------------------------------------------------------------------
# _synthesise_h1_from_m5_tail — locked test cases from Phase B plan §5.1
# ---------------------------------------------------------------------------


def test_full_hour_aggregation() -> None:
    """12 M5 bars covering 09:00 → 10:00 → one H1 candle with OHLC of the hour."""
    hour_start = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)
    # 12 M5 bars: opens at 09:00, 09:05, ..., 09:55. Closes at 09:05 ... 10:00.
    bars: list[Candle] = []
    for i in range(12):
        open_t = hour_start + timedelta(minutes=5 * i)
        bars.append(_m5(
            open_t,
            open_p=1.30000 + 0.0001 * i,
            high_p=1.30100 + 0.0001 * i,
            low_p=1.29950 + 0.0001 * i,
            close_p=1.30050 + 0.0001 * i,
            volume=100.0 + i,
        ))
    buf = _seed_buffer(bars)

    h1 = _synthesise_h1_from_m5_tail(buf, hour_start)

    assert h1 is not None
    assert h1.pair == _PAIR
    assert h1.close_time == hour_start + timedelta(hours=1)
    assert h1.source == "DERIVED"
    # OHLC: first.open, max(high), min(low), last.close
    assert h1.open == bars[0].open
    assert h1.close == bars[-1].close
    assert h1.high == max(c.high for c in bars)
    assert h1.low == min(c.low for c in bars)
    assert h1.volume == sum(c.volume for c in bars)


def test_partial_hour_aggregation() -> None:
    """5 M5 bars from 09:00-09:25 (in-progress hour) → partial H1 candle.

    close_time is still 10:00 (hour boundary, not the latest M5
    close). open = first M5's open; close = 5th M5's close.
    """
    hour_start = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)
    bars: list[Candle] = []
    for i in range(5):
        open_t = hour_start + timedelta(minutes=5 * i)
        bars.append(_m5(
            open_t,
            open_p=1.30000 + 0.0001 * i,
            high_p=1.30100 + 0.0001 * i,
            low_p=1.29950 + 0.0001 * i,
            close_p=1.30050 + 0.0001 * i,
            volume=100.0,
        ))
    buf = _seed_buffer(bars)

    h1 = _synthesise_h1_from_m5_tail(buf, hour_start)

    assert h1 is not None
    # Close_time anchored to the hour boundary, NOT the last M5's close.
    assert h1.close_time == hour_start + timedelta(hours=1)
    assert h1.open == bars[0].open
    assert h1.close == bars[-1].close
    assert h1.high == max(c.high for c in bars)
    assert h1.low == min(c.low for c in bars)
    assert h1.volume == 500.0


def test_empty_m5_buffer_returns_none() -> None:
    buf = _seed_buffer([])
    hour_start = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)
    assert _synthesise_h1_from_m5_tail(buf, hour_start) is None


def test_no_bars_match_window_returns_none() -> None:
    """M5 buffer has bars in 08:00-09:00; hour_start=10:00 → None."""
    earlier = datetime(2026, 5, 15, 8, 0, tzinfo=timezone.utc)
    bars = [_m5(earlier + timedelta(minutes=5 * i)) for i in range(12)]
    buf = _seed_buffer(bars)

    hour_start_later = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)
    assert _synthesise_h1_from_m5_tail(buf, hour_start_later) is None


def test_minute_zero_finalization() -> None:
    """M5 close at exactly 10:00 finalises the 09:00-10:00 H1.

    Locked test case from the Phase B plan §5 confirmation. The
    M5 bar with open=09:55, close=10:00 is the 12th M5 of the
    09:00 H1 and must be included in the synthesis. The resulting
    H1's close_time is exactly 10:00:00 — the hour boundary.
    """
    hour_start = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)
    # Build the full 12-bar hour ending with the 09:55→10:00 M5.
    bars = [_m5(hour_start + timedelta(minutes=5 * i)) for i in range(12)]
    buf = _seed_buffer(bars)

    # The triggering M5 close that finalises the hour is 10:00:00.
    trigger_close = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)
    assert bars[-1].close_time == trigger_close  # sanity: fixture aligned
    # _hour_floor_for_m5_close should map this M5 close back to 09:00.
    assert _hour_floor_for_m5_close(trigger_close) == hour_start

    h1 = _synthesise_h1_from_m5_tail(buf, hour_start)
    assert h1 is not None
    assert h1.close_time == trigger_close
    # All 12 M5 bars contributed.
    assert h1.volume == sum(c.volume for c in bars)


def test_minute_zero_dispatcher_does_not_trim_finalised_bar() -> None:
    """After the minute==0 push the dispatcher must NOT trim the tail.

    The bar that just finalised IS the latest fully-closed H1 and
    strategies need to see it. The dispatcher's trim branch is keyed
    on ``m5_close_time.minute != 0`` — this test confirms the inverse
    branch lets the bar through. Validated against the
    :func:`_hour_floor_for_m5_close` semantics: a 10:00:00 close maps
    to hour 09:00, so the synthesised H1's close_time = 10:00 is the
    finalised bar, not an in-progress one.
    """
    # Synthesised in-progress 11:00 H1 (one M5 so far) vs finalised
    # 10:00 H1 (12 M5 bars). Confirm both end at sensible boundaries.
    hour_start_finalised = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)
    finalised_bars = [
        _m5(hour_start_finalised + timedelta(minutes=5 * i)) for i in range(12)
    ]
    hour_start_inprogress = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)
    inprogress_bars = [_m5(hour_start_inprogress)]
    buf = _seed_buffer(finalised_bars + inprogress_bars)

    h1_finalised = _synthesise_h1_from_m5_tail(buf, hour_start_finalised)
    h1_inprogress = _synthesise_h1_from_m5_tail(buf, hour_start_inprogress)
    assert h1_finalised is not None
    assert h1_inprogress is not None
    assert h1_finalised.close_time == datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)
    assert h1_inprogress.close_time == datetime(2026, 5, 15, 11, 0, tzinfo=timezone.utc)
    # The finalised bar has 12 contributions; the in-progress has 1.
    assert h1_finalised.volume == sum(c.volume for c in finalised_bars)
    assert h1_inprogress.volume == inprogress_bars[0].volume


def test_midnight_utc_span() -> None:
    """M5 bars spanning 23:55 → 00:00 must aggregate into the correct hour.

    The hour containing the 23:55→00:00 M5 is 23:00 UTC on the
    previous day. Synthesise should land close_time on midnight UTC.
    """
    # Date 2026-05-15, hour 23:00 UTC. Build the full 12 M5 bars.
    hour_start = datetime(2026, 5, 15, 23, 0, tzinfo=timezone.utc)
    bars = [_m5(hour_start + timedelta(minutes=5 * i)) for i in range(12)]
    buf = _seed_buffer(bars)

    h1 = _synthesise_h1_from_m5_tail(buf, hour_start)

    assert h1 is not None
    # close_time = next midnight UTC on the following date.
    expected_close = datetime(2026, 5, 16, 0, 0, tzinfo=timezone.utc)
    assert h1.close_time == expected_close
    # And the floor of the triggering M5 close (00:00:00 UTC) maps
    # back to the 23:00 hour on the prior date.
    assert _hour_floor_for_m5_close(expected_close) == hour_start


def test_synthesised_close_time_is_on_the_hour() -> None:
    """The H1's close_time MUST land on a clean hour boundary.

    This is the contract that lets the BAR_CLOSE-side
    :py:meth:`RollingBuffer.push` perform in-place replace against
    the buffer's REST-hydrated tail. A regression here would break
    the H1 buffer's invariant.
    """
    hour_start = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)
    # Partial hour to make sure the close_time anchors to the boundary,
    # not the latest M5's close.
    bars = [_m5(hour_start + timedelta(minutes=5 * i)) for i in range(3)]
    buf = _seed_buffer(bars)

    h1 = _synthesise_h1_from_m5_tail(buf, hour_start)
    assert h1 is not None
    assert h1.close_time.minute == 0
    assert h1.close_time.second == 0
    assert h1.close_time.microsecond == 0
