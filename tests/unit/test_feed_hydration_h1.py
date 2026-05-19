"""Tests for the Phase B H1 hydration leg in :mod:`feed.hydration`.

The H1 leg piggy-backs on the existing :func:`hydrate_pair` by calling
it a second time per pair with ``resolution="HOUR"`` and a separate
``buffer`` / ``archive``. Coverage here mirrors the M5 cases in
``test_feed_hydration.py`` but at H1 cadence (3600s bars) and uses the
new ``h1_enabled=True`` knob on :func:`hydrate_pairs` so the tests are
independent of the default-off env flag.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from feed.archive import CandleArchive
from feed.constants import FEED_ARCHIVE_CSV_TEMPLATE_H1
from feed.hydration import (
    PairBundle,
    hydrate_pair,
    hydrate_pairs,
)
from feed.rolling_buffer import RollingBuffer
from feed.types import Candle


_NOW = datetime(2026, 5, 15, 13, 0, tzinfo=timezone.utc)


def _ig_h1_price(offset_h: int, base: float = 1.30000) -> dict:
    """One row in an IG H1 history /prices payload (bid+ask form).

    ``offset_h`` is the bar-open offset in hours from the oldest bar
    in the payload. close_time = open + 1h. We deliberately use
    ``snapshotTimeUTC`` (v2) — H1 mirrors the M5 wire shape.
    """
    open_t = _NOW - timedelta(hours=(72 - offset_h))
    return {
        "snapshotTime": open_t.strftime("%Y/%m/%d %H:%M:%S"),
        "snapshotTimeUTC": open_t.strftime("%Y-%m-%dT%H:%M:%S"),
        "openPrice":  {"bid": base + 0.0001 * offset_h - 0.00005, "ask": base + 0.0001 * offset_h + 0.00005},
        "highPrice":  {"bid": base + 0.0002 * offset_h - 0.00005, "ask": base + 0.0002 * offset_h + 0.00005},
        "lowPrice":   {"bid": base - 0.00005, "ask": base + 0.00005},
        "closePrice": {"bid": base + 0.00015 * offset_h - 0.00005, "ask": base + 0.00015 * offset_h + 0.00005},
        "lastTradedVolume": 1000 + offset_h,
    }


def _ig_h1_payload(n: int = 72) -> dict:
    return {"prices": [_ig_h1_price(i) for i in range(n)]}


def _h1_candle(pair: str, ts: datetime) -> Candle:
    return Candle(
        pair=pair,
        close_time=ts,
        open=1.30000,
        high=1.30200,
        low=1.29800,
        close=1.30100,
        volume=1500.0,
        source="REST",
    )


def _seed_h1_archive(
    tmp_path: Path, pair: str, bars: list[Candle]
) -> CandleArchive:
    arc = CandleArchive(
        pair, base_dir=tmp_path, template=FEED_ARCHIVE_CSV_TEMPLATE_H1,
    )
    arc.append_many(bars)
    return arc


# ---------------------------------------------------------------------------
# §5.1 #1 — cold-start rest_only
# ---------------------------------------------------------------------------


def test_h1_cold_start_rest_only(tmp_path: Path) -> None:
    """Empty archive + 72-bar IG payload → mode=rest_only, buffer full."""
    pair = "GBPUSD"
    archive = CandleArchive(
        pair, base_dir=tmp_path, template=FEED_ARCHIVE_CSV_TEMPLATE_H1,
    )
    buf = RollingBuffer(pair, capacity=72)
    rest_calls: list[tuple] = []

    def fetcher(epic, resolution, n):
        rest_calls.append((epic, resolution, n))
        return _ig_h1_payload(n=72)

    report = hydrate_pair(
        pair, "CS.D.GBPUSD.TODAY.IP",
        archive=archive, buffer=buf,
        fetcher=fetcher,
        now_utc=lambda: _NOW,
        backfill_bars=72,
        resolution="HOUR",
    )

    assert report.mode == "rest_only"
    assert report.cached_bars == 0
    assert report.rest_bars == 72
    assert report.final_buffer_size == 72
    # Confirm the resolution was passed through verbatim — IG accepts
    # "HOUR" (not "MINUTE_60") and a regression here would silently
    # drop us back to a 5-minute fetch.
    assert rest_calls == [("CS.D.GBPUSD.TODAY.IP", "HOUR", 72)]
    # And the archive on disk used the H1 template, not the M5 one.
    assert archive.path.name == "GBPUSD_1h.csv"
    # Gap A: every bar must be exactly 1 hour apart, and the first
    # bar's close_time must land on a clean hour boundary. This
    # explicitly proves the parser fix (close_time = open + 1h, not
    # +5min) — before that fix the bars would have been 1h apart but
    # offset by 5 minutes from the hour mark.
    snap = buf.snapshot()
    for a, b in zip(snap, snap[1:]):
        assert (b.close_time - a.close_time) == timedelta(hours=1)
    assert snap[0].close_time.minute == 0
    assert snap[0].close_time.second == 0


# ---------------------------------------------------------------------------
# §5.1 #2 — cache-only
# ---------------------------------------------------------------------------


def test_h1_cache_only(tmp_path: Path) -> None:
    """Pre-populated H1 archive with fresh tail → no fetcher call."""
    pair = "GBPUSD"
    # 72 H1 bars, newest within FRESHNESS_THRESHOLD_MIN (60min) of now.
    bars = [
        _h1_candle(pair, _NOW - timedelta(hours=(72 - i)))
        for i in range(72)
    ]
    archive = _seed_h1_archive(tmp_path, pair, bars)
    buf = RollingBuffer(pair, capacity=72)

    def fetcher(*_a, **_kw):
        raise AssertionError("fetcher must not be called when cache is fresh")

    report = hydrate_pair(
        pair, "CS.D.GBPUSD.TODAY.IP",
        archive=archive, buffer=buf,
        fetcher=fetcher,
        now_utc=lambda: _NOW,
        backfill_bars=72,
        resolution="HOUR",
    )

    assert report.mode == "cache_only"
    assert report.cached_bars == 72
    assert report.rest_bars == 0
    assert report.final_buffer_size == 72


# ---------------------------------------------------------------------------
# §5.1 #3 — cache + REST top-up
# ---------------------------------------------------------------------------


def test_h1_cache_plus_rest(tmp_path: Path) -> None:
    """Stale H1 tail by 5 hours → fetcher called once, top-up merged."""
    pair = "GBPUSD"
    # Cache ends 5 hours behind _NOW → stale.
    stale_ts = _NOW - timedelta(hours=5)
    bars = [
        _h1_candle(pair, stale_ts - timedelta(hours=(72 - i)))
        for i in range(72)
    ]
    archive = _seed_h1_archive(tmp_path, pair, bars)
    buf = RollingBuffer(pair, capacity=144)
    rest_calls: list[tuple] = []

    # REST payload must contain bars NEWER than the cache tail to
    # exercise a real top-up. Build 5 H1 bars whose opens span
    # (NOW - 5h) ... (NOW - 1h), so closes span (NOW - 4h) ... (NOW).
    def _topup_payload() -> dict:
        prices = []
        for i in range(5):
            open_t = _NOW - timedelta(hours=(5 - i))
            prices.append({
                "snapshotTimeUTC": open_t.strftime("%Y-%m-%dT%H:%M:%S"),
                "openPrice":  {"bid": 1.30000, "ask": 1.30002},
                "highPrice":  {"bid": 1.30100, "ask": 1.30102},
                "lowPrice":   {"bid": 1.29950, "ask": 1.29952},
                "closePrice": {"bid": 1.30050, "ask": 1.30052},
                "lastTradedVolume": 1000 + i,
            })
        return {"prices": prices}

    def fetcher(epic, resolution, n):
        rest_calls.append((epic, resolution, n))
        return _topup_payload()

    report = hydrate_pair(
        pair, "CS.D.GBPUSD.TODAY.IP",
        archive=archive, buffer=buf,
        fetcher=fetcher,
        now_utc=lambda: _NOW,
        backfill_bars=72,
        resolution="HOUR",
    )

    assert report.mode == "cache_plus_rest"
    # Gap B: REST fetcher called exactly once with the HOUR resolution.
    assert len(rest_calls) == 1
    assert rest_calls[0][1] == "HOUR"
    assert report.cached_bars == 72
    assert report.rest_bars == 5
    # Buffer must be strictly ordered after the join.
    snap = buf.snapshot()
    for a, b in zip(snap, snap[1:]):
        assert a.close_time < b.close_time
    # Gap B (cont.): the REST-fetched tail bars must land on hour
    # boundaries — proves the parser fix is applied for the
    # cache_plus_rest path, not only the rest_only path.
    rest_tail = [c for c in snap if c.close_time > stale_ts]
    assert len(rest_tail) == 5
    for c in rest_tail:
        assert c.close_time.minute == 0
        assert c.close_time.second == 0


# ---------------------------------------------------------------------------
# §5.1 #4 — REST failure with usable cache → degraded
# ---------------------------------------------------------------------------


def test_h1_rest_failure_degraded(tmp_path: Path) -> None:
    """REST raises + cache >= FEED_MIN_USABLE_BARS → cache_only_degraded."""
    pair = "GBPUSD"
    # 70 bars > FEED_MIN_USABLE_BARS=50; stale enough to trigger REST.
    bars = [
        _h1_candle(pair, _NOW - timedelta(hours=10) - timedelta(hours=(70 - i)))
        for i in range(70)
    ]
    archive = _seed_h1_archive(tmp_path, pair, bars)
    buf = RollingBuffer(pair, capacity=144)

    def fetcher(*_a, **_kw):
        raise RuntimeError("simulated IG H1 outage")

    report = hydrate_pair(
        pair, "CS.D.GBPUSD.TODAY.IP",
        archive=archive, buffer=buf,
        fetcher=fetcher,
        now_utc=lambda: _NOW,
        backfill_bars=72,
        resolution="HOUR",
    )

    assert report.mode == "cache_only_degraded"
    assert "simulated IG H1 outage" in (report.error or "")
    assert report.cached_bars == 70
    assert report.rest_bars == 0
    assert report.final_buffer_size == 70


# ---------------------------------------------------------------------------
# §5.1 #5 — short payload (< FEED_H1_MIN_USABLE_BARS) logs WARNING
# ---------------------------------------------------------------------------


def test_h1_short_payload_logs_warning(
    tmp_path: Path, caplog
) -> None:
    """IG returns 40 bars (< 60 minimum) → hydrate_pairs logs a WARNING.

    The buffer is still populated with what arrived — the classifier's
    own ``insufficient_indicator_data`` guard handles short series, and
    the bot loop's dispatcher will route to the M5-resample fallback
    until the H1 buffer warms up via live BAR_CLOSE updates.
    """
    pair = "GBPUSD"
    archive_m5 = CandleArchive(pair, base_dir=tmp_path)
    archive_h1 = CandleArchive(
        pair, base_dir=tmp_path, template=FEED_ARCHIVE_CSV_TEMPLATE_H1,
    )
    buf_m5 = RollingBuffer(pair, capacity=200)
    buf_h1 = RollingBuffer(pair, capacity=72)

    short_h1_payload = {"prices": [_ig_h1_price(i) for i in range(40)]}

    def fetcher(epic, resolution, n):
        if resolution == "HOUR":
            return short_h1_payload
        # M5 leg — return a small valid payload so the M5 call itself
        # succeeds (we're testing the H1 short-payload path).
        return {"prices": []}

    bundle = PairBundle(
        pair=pair,
        epic="CS.D.GBPUSD.TODAY.IP",
        archive=archive_m5,
        buffer=buf_m5,
        archive_h1=archive_h1,
        buffer_h1=buf_h1,
    )

    with caplog.at_level(logging.WARNING, logger="feed.hydration"):
        report = hydrate_pairs(
            [bundle],
            fetcher=fetcher,
            now_utc=lambda: _NOW,
            h1_enabled=True,
            h1_backfill_bars=72,
            h1_min_usable_bars=60,
        )

    # The aggregate report carries an H1 leg.
    assert len(report.per_pair_h1) == 1
    h1_report = report.per_pair_h1[0]
    assert h1_report.pair == pair
    assert h1_report.final_buffer_size == 40
    # Gap D: the leg must NOT be marked failed — a short payload is a
    # warning-and-degrade signal, not a hard failure. The bot loop's
    # dispatcher falls back to legacy M5-resample on its own.
    assert h1_report.mode == "rest_only"
    # And the WARNING fired with the diagnostic bar counts.
    warnings = [
        rec for rec in caplog.records
        if rec.levelno == logging.WARNING
        and "H1 buffer for GBPUSD" in rec.getMessage()
    ]
    assert warnings, "expected a WARNING about short H1 buffer"
    msg = warnings[0].getMessage()
    assert "40 bars" in msg
    assert "60 minimum" in msg


# ---------------------------------------------------------------------------
# Gap C — H1 leg failure must NOT poison the M5 leg
# ---------------------------------------------------------------------------


def test_h1_leg_raises_does_not_poison_m5(tmp_path: Path) -> None:
    """If the H1 REST fetch raises, the M5 leg still succeeds.

    This is the structural-isolation contract added to
    :func:`hydrate_pairs`: an H1-side failure produces an
    ``per_pair_h1`` entry with ``mode="failed"`` and an empty H1
    buffer, while ``per_pair`` for the same pair reports normal M5
    hydration. The bot loop's dispatcher falls back to the legacy
    M5-resample path silently.
    """
    pair = "GBPUSD"
    archive_m5 = CandleArchive(pair, base_dir=tmp_path)
    archive_h1 = CandleArchive(
        pair, base_dir=tmp_path, template=FEED_ARCHIVE_CSV_TEMPLATE_H1,
    )
    buf_m5 = RollingBuffer(pair, capacity=200)
    buf_h1 = RollingBuffer(pair, capacity=72)

    # M5 returns a real payload (100 bars), H1 raises hard. The two
    # legs share the fetcher; the resolution discriminator is the
    # only thing routing behaviour.
    def fetcher(epic, resolution, n):
        if resolution == "HOUR":
            raise RuntimeError("simulated IG H1 outage")
        # Build a fresh M5 payload anchored at _NOW so the
        # cache_only freshness check decides "rest_only" (empty
        # archive → REST cold start).
        return _ig_m5_payload(n)

    def _ig_m5_payload(n: int) -> dict:
        prices = []
        for i in range(n):
            open_t = _NOW - timedelta(minutes=5 * (n - i))
            prices.append({
                "snapshotTimeUTC": open_t.strftime("%Y-%m-%dT%H:%M:%S"),
                "openPrice":  {"bid": 1.30000, "ask": 1.30002},
                "highPrice":  {"bid": 1.30100, "ask": 1.30102},
                "lowPrice":   {"bid": 1.29950, "ask": 1.29952},
                "closePrice": {"bid": 1.30050, "ask": 1.30052},
                "lastTradedVolume": 100,
            })
        return {"prices": prices}

    bundle = PairBundle(
        pair=pair,
        epic="CS.D.GBPUSD.TODAY.IP",
        archive=archive_m5,
        buffer=buf_m5,
        archive_h1=archive_h1,
        buffer_h1=buf_h1,
    )

    report = hydrate_pairs(
        [bundle],
        fetcher=fetcher,
        now_utc=lambda: _NOW,
        h1_enabled=True,
        h1_backfill_bars=72,
        h1_min_usable_bars=60,
    )

    # The aggregate report must remain "ok" because the M5 leg
    # succeeded. The H1 leg's failure does NOT count against ok.
    assert report.ok is True
    assert len(report.per_pair) == 1
    m5_report = report.per_pair[0]
    assert m5_report.pair == pair
    assert m5_report.mode != "failed"
    assert m5_report.final_buffer_size > 0
    # H1 leg must surface its failure cleanly via per_pair_h1.
    assert len(report.per_pair_h1) == 1
    h1_report = report.per_pair_h1[0]
    assert h1_report.pair == pair
    assert h1_report.mode == "failed"
    assert h1_report.error and "simulated IG H1 outage" in h1_report.error
    # And the H1 buffer must not be populated — half-state would let
    # the loop dispatcher route to the buffer instead of the legacy
    # path and serve stale / partial bars.
    assert len(buf_h1) == 0
