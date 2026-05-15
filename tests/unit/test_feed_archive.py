"""Tests for feed.archive — CandleArchive."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from feed.archive import CandleArchive
from feed.constants import FEED_ARCHIVE_COLUMNS
from feed.types import Candle


_TS0 = datetime(2026, 5, 15, 13, 0, tzinfo=timezone.utc)


def _candle(offset_min: int = 0, pair: str = "GBPUSD", **overrides) -> Candle:
    defaults = dict(
        pair=pair,
        close_time=_TS0 + timedelta(minutes=5 * offset_min),
        open=1.30000 + 0.0001 * offset_min,
        high=1.30100 + 0.0001 * offset_min,
        low=1.29950 + 0.0001 * offset_min,
        close=1.30050 + 0.0001 * offset_min,
        volume=200.0,
        source="LS_NATIVE_5M",
    )
    defaults.update(overrides)
    return Candle(**defaults)


def test_append_creates_file_with_header(tmp_path: Path) -> None:
    arc = CandleArchive("GBPUSD", base_dir=tmp_path)
    assert arc.append(_candle()) is True
    content = arc.path.read_text().splitlines()
    assert content[0].split(",") == list(FEED_ARCHIVE_COLUMNS)
    assert len(content) == 2  # header + 1 row


def test_append_then_load_round_trip(tmp_path: Path) -> None:
    arc = CandleArchive("GBPUSD", base_dir=tmp_path)
    for i in range(3):
        arc.append(_candle(i))
    loaded = arc.load()
    assert [c.close_time for c in loaded] == [
        _TS0 + timedelta(minutes=5 * i) for i in range(3)
    ]
    # Source flips to REST on load (archive is treated as historical).
    assert all(c.source == "REST" for c in loaded)


def test_append_dedups_duplicate_timestamp(tmp_path: Path) -> None:
    arc = CandleArchive("GBPUSD", base_dir=tmp_path)
    assert arc.append(_candle(0)) is True
    assert arc.append(_candle(0)) is False  # same close_time
    rows = arc.path.read_text().splitlines()
    assert len(rows) == 2  # header + 1 row


def test_append_dedups_older_timestamp(tmp_path: Path) -> None:
    arc = CandleArchive("GBPUSD", base_dir=tmp_path)
    arc.append(_candle(5))
    assert arc.append(_candle(3)) is False  # older
    assert arc.last_close_time_ms is not None


def test_append_many_returns_written_count(tmp_path: Path) -> None:
    arc = CandleArchive("GBPUSD", base_dir=tmp_path)
    n = arc.append_many([_candle(0), _candle(1), _candle(1), _candle(2)])
    assert n == 3  # one dup of offset=1 dropped


def test_append_rejects_wrong_pair(tmp_path: Path) -> None:
    arc = CandleArchive("GBPUSD", base_dir=tmp_path)
    with pytest.raises(ValueError):
        arc.append(_candle(pair="EURUSD"))


def test_archive_creates_missing_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "nested" / "deep" / "path"
    arc = CandleArchive("GBPUSD", base_dir=nested)
    arc.append(_candle())
    assert arc.path.exists()


def test_load_empty_when_file_missing(tmp_path: Path) -> None:
    arc = CandleArchive("GBPUSD", base_dir=tmp_path)
    assert arc.load() == []


def test_load_handles_corrupt_trailing_line(tmp_path: Path) -> None:
    arc = CandleArchive("GBPUSD", base_dir=tmp_path)
    arc.append(_candle(0))
    arc.append(_candle(1))
    # Append a partially-written final row (simulates SIGKILL mid-write).
    with arc.path.open("a") as fh:
        fh.write("2026-05-15T13:10:00+00:00,GARBAGE")
    loaded = arc.load()
    # Two good rows recovered; corrupt tail skipped.
    assert len(loaded) == 2


def test_load_skips_bad_rows_silently(tmp_path: Path) -> None:
    arc = CandleArchive("GBPUSD", base_dir=tmp_path)
    arc.append(_candle(0))
    # Mid-file row with non-numeric price
    with arc.path.open("a") as fh:
        fh.write(
            "2026-05-15T13:05:00+00:00,1747315500000,oops,1.302,1.300,1.301,200\n"
        )
    arc.append(_candle(2))
    loaded = arc.load()
    # 2 good rows, the bad row in the middle is skipped.
    assert len(loaded) == 2


def test_load_with_limit(tmp_path: Path) -> None:
    arc = CandleArchive("GBPUSD", base_dir=tmp_path)
    for i in range(10):
        arc.append(_candle(i))
    loaded = arc.load(limit=3)
    assert len(loaded) == 3
    # The most recent N
    assert loaded[0].close_time == _TS0 + timedelta(minutes=5 * 7)
    assert loaded[-1].close_time == _TS0 + timedelta(minutes=5 * 9)


def test_last_ts_seeded_from_existing_file(tmp_path: Path) -> None:
    arc1 = CandleArchive("GBPUSD", base_dir=tmp_path)
    arc1.append(_candle(0))
    arc1.append(_candle(1))
    # New instance should pick up the existing tail timestamp.
    arc2 = CandleArchive("GBPUSD", base_dir=tmp_path)
    assert arc2.last_close_time_ms is not None
    # First write attempt for a duplicate stays a no-op without re-loading.
    assert arc2.append(_candle(1)) is False
    assert arc2.append(_candle(2)) is True
