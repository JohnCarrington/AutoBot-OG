"""Tests for feed.rolling_buffer — RollingBuffer."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from feed.rolling_buffer import RollingBuffer
from feed.types import Candle


_TS0 = datetime(2026, 5, 15, 13, 0, tzinfo=timezone.utc)


def _candle(offset_min: int, pair: str = "GBPUSD") -> Candle:
    ts = _TS0 + timedelta(minutes=5 * offset_min)
    return Candle(
        pair=pair,
        close_time=ts,
        open=1.30000 + 0.0001 * offset_min,
        high=1.30100 + 0.0001 * offset_min,
        low=1.29950 + 0.0001 * offset_min,
        close=1.30050 + 0.0001 * offset_min,
        volume=200.0,
        source="LS_NATIVE_5M",
    )


def test_buffer_starts_empty() -> None:
    b = RollingBuffer("GBPUSD", capacity=5)
    assert len(b) == 0
    assert b.latest() is None
    assert b.snapshot() == []
    df = b.to_dataframe()
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.tz is not None and str(df.index.tz) == "UTC"


def test_push_appends_and_orders() -> None:
    b = RollingBuffer("GBPUSD", capacity=5)
    for i in range(3):
        b.push(_candle(i))
    snap = b.snapshot()
    assert [c.close_time for c in snap] == [
        _TS0 + timedelta(minutes=5 * i) for i in range(3)
    ]
    assert b.latest().close_time == _TS0 + timedelta(minutes=10)


def test_capacity_evicts_oldest() -> None:
    b = RollingBuffer("GBPUSD", capacity=3)
    for i in range(5):
        b.push(_candle(i))
    snap = b.snapshot()
    assert len(snap) == 3
    assert snap[0].close_time == _TS0 + timedelta(minutes=10)
    assert snap[-1].close_time == _TS0 + timedelta(minutes=20)


def test_push_same_timestamp_replaces_in_place() -> None:
    b = RollingBuffer("GBPUSD", capacity=5)
    b.push(_candle(0))
    updated = _candle(0)
    # Use object.__setattr__ — Candle is frozen, but pretending the LS
    # feed re-emitted with a different close is the realistic case.
    object.__setattr__(updated, "close", 1.40000)
    b.push(updated)
    assert len(b) == 1
    assert b.latest().close == 1.40000


def test_push_rejects_out_of_order() -> None:
    b = RollingBuffer("GBPUSD", capacity=5)
    b.push(_candle(2))
    with pytest.raises(ValueError):
        b.push(_candle(0))


def test_push_rejects_wrong_pair() -> None:
    b = RollingBuffer("GBPUSD", capacity=5)
    with pytest.raises(ValueError):
        b.push(_candle(0, pair="EURUSD"))


def test_bulk_append_skips_overlap_and_wrong_order() -> None:
    b = RollingBuffer("GBPUSD", capacity=10)
    b.push(_candle(0))
    b.push(_candle(1))
    # Bulk includes overlap on index 1 plus new bars 2, 3.
    added = b.bulk_append([_candle(1), _candle(2), _candle(3)])
    assert added == 2
    snap = b.snapshot()
    assert [c.close_time for c in snap] == [
        _TS0 + timedelta(minutes=5 * i) for i in range(4)
    ]


def test_to_dataframe_yields_utc_index_and_float_cols() -> None:
    b = RollingBuffer("GBPUSD", capacity=5)
    for i in range(3):
        b.push(_candle(i))
    df = b.to_dataframe()
    assert isinstance(df.index, pd.DatetimeIndex)
    assert str(df.index.tz) == "UTC"
    assert df["close"].dtype == "float64"
    assert df.index[0] == _TS0
    assert len(df) == 3


def test_clear_resets_buffer() -> None:
    b = RollingBuffer("GBPUSD", capacity=5)
    b.push(_candle(0))
    b.clear()
    assert len(b) == 0
    assert b.latest() is None


def test_threading_concurrent_pushes_are_consistent() -> None:
    b = RollingBuffer("GBPUSD", capacity=1000)
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def worker(start: int) -> None:
        try:
            barrier.wait()
            for i in range(start, start + 50):
                # Each thread targets its own disjoint timestamp range so
                # pushes are append-only from RollingBuffer's perspective
                # (sortable by start offset).
                b.push(_candle(i))
        except BaseException as exc:  # noqa: BLE001 — propagate to caller
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(s,)) for s in (0, 50, 100, 150)
    ]
    # Threads start in disjoint regions but timing-interleaved pushes
    # across the ordered space exercise the lock. We expect either:
    # - all 200 land in order, OR
    # - some pushes hit the out-of-order ValueError when an "older" worker
    #   loses the race. Both outcomes are acceptable: the buffer is
    #   never corrupt. We assert no internal exception, and order
    #   invariants hold on what got in.
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Some workers may have raised ValueError on out-of-order push due to
    # interleaving — that's expected. Filter them out and confirm the
    # buffer itself is consistent.
    non_value_errs = [e for e in errors if not isinstance(e, ValueError)]
    assert not non_value_errs, f"Unexpected errors: {non_value_errs}"
    snap = b.snapshot()
    # Ordering invariant: timestamps strictly increase.
    for prev, curr in zip(snap, snap[1:]):
        assert prev.close_time < curr.close_time
