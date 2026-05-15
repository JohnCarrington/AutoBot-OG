"""Tests for feed.hydration — cache-first decision tree."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

import pytest

from feed.archive import CandleArchive
from feed.hydration import (
    PairBundle,
    hydrate_pair,
    hydrate_pairs,
    parse_ig_history,
)
from feed.rolling_buffer import RollingBuffer
from feed.types import Candle


_NOW = datetime(2026, 5, 15, 13, 0, tzinfo=timezone.utc)


def _ig_price(offset_min: int, base: float = 1.30000) -> dict:
    """One row in an IG history /prices payload (bid+ask form).

    Uses the real IG v1 wire shape: ``snapshotTime`` with the slash
    date format and ``snapshotTimeUTC`` with the ISO ``T`` separator.
    """
    open_t = _NOW - timedelta(minutes=5 * (200 - offset_min))
    return {
        "snapshotTime": open_t.strftime("%Y/%m/%d %H:%M:%S"),
        "snapshotTimeUTC": open_t.strftime("%Y-%m-%dT%H:%M:%S"),
        "openPrice":  {"bid": base + 0.0001 * offset_min - 0.00005, "ask": base + 0.0001 * offset_min + 0.00005},
        "highPrice":  {"bid": base + 0.0002 * offset_min - 0.00005, "ask": base + 0.0002 * offset_min + 0.00005},
        "lowPrice":   {"bid": base - 0.00005, "ask": base + 0.00005},
        "closePrice": {"bid": base + 0.00015 * offset_min - 0.00005, "ask": base + 0.00015 * offset_min + 0.00005},
        "lastTradedVolume": 100 + offset_min,
    }


def _ig_payload(n: int = 100) -> dict:
    return {"prices": [_ig_price(i) for i in range(n)]}


def _cached_candle(pair: str, ts: datetime) -> Candle:
    return Candle(
        pair=pair,
        close_time=ts,
        open=1.30000,
        high=1.30100,
        low=1.29950,
        close=1.30050,
        volume=200.0,
        source="REST",
    )


def _seed_archive(tmp_path: Path, pair: str, bars: list[Candle]) -> CandleArchive:
    arc = CandleArchive(pair, base_dir=tmp_path)
    arc.append_many(bars)
    return arc


# ---------------------------------------------------------------------------
# parse_ig_history
# ---------------------------------------------------------------------------


def test_parse_ig_history_handles_v2_iso_timestamp() -> None:
    raw = _ig_payload(n=3)
    candles = parse_ig_history("GBPUSD", raw)
    assert len(candles) == 3
    assert all(c.source == "REST" for c in candles)
    # close_time is open + 5 min
    expected_first_open = _NOW - timedelta(minutes=5 * 200)
    assert candles[0].close_time == expected_first_open + timedelta(minutes=5)


def test_parse_ig_history_skips_malformed_rows() -> None:
    raw = {
        "prices": [
            _ig_price(0),
            {"snapshotTime": "garbage", "openPrice": {}},  # bad
            _ig_price(1),
        ]
    }
    candles = parse_ig_history("GBPUSD", raw)
    assert len(candles) == 2


def test_parse_ig_history_no_prices_returns_empty() -> None:
    assert parse_ig_history("GBPUSD", {}) == []
    assert parse_ig_history("GBPUSD", {"prices": "wat"}) == []


# ---------------------------------------------------------------------------
# hydrate_pair — three modes + failure
# ---------------------------------------------------------------------------


def test_hydrate_cache_only_fresh_and_full(tmp_path: Path) -> None:
    pair = "GBPUSD"
    bars = [
        _cached_candle(pair, _NOW - timedelta(minutes=5 * (100 - i)))
        for i in range(100)
    ]
    archive = _seed_archive(tmp_path, pair, bars)
    buf = RollingBuffer(pair, capacity=200)

    def fetcher(*_a, **_kw):
        raise AssertionError("fetcher must not be called in cache_only mode")

    report = hydrate_pair(
        pair, "CS.D.GBPUSD.TODAY.IP",
        archive=archive, buffer=buf,
        fetcher=fetcher,
        now_utc=lambda: _NOW,
    )
    assert report.mode == "cache_only"
    assert report.rest_bars == 0
    assert report.cached_bars == 100
    assert len(buf) == 100


def test_hydrate_cache_stale_triggers_rest_topup(tmp_path: Path) -> None:
    pair = "GBPUSD"
    # Cache exists but newest is 2h old → stale.
    stale_ts = _NOW - timedelta(hours=2)
    bars = [
        _cached_candle(pair, stale_ts - timedelta(minutes=5 * (100 - i)))
        for i in range(100)
    ]
    archive = _seed_archive(tmp_path, pair, bars)
    buf = RollingBuffer(pair, capacity=300)
    rest_calls: list[tuple] = []

    def fetcher(epic, resolution, n):
        rest_calls.append((epic, resolution, n))
        return _ig_payload(n=20)

    report = hydrate_pair(
        pair, "CS.D.GBPUSD.TODAY.IP",
        archive=archive, buffer=buf,
        fetcher=fetcher,
        now_utc=lambda: _NOW,
    )
    assert report.mode == "cache_plus_rest"
    assert rest_calls and rest_calls[0][1] == "MINUTE_5"
    assert report.cached_bars == 100
    # Buffer holds cached + (some) new bars, ordered.
    assert len(buf) >= 100
    snap = buf.snapshot()
    for a, b in zip(snap, snap[1:]):
        assert a.close_time < b.close_time


def test_hydrate_no_cache_does_full_rest(tmp_path: Path) -> None:
    pair = "GBPUSD"
    archive = CandleArchive(pair, base_dir=tmp_path)  # empty
    buf = RollingBuffer(pair, capacity=200)
    rest_calls: list[tuple] = []

    def fetcher(epic, resolution, n):
        rest_calls.append((epic, resolution, n))
        return _ig_payload(n=100)

    report = hydrate_pair(
        pair, "CS.D.GBPUSD.TODAY.IP",
        archive=archive, buffer=buf,
        fetcher=fetcher,
        now_utc=lambda: _NOW,
    )
    assert report.mode == "rest_only"
    assert report.cached_bars == 0
    assert report.rest_bars == 100
    # Archive now has 100 bars.
    assert len(archive.load()) == 100
    assert len(rest_calls) == 1


def test_hydrate_short_cache_under_backfill_triggers_rest(tmp_path: Path) -> None:
    pair = "GBPUSD"
    # 30 cached bars < FEED_BACKFILL_BARS=100 → still needs REST.
    bars = [
        _cached_candle(pair, _NOW - timedelta(minutes=5 * (30 - i)))
        for i in range(30)
    ]
    archive = _seed_archive(tmp_path, pair, bars)
    buf = RollingBuffer(pair, capacity=300)
    called = {"n": 0}

    def fetcher(*_a, **_kw):
        called["n"] += 1
        return _ig_payload(n=100)

    report = hydrate_pair(
        pair, "CS.D.GBPUSD.TODAY.IP",
        archive=archive, buffer=buf,
        fetcher=fetcher,
        now_utc=lambda: _NOW,
    )
    assert called["n"] == 1
    assert report.mode == "cache_plus_rest"
    assert report.cached_bars == 30


def test_hydrate_rest_failure_with_short_cache_returns_failed(tmp_path: Path) -> None:
    """REST fails + cache below MIN_USABLE_BARS → mode='failed', no buffer."""
    pair = "GBPUSD"
    bars = [
        _cached_candle(pair, _NOW - timedelta(hours=2) - timedelta(minutes=5 * (30 - i)))
        for i in range(30)  # < FEED_MIN_USABLE_BARS=50
    ]
    archive = _seed_archive(tmp_path, pair, bars)
    buf = RollingBuffer(pair, capacity=300)

    def fetcher(*_a, **_kw):
        raise RuntimeError("simulated IG outage")

    report = hydrate_pair(
        pair, "CS.D.GBPUSD.TODAY.IP",
        archive=archive, buffer=buf,
        fetcher=fetcher,
        now_utc=lambda: _NOW,
    )
    assert report.mode == "failed"
    assert "simulated IG outage" in (report.error or "")
    assert len(buf) == 0


def test_hydrate_rest_failure_with_usable_cache_degrades(tmp_path: Path) -> None:
    """REST fails + cache >= MIN_USABLE_BARS → mode='cache_only_degraded',
    buffer populated from cache, error string preserved for ops alerts."""
    pair = "GBPUSD"
    # 80 bars > FEED_MIN_USABLE_BARS=50, stale enough to need REST.
    bars = [
        _cached_candle(pair, _NOW - timedelta(hours=2) - timedelta(minutes=5 * (80 - i)))
        for i in range(80)
    ]
    archive = _seed_archive(tmp_path, pair, bars)
    buf = RollingBuffer(pair, capacity=300)

    def fetcher(*_a, **_kw):
        raise RuntimeError("simulated IG outage")

    report = hydrate_pair(
        pair, "CS.D.GBPUSD.TODAY.IP",
        archive=archive, buffer=buf,
        fetcher=fetcher,
        now_utc=lambda: _NOW,
    )
    assert report.mode == "cache_only_degraded"
    assert "simulated IG outage" in (report.error or "")
    assert report.cached_bars == 80
    assert report.rest_bars == 0
    # Buffer was actually populated from cache.
    assert len(buf) == 80
    assert report.final_buffer_size == 80


def test_hydrate_rest_failure_no_cache_returns_failed(tmp_path: Path) -> None:
    pair = "GBPUSD"
    archive = CandleArchive(pair, base_dir=tmp_path)
    buf = RollingBuffer(pair, capacity=200)

    def fetcher(*_a, **_kw):
        raise RuntimeError("connection refused")

    report = hydrate_pair(
        pair, "CS.D.GBPUSD.TODAY.IP",
        archive=archive, buffer=buf,
        fetcher=fetcher,
        now_utc=lambda: _NOW,
    )
    assert report.mode == "failed"
    assert report.rest_bars == 0


def test_hydrate_archive_oserror_leaves_buffer_empty_report_failed(
    tmp_path: Path,
) -> None:
    """M1: archive failure during hydration must NOT half-populate the buffer.

    The cleanup commit swapped the merge order to ``archive.append_many``
    *before* ``buffer.bulk_append``, so an OSError on the archive write
    propagates before any buffer mutation. The result is a clean
    ``mode="failed"`` report with ``final_buffer_size=0``; recovery on
    next startup sees a known state instead of a buffer-archive
    divergence.
    """
    pair = "GBPUSD"

    class _BrokenArchive(CandleArchive):
        def append_many(self, candles):  # noqa: D401 — test override
            raise OSError("simulated disk-full mid-write")

    archive = _BrokenArchive(pair, base_dir=tmp_path)
    buf = RollingBuffer(pair, capacity=200)
    bundles = [
        PairBundle(pair=pair, epic="CS.D.GBPUSD.TODAY.IP", archive=archive, buffer=buf)
    ]

    def fetcher(*_a, **_kw):
        return _ig_payload(n=100)  # cold REST returns 100 bars

    report = hydrate_pairs(
        bundles, fetcher=fetcher, now_utc=lambda: _NOW,
    )
    by_pair = {r.pair: r for r in report.per_pair}
    assert by_pair[pair].mode == "failed"
    assert by_pair[pair].final_buffer_size == 0
    assert "disk-full" in (by_pair[pair].error or "")
    # The critical post-condition: the buffer was NOT half-populated.
    assert len(buf) == 0


def test_hydrate_pair_does_not_re_archive_cached_bars(tmp_path: Path) -> None:
    pair = "GBPUSD"
    bars = [
        _cached_candle(pair, _NOW - timedelta(minutes=5 * (100 - i)))
        for i in range(100)
    ]
    archive = _seed_archive(tmp_path, pair, bars)
    initial_bytes = archive.path.stat().st_size
    buf = RollingBuffer(pair, capacity=200)
    hydrate_pair(
        pair, "CS.D.GBPUSD.TODAY.IP",
        archive=archive, buffer=buf,
        fetcher=lambda *a, **kw: _ig_payload(n=0),
        now_utc=lambda: _NOW,
    )
    # No REST bars to append → archive unchanged.
    assert archive.path.stat().st_size == initial_bytes


# ---------------------------------------------------------------------------
# hydrate_pairs — parallel orchestration
# ---------------------------------------------------------------------------


def test_hydrate_pairs_runs_each_pair_and_aggregates(tmp_path: Path) -> None:
    pairs = ["GBPUSD", "EURUSD"]
    bundles = []
    for p in pairs:
        archive = CandleArchive(p, base_dir=tmp_path)
        buffer = RollingBuffer(p, capacity=200)
        bundles.append(PairBundle(pair=p, epic=f"CS.D.{p}.TODAY.IP", archive=archive, buffer=buffer))

    def fetcher(epic, resolution, n):
        return _ig_payload(n=50)

    report = hydrate_pairs(
        bundles, fetcher=fetcher, now_utc=lambda: _NOW,
    )
    assert {r.pair for r in report.per_pair} == set(pairs)
    assert all(r.mode == "rest_only" for r in report.per_pair)
    assert report.ok is True


def test_hydrate_pairs_handles_per_pair_failure_isolated(tmp_path: Path) -> None:
    bundles = [
        PairBundle(
            pair="GBPUSD",
            epic="CS.D.GBPUSD.TODAY.IP",
            archive=CandleArchive("GBPUSD", base_dir=tmp_path),
            buffer=RollingBuffer("GBPUSD", capacity=200),
        ),
        PairBundle(
            pair="EURUSD",
            epic="CS.D.EURUSD.TODAY.IP",
            archive=CandleArchive("EURUSD", base_dir=tmp_path),
            buffer=RollingBuffer("EURUSD", capacity=200),
        ),
    ]

    def fetcher(epic, resolution, n):
        if "EURUSD" in epic:
            raise RuntimeError("simulated EURUSD outage")
        return _ig_payload(n=50)

    report = hydrate_pairs(
        bundles, fetcher=fetcher, now_utc=lambda: _NOW,
    )
    by_pair = {r.pair: r for r in report.per_pair}
    assert by_pair["GBPUSD"].mode == "rest_only"
    assert by_pair["EURUSD"].mode == "failed"
    assert report.ok is False
    assert "EURUSD" in report.failed_pairs


def test_hydrate_pairs_empty_bundles_returns_empty_report(tmp_path: Path) -> None:
    rep = hydrate_pairs([], fetcher=lambda *a, **k: {}, now_utc=lambda: _NOW)
    assert rep.per_pair == ()
    assert rep.ok is True


def test_hydrate_freshness_thresh_at_boundary(tmp_path: Path) -> None:
    pair = "GBPUSD"
    # Newest bar is exactly 60 minutes old → still fresh.
    boundary_ts = _NOW - timedelta(minutes=60)
    bars = [
        _cached_candle(pair, boundary_ts - timedelta(minutes=5 * (99 - i)))
        for i in range(100)
    ]
    archive = _seed_archive(tmp_path, pair, bars)
    buf = RollingBuffer(pair, capacity=200)

    def fetcher(*_a, **_kw):
        raise AssertionError("must not REST at boundary")

    report = hydrate_pair(
        pair, "CS.D.GBPUSD.TODAY.IP",
        archive=archive, buffer=buf,
        fetcher=fetcher,
        now_utc=lambda: _NOW,
    )
    assert report.mode == "cache_only"


# ---------------------------------------------------------------------------
# H3 — snapshotTime timezone handling
# ---------------------------------------------------------------------------


def test_parse_history_v2_utc_passthrough() -> None:
    """v2 ``snapshotTimeUTC`` is interpreted as UTC literally (no shift)."""
    raw = {
        "prices": [
            {
                "snapshotTimeUTC": "2026-05-15T13:00:00",
                "openPrice":  {"bid": 1.3, "ask": 1.3002},
                "highPrice":  {"bid": 1.3005, "ask": 1.3007},
                "lowPrice":   {"bid": 1.2995, "ask": 1.2997},
                "closePrice": {"bid": 1.3001, "ask": 1.3003},
                "lastTradedVolume": 100,
            }
        ]
    }
    candles = parse_ig_history("GBPUSD", raw)
    assert len(candles) == 1
    # close_time = open + 5min = 13:05 UTC.
    assert candles[0].close_time == datetime(
        2026, 5, 15, 13, 5, tzinfo=timezone.utc,
    )


def test_parse_history_v1_bst_shifts_minus_one_hour() -> None:
    """v1 ``snapshotTime`` in BST (June) shifts -1h to reach UTC."""
    raw = {
        "prices": [
            {
                # No snapshotTimeUTC → falls back to snapshotTime, which
                # IG returns in broker-local London. June 15 is in BST
                # (UTC+1), so 13:00 London -> 12:00 UTC.
                "snapshotTime": "2026/06/15 13:00:00",
                "openPrice":  {"bid": 1.3, "ask": 1.3002},
                "highPrice":  {"bid": 1.3005, "ask": 1.3007},
                "lowPrice":   {"bid": 1.2995, "ask": 1.2997},
                "closePrice": {"bid": 1.3001, "ask": 1.3003},
                "lastTradedVolume": 100,
            }
        ]
    }
    candles = parse_ig_history("GBPUSD", raw)
    assert len(candles) == 1
    # 13:00 BST -> 12:00 UTC; close_time = 12:05 UTC.
    assert candles[0].close_time == datetime(
        2026, 6, 15, 12, 5, tzinfo=timezone.utc,
    )


def test_parse_history_v1_gmt_no_shift() -> None:
    """v1 ``snapshotTime`` in GMT (January) needs no offset."""
    raw = {
        "prices": [
            {
                # January = GMT (UTC+0), so 13:00 London == 13:00 UTC.
                "snapshotTime": "2026/01/15 13:00:00",
                "openPrice":  {"bid": 1.3, "ask": 1.3002},
                "highPrice":  {"bid": 1.3005, "ask": 1.3007},
                "lowPrice":   {"bid": 1.2995, "ask": 1.2997},
                "closePrice": {"bid": 1.3001, "ask": 1.3003},
                "lastTradedVolume": 100,
            }
        ]
    }
    candles = parse_ig_history("GBPUSD", raw)
    assert len(candles) == 1
    assert candles[0].close_time == datetime(
        2026, 1, 15, 13, 5, tzinfo=timezone.utc,
    )


def test_parse_history_explicit_offset_overrides_flag() -> None:
    """An ISO string with explicit offset is respected verbatim."""
    raw = {
        "prices": [
            {
                # Explicit -05:00 offset — must override is_utc=True path.
                "snapshotTimeUTC": "2026-05-15T08:00:00-05:00",
                "openPrice":  {"bid": 1.3, "ask": 1.3002},
                "highPrice":  {"bid": 1.3005, "ask": 1.3007},
                "lowPrice":   {"bid": 1.2995, "ask": 1.2997},
                "closePrice": {"bid": 1.3001, "ask": 1.3003},
                "lastTradedVolume": 100,
            }
        ]
    }
    candles = parse_ig_history("GBPUSD", raw)
    assert len(candles) == 1
    # 08:00 -05:00 -> 13:00 UTC; close_time = 13:05 UTC.
    assert candles[0].close_time == datetime(
        2026, 5, 15, 13, 5, tzinfo=timezone.utc,
    )
