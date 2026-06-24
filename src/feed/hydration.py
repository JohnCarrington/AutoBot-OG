"""Cold-start hydration of the rolling buffer (Phase 7).

The hydration path runs once per pair before the live Lightstreamer
subscription opens. Its job is to populate
:class:`feed.rolling_buffer.RollingBuffer` with enough recent M5 bars
for every indicator to seed (``FEED_BACKFILL_BARS = 100``), preferring
the on-disk cache over the REST API to conserve the broker's daily
allowance.

Decision tree (per pair)::

    cached = archive.load(limit=BACKFILL_BARS)
    newest = cached[-1].close_time if cached else None

    if len(cached) >= BACKFILL_BARS and is_fresh(newest):
        # Cache-only: no REST call.
    elif cached:
        # Cache-stale or short: REST top-up only the missing tail.
        # On REST failure with len(cached) >= FEED_MIN_USABLE_BARS,
        # fall back to cache-only-degraded (H1 fix); otherwise mark
        # the pair as failed.
    else:
        # No cache: REST fetch BACKFILL_BARS.

    # Archive write precedes buffer mutation so an OSError leaves the
    # report ``failed`` cleanly without a populated buffer (M1 fix).
    archive.append_many(rest_topup)   # new bars only
    buffer.bulk_append(cached + rest_topup)

Reasoning:

- The cache is the source of truth for "stuff we've already seen".
  Re-fetching the whole window on every start is the simplest design
  but burns 400 REST points across 4 pairs every restart — that's
  4% of IG's ~10k daily allowance evaporating on the bot bouncing.
- The freshness check (``FRESHNESS_THRESHOLD_MIN = 60``) covers the
  common case: the bot was running, was restarted, and the cache
  is up-to-date. The session resumes without a single REST call.
- When the cache is short or stale, we still only fetch enough to
  fill the gap. We *never* fetch ahead of the newest cached bar's
  close_time — that's where the live LS subscription takes over.

REST budget guardrails:

- ``FEED_BACKFILL_BARS = 100`` is the per-pair fetch ceiling. The
  IG allowance is ~10k points/day; 400 on cold start is 4%.
- After cold-start, hydration is **never** called again from a
  steady-state code path. Gap-fill on reconnect is handled in
  :py:meth:`feed.feed_manager.FeedManager._handle_reconnect` and
  bounded to ``FEED_GAP_FILL_WINDOW_MIN``.

If tempted to add a REST poll loop, **don't**. Use Lightstreamer.

Parallelism:

- :func:`hydrate_pairs` fans out across pairs in a thread pool. The
  IG REST library is blocking (it sits on ``requests``); a small
  thread pool means a 4-pair hydration finishes in roughly the
  latency of one call rather than four.
- The pool size matches the pair count — there's no benefit to
  larger pools because each pair calls REST at most once.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional
from zoneinfo import ZoneInfo

from .archive import CandleArchive
from .constants import (
    FEED_BACKFILL_BARS,
    FEED_FRESHNESS_THRESHOLD_MIN,
    FEED_H1_BACKFILL_BARS,
    FEED_H1_HYDRATION_ENABLED,
    FEED_H1_MIN_USABLE_BARS,
    FEED_MIN_USABLE_BARS,
)
from .rolling_buffer import RollingBuffer
from .types import Candle

logger = logging.getLogger(__name__)


# IG's REST `/prices` endpoint returns ``snapshotTimeUTC`` (v2) as a
# UTC string with no offset (e.g. ``"2026-05-15T13:00:00"``). The
# fallback ``snapshotTime`` (v1) is broker-LOCAL time without an
# offset — historically London (Europe/London), which alternates
# between GMT (UTC+0) and BST (UTC+1). See H3 in the Phase 7
# adversarial review for the verification chain.
_LONDON = ZoneInfo("Europe/London")


# ---------------------------------------------------------------------------
# Report dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PairHydrationReport:
    """Per-pair outcome of a hydration call.

    ``mode`` is one of:

    - ``"cache_only"`` — cache was fresh and full; no REST call made.
    - ``"cache_plus_rest"`` — cache plus REST top-up succeeded.
    - ``"rest_only"`` — no cache (cold start), REST fetch succeeded.
    - ``"cache_only_degraded"`` — REST top-up failed but the cache held
      at least :data:`feed.constants.FEED_MIN_USABLE_BARS` bars, so the
      buffer was seeded from cache alone. ``error`` is set to the REST
      failure message; ``ok`` on the aggregate report stays ``True``
      because the pair has usable data. Callers that care about ops
      visibility (telegram alerts, dashboards) should inspect ``error``
      and the mode tag.
    - ``"failed"`` — neither cache (or under-threshold) nor REST
      produced bars. The pair has no data; strategies must not trade.
    """

    pair: str
    mode: str
    cached_bars: int
    rest_bars: int
    final_buffer_size: int
    newest_close_time: Optional[datetime]
    error: Optional[str] = None


@dataclass(frozen=True)
class HydrationReport:
    """Aggregate report for a hydration call across all pairs.

    ``ok`` is ``True`` iff every pair has *some* usable data — i.e.
    no pair is in ``mode="failed"``. ``cache_only_degraded`` pairs
    are considered OK for trading purposes (the cache seeded the
    buffer) but carry an ``error`` string for ops visibility.

    ``per_pair_h1`` carries the optional H1 hydration leg added in
    Phase B. It is an empty tuple when the H1 flag is off, so
    existing callers that only inspect ``per_pair`` see no change.
    The aggregate ``ok``/``failed_pairs``/``degraded_pairs``
    properties intentionally ignore the H1 leg — a partial H1
    failure must NOT block bot startup; the bot loop's dispatcher
    falls back to the legacy M5-resample H1 path automatically.
    """

    started_at_utc: datetime
    finished_at_utc: datetime
    per_pair: tuple[PairHydrationReport, ...]
    per_pair_h1: tuple[PairHydrationReport, ...] = ()

    @property
    def ok(self) -> bool:
        return all(p.mode != "failed" for p in self.per_pair)

    @property
    def failed_pairs(self) -> tuple[str, ...]:
        return tuple(p.pair for p in self.per_pair if p.mode == "failed")

    @property
    def degraded_pairs(self) -> tuple[str, ...]:
        """Pairs that fell back to cache after a REST failure (ops signal)."""
        return tuple(
            p.pair for p in self.per_pair if p.mode == "cache_only_degraded"
        )


# ---------------------------------------------------------------------------
# REST adapter — converts a raw IG history payload into Candles
# ---------------------------------------------------------------------------


HistoryFetcher = Callable[[str, str, int], Mapping[str, Any]]
"""Signature for the REST fetcher injected into hydrate_pair.

``(epic, resolution, num_points) -> raw IG response dict``. In
production this is a thin wrapper around
:py:func:`feed.ig_rest.history.fetch_historical_prices`; tests pass
an in-memory stub.
"""


# IG resolution string → bar duration. Used by parse_ig_history to
# compute close_time from the snapshot (bar-open) timestamp. The
# default M5 entry preserves the legacy behaviour (close = open + 5m);
# the H1 entry is what unlocks Phase B Commit 2 — REST H1 close_times
# must land on hour boundaries to match what _synthesise_h1_from_m5_tail
# produces from M5 BAR_CLOSE bars.
_RESOLUTION_TO_DURATION: dict[str, timedelta] = {
    "MINUTE_5":  timedelta(minutes=5),
    "MINUTE_15": timedelta(minutes=15),
    "MINUTE_30": timedelta(minutes=30),
    "HOUR":      timedelta(hours=1),
    "HOUR_2":    timedelta(hours=2),
    "HOUR_4":    timedelta(hours=4),
    "DAY":       timedelta(days=1),
    "WEEK":      timedelta(weeks=1),
}


def _resolution_to_duration(resolution: str) -> timedelta:
    """Map an IG resolution string to the bar duration.

    Raises :class:`ValueError` on unknown resolutions so a typo in a
    caller fails loudly at parse time rather than silently producing
    wrong-cadence candles. Add new mappings here when a new
    timeframe is genuinely needed — there's no fallback by design.
    """
    try:
        return _RESOLUTION_TO_DURATION[resolution]
    except KeyError as exc:
        raise ValueError(
            f"parse_ig_history: unknown IG resolution {resolution!r}; "
            f"expected one of {sorted(_RESOLUTION_TO_DURATION)}"
        ) from exc


def parse_ig_history(
    pair: str,
    raw: Mapping[str, Any],
    *,
    resolution: str = "MINUTE_5",
) -> list[Candle]:
    """Parse a raw IG `/prices` response into :class:`Candle` list.

    Each entry in ``raw["prices"]`` has the shape::

        {
            "snapshotTime": "2026/05/15 13:00:00",
            "snapshotTimeUTC": "2026-05-15T13:00:00",   # v2 only
            "openPrice":  {"bid": ..., "ask": ..., "lastTraded": ...},
            "highPrice":  {...},
            "lowPrice":   {...},
            "closePrice": {...},
            "lastTradedVolume": 252,
        }

    We prefer ``snapshotTimeUTC`` when present (v2 API, literally UTC)
    and fall back to ``snapshotTime`` (v1 API, broker-local Europe/London
    — see :func:`_parse_ig_timestamp` and H3 in the adversarial review
    for the BST/GMT handling). Malformed entries are skipped with a
    warning — we never fail hydration on a single bad bar.

    ``resolution`` selects the bar duration added to the snapshot
    (bar-open) timestamp to compute ``close_time``. Defaults to
    ``"MINUTE_5"`` so legacy M5 callers are byte-identical; Phase B
    H1 hydration passes ``"HOUR"`` to land close_times on hour
    boundaries. An unknown resolution raises :class:`ValueError`
    immediately — silent fall-back to M5 would corrupt the H1
    buffer in ways the in-place-replace mechanic can't recover from.
    """
    bar_duration = _resolution_to_duration(resolution)
    out: list[Candle] = []
    prices = raw.get("prices") if isinstance(raw, Mapping) else None
    if not isinstance(prices, list):
        logger.warning(
            "parse_ig_history(%s): no 'prices' list in payload (got %s)",
            pair,
            type(prices).__name__,
        )
        return out
    for idx, entry in enumerate(prices):
        try:
            candle = _parse_history_entry(pair, entry, bar_duration)
        except Exception as exc:
            logger.warning(
                "parse_ig_history(%s): row %d skipped: %s",
                pair,
                idx,
                exc,
            )
            continue
        if candle is not None:
            out.append(candle)
    out.sort(key=lambda c: c.close_time)
    return out


def _parse_history_entry(
    pair: str, entry: Mapping[str, Any], bar_duration: timedelta,
) -> Optional[Candle]:
    if not isinstance(entry, Mapping):
        return None
    # Prefer v2's snapshotTimeUTC (literally UTC, no conversion needed)
    # over v1's snapshotTime (broker-local, London tz). The is_utc flag
    # tells _parse_ig_timestamp how to interpret a naive datetime.
    snap_utc = entry.get("snapshotTimeUTC")
    if snap_utc:
        open_time = _parse_ig_timestamp(str(snap_utc), is_utc=True)
    else:
        snap_local = entry.get("snapshotTime")
        if not snap_local:
            return None
        open_time = _parse_ig_timestamp(str(snap_local), is_utc=False)
    if open_time is None:
        return None
    close_time = open_time + bar_duration

    def _mid(slot: str) -> Optional[float]:
        section = entry.get(slot)
        if not isinstance(section, Mapping):
            return None
        bid = section.get("bid")
        ask = section.get("ask")
        try:
            bid_f = float(bid)
            ask_f = float(ask)
        except (TypeError, ValueError):
            return None
        return (bid_f + ask_f) / 2.0

    open_mid = _mid("openPrice")
    high_mid = _mid("highPrice")
    low_mid = _mid("lowPrice")
    close_mid = _mid("closePrice")
    if None in (open_mid, high_mid, low_mid, close_mid):
        return None

    vol_raw = entry.get("lastTradedVolume", 0)
    try:
        vol = float(vol_raw) if vol_raw is not None else 0.0
    except (TypeError, ValueError):
        vol = 0.0

    return Candle(
        pair=pair,
        close_time=close_time,
        open=open_mid,
        high=high_mid,
        low=low_mid,
        close=close_mid,
        volume=vol,
        source="REST",
    )


def _parse_ig_timestamp(s: str, *, is_utc: bool) -> Optional[datetime]:
    """Parse one of IG's snapshot-time formats into a tz-aware UTC datetime.

    ``is_utc`` says whether a naive (no-offset) result should be
    interpreted as UTC (v2 ``snapshotTimeUTC``) or as Europe/London
    local time (v1 ``snapshotTime``, broker-local). An explicit
    offset in the string always overrides this flag.

    Returns ``None`` if the string cannot be parsed at all.
    """
    s = s.strip()
    candidates = (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y:%m:%d-%H:%M:%S",
    )
    parsed: Optional[datetime] = None
    for fmt in candidates:
        try:
            parsed = datetime.strptime(s, fmt)
        except ValueError:
            continue
        break
    if parsed is None:
        # ISO with explicit Z / offset.
        try:
            parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return _coerce_to_utc(parsed, is_utc=is_utc)


def _coerce_to_utc(dt: datetime, *, is_utc: bool) -> datetime:
    """Return ``dt`` as a UTC-tz-aware datetime, applying London tz if needed.

    DST caveat (A2, Phase 7 follow-up): a naive timestamp inside the
    autumn fall-back ambiguous hour (e.g. 2026-10-25 01:30 in London,
    which exists *twice* — once as BST and once as GMT) is interpreted
    with ``fold=0`` semantics, i.e. the **first occurrence** (BST).
    In practice the FX market closes for the weekend around the UK DST
    transitions so the affected window is empty; the safer path is for
    callers to consume ``snapshotTimeUTC`` (v2 REST) or strings with an
    explicit offset, both of which bypass the London-tz branch here.
    """
    if dt.tzinfo is not None:
        # An explicit offset wins regardless of ``is_utc`` — IG is
        # already telling us the zone.
        return dt.astimezone(timezone.utc)
    if is_utc:
        return dt.replace(tzinfo=timezone.utc)
    # Naive + broker-local: localise to Europe/London (handles BST/GMT
    # transitions automatically), then convert to UTC.
    return dt.replace(tzinfo=_LONDON).astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Per-pair hydration
# ---------------------------------------------------------------------------


def hydrate_pair(
    pair: str,
    epic: str,
    *,
    archive: CandleArchive,
    buffer: RollingBuffer,
    fetcher: HistoryFetcher,
    now_utc: Optional[Callable[[], datetime]] = None,
    backfill_bars: int = FEED_BACKFILL_BARS,
    freshness_min: int = FEED_FRESHNESS_THRESHOLD_MIN,
    resolution: str = "MINUTE_5",
) -> PairHydrationReport:
    """Hydrate one pair's buffer from cache + (maybe) REST.

    Parameters
    ----------
    pair, epic : str
        The pair (cache key, used inside :class:`Candle`) and its IG
        epic (passed to the REST fetcher).
    archive : CandleArchive
        Pre-constructed archive for this pair.
    buffer : RollingBuffer
        Pre-constructed buffer for this pair. Must be empty when
        called.
    fetcher : HistoryFetcher
        Test seam. In production, the FeedManager passes a closure
        over :py:func:`feed.ig_rest.history.fetch_historical_prices`.
    now_utc : callable, optional
        Test seam for the freshness clock. Defaults to
        ``datetime.now(timezone.utc)``.
    backfill_bars, freshness_min : int
        Test seams for the hydration thresholds; defaults come from
        :py:mod:`feed.constants`.
    resolution : str
        IG resolution string passed to ``fetcher``. Default
        ``"MINUTE_5"`` (the locked v1 timeframe).

    Returns
    -------
    PairHydrationReport
        Includes the decision mode (cache_only / cache_plus_rest /
        rest_only / failed) and the final buffer size.
    """
    clock = now_utc or (lambda: datetime.now(timezone.utc))

    # --- 1. Load cache ------------------------------------------------
    try:
        cached = archive.load(limit=backfill_bars)
    except Exception as exc:
        logger.error(
            "hydrate_pair(%s): archive.load() raised %s — falling back to "
            "REST-only", pair, exc,
        )
        cached = []

    # --- 2. Decide ----------------------------------------------------
    fresh_enough = (
        len(cached) >= backfill_bars
        and cached
        and _is_fresh(cached[-1].close_time, clock(), freshness_min)
    )

    rest_bars: list[Candle] = []
    mode: str

    degraded_error: Optional[str] = None

    if fresh_enough:
        mode = "cache_only"
    elif cached:
        # Top up the tail since the newest cached bar. Bound the request
        # by ``backfill_bars`` so a multi-hour outage doesn't quietly
        # balloon into a thousand-bar fetch.
        try:
            raw = fetcher(epic, resolution, backfill_bars)
            fresh_candles = parse_ig_history(pair, raw, resolution=resolution)
            newest_cached_ts = cached[-1].close_time
            rest_bars = [
                c for c in fresh_candles if c.close_time > newest_cached_ts
            ]
            mode = "cache_plus_rest"
        except Exception as exc:
            # H1 (adversarial review 2026-05-15): the cache is the whole
            # point of cache-first hydration. A transient REST failure
            # must not collapse to ``mode="failed"`` when we hold enough
            # cached bars to start the strategy layer. We fall through
            # to the buffer-population step with ``rest_bars=[]`` and
            # tag the mode ``cache_only_degraded`` so the caller can
            # surface an ops alert without halting trading.
            if len(cached) >= FEED_MIN_USABLE_BARS:
                logger.warning(
                    "hydrate_pair(%s): REST top-up failed: %s — falling "
                    "back to cache (%d bars, threshold %d)",
                    pair, exc, len(cached), FEED_MIN_USABLE_BARS,
                )
                mode = "cache_only_degraded"
                degraded_error = f"REST top-up failed: {exc}"
                rest_bars = []
            else:
                logger.error(
                    "hydrate_pair(%s): REST top-up failed: %s and cache "
                    "has only %d bars (< %d threshold) — marking failed",
                    pair, exc, len(cached), FEED_MIN_USABLE_BARS,
                )
                return PairHydrationReport(
                    pair=pair,
                    mode="failed",
                    cached_bars=len(cached),
                    rest_bars=0,
                    final_buffer_size=0,
                    newest_close_time=None,
                    error=f"REST top-up failed: {exc}",
                )
    else:
        try:
            raw = fetcher(epic, resolution, backfill_bars)
            rest_bars = parse_ig_history(pair, raw, resolution=resolution)
            mode = "rest_only"
        except Exception as exc:
            logger.error(
                "hydrate_pair(%s): cold REST fetch failed: %s",
                pair, exc,
            )
            return PairHydrationReport(
                pair=pair,
                mode="failed",
                cached_bars=0,
                rest_bars=0,
                final_buffer_size=0,
                newest_close_time=None,
                error=f"cold REST fetch failed: {exc}",
            )

    # --- 3. Merge into archive + buffer ------------------------------
    # Buffer wants strictly increasing order. Cached is already sorted
    # by load(); rest_bars is sorted by parse_ig_history.
    #
    # M1 (Phase 7 adversarial review): archive is written FIRST so that
    # an OSError (disk full, perms) on the archive write doesn't leave
    # the buffer populated with bars that aren't durable. With this
    # order an archive failure propagates up before the buffer mutates,
    # and ``hydrate_pairs`` reports the pair as ``failed`` cleanly.
    #
    # H1 forming-bar guard (HOUR resolution only). IG's HOUR history
    # endpoint includes the in-progress hour as its newest row;
    # parse_ig_history labels it close_time = snapshotTime + 1h, i.e.
    # up to ~1h in the future. Left in, that bar becomes the H1
    # RollingBuffer's tail and blocks _maybe_push_synthesised_h1's
    # in-place replace — every live M5 close then logs an out-of-order
    # push rejection. Drop any REST bar whose close_time is past now
    # (+60s tolerance). Covers rest_only and cache_plus_rest (both
    # populate rest_bars); filtering here also keeps the forming bar
    # out of the archive, so a later cache_only boot stays clean. M5
    # is untouched — its forming bar is handled by the live feed.
    if resolution == "HOUR" and rest_bars:
        forming_cutoff = clock() + timedelta(seconds=60)
        kept = [c for c in rest_bars if c.close_time <= forming_cutoff]
        dropped = len(rest_bars) - len(kept)
        if dropped:
            logger.info(
                "hydrate_pair(%s): dropped %d forming H1 bar(s) "
                "(close_time past %s)",
                pair, dropped, forming_cutoff.isoformat(),
            )
        rest_bars = kept
    combined: list[Candle] = list(cached)
    if rest_bars:
        combined.extend(rest_bars)
    if combined:
        # Only NEW bars are appended to the archive — the cache entries
        # are already on disk. The archive's _last_ts guard makes this
        # idempotent but the explicit slice keeps I/O bounded.
        archive.append_many(rest_bars)
        buffer.bulk_append(combined)

    newest = combined[-1].close_time if combined else None
    return PairHydrationReport(
        pair=pair,
        mode=mode,
        cached_bars=len(cached),
        rest_bars=len(rest_bars),
        final_buffer_size=len(buffer),
        newest_close_time=newest,
        error=degraded_error,
    )


def _is_fresh(newest: datetime, now: datetime, freshness_min: int) -> bool:
    return (now - newest) <= timedelta(minutes=freshness_min)


# ---------------------------------------------------------------------------
# Multi-pair hydration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PairBundle:
    """Inputs required to hydrate one pair, bundled for the executor.

    ``archive_h1`` / ``buffer_h1`` are populated only when
    :data:`feed.constants.FEED_H1_HYDRATION_ENABLED` is on. When
    either is ``None`` the H1 hydration leg is skipped silently for
    that pair, preserving the legacy M5-only behaviour.
    """

    pair: str
    epic: str
    archive: CandleArchive
    buffer: RollingBuffer
    archive_h1: Optional[CandleArchive] = None
    buffer_h1: Optional[RollingBuffer] = None


def hydrate_pairs(
    bundles: list[PairBundle],
    *,
    fetcher: HistoryFetcher,
    now_utc: Optional[Callable[[], datetime]] = None,
    backfill_bars: int = FEED_BACKFILL_BARS,
    freshness_min: int = FEED_FRESHNESS_THRESHOLD_MIN,
    resolution: str = "MINUTE_5",
    h1_enabled: bool = FEED_H1_HYDRATION_ENABLED,
    h1_backfill_bars: int = FEED_H1_BACKFILL_BARS,
    h1_min_usable_bars: int = FEED_H1_MIN_USABLE_BARS,
) -> HydrationReport:
    """Hydrate every bundle in parallel and return an aggregate report.

    Uses a thread pool sized to ``len(bundles)`` — the bottleneck is
    REST latency, not CPU, so spinning one thread per pair is the
    cleanest way to overlap the calls.

    When ``h1_enabled`` is true *and* a bundle carries both
    ``buffer_h1`` and ``archive_h1``, a second hydration call is
    issued for that pair with ``resolution="HOUR"`` and its result is
    aggregated into :py:attr:`HydrationReport.per_pair_h1`. The H1 leg
    runs sequentially after the M5 leg for the same pair (so a single
    pair never burns two REST quota slots in parallel) but H1 legs for
    different pairs still overlap via the same thread pool.

    ``h1_enabled`` / ``h1_backfill_bars`` / ``h1_min_usable_bars`` are
    test seams; defaults flow from :py:mod:`feed.constants`.
    """
    started = (now_utc or (lambda: datetime.now(timezone.utc)))()
    if not bundles:
        return HydrationReport(
            started_at_utc=started,
            finished_at_utc=started,
            per_pair=(),
            per_pair_h1=(),
        )
    per_pair: list[PairHydrationReport] = []
    per_pair_h1: list[PairHydrationReport] = []

    def _hydrate_one(b: PairBundle) -> tuple[
        PairHydrationReport, Optional[PairHydrationReport]
    ]:
        m5_report = hydrate_pair(
            b.pair,
            b.epic,
            archive=b.archive,
            buffer=b.buffer,
            fetcher=fetcher,
            now_utc=now_utc,
            backfill_bars=backfill_bars,
            freshness_min=freshness_min,
            resolution=resolution,
        )
        h1_report: Optional[PairHydrationReport] = None
        if (
            h1_enabled
            and b.buffer_h1 is not None
            and b.archive_h1 is not None
        ):
            try:
                h1_report = hydrate_pair(
                    b.pair,
                    b.epic,
                    archive=b.archive_h1,
                    buffer=b.buffer_h1,
                    fetcher=fetcher,
                    now_utc=now_utc,
                    backfill_bars=h1_backfill_bars,
                    freshness_min=freshness_min,
                    resolution="HOUR",
                )
            except Exception as exc:
                # H1 leg must never poison the M5 path. The loop's
                # dispatcher will fall back to _legacy_h1_from_m5 when
                # the H1 buffer is short / empty (Commit 2).
                logger.exception(
                    "hydrate_pairs: H1 leg for %s raised — falling back "
                    "to M5-resample", b.pair,
                )
                h1_report = PairHydrationReport(
                    pair=b.pair,
                    mode="failed",
                    cached_bars=0,
                    rest_bars=0,
                    final_buffer_size=0,
                    newest_close_time=None,
                    error=f"H1 leg raised: {exc}",
                )
            if (
                h1_report is not None
                and h1_report.mode != "failed"
                and h1_report.final_buffer_size < h1_min_usable_bars
            ):
                # R1 in the design doc: new market / illiquid epic
                # returned fewer than the classifier minimum. The
                # buffer is still populated; the loop dispatcher will
                # route to the legacy path until the buffer warms up
                # via live BAR_CLOSE updates.
                logger.warning(
                    "hydrate_pairs: H1 buffer for %s has %d bars "
                    "(< %d minimum) — dispatcher will use legacy "
                    "M5-resample fallback until warmed up",
                    b.pair,
                    h1_report.final_buffer_size,
                    h1_min_usable_bars,
                )
        return m5_report, h1_report

    with ThreadPoolExecutor(max_workers=len(bundles)) as pool:
        futures = {pool.submit(_hydrate_one, b): b.pair for b in bundles}
        for fut in as_completed(futures):
            pair = futures[fut]
            try:
                m5_report, h1_report = fut.result()
                per_pair.append(m5_report)
                if h1_report is not None:
                    per_pair_h1.append(h1_report)
            except Exception as exc:
                logger.exception(
                    "hydrate_pairs: pair %s raised unhandled exception", pair,
                )
                per_pair.append(
                    PairHydrationReport(
                        pair=pair,
                        mode="failed",
                        cached_bars=0,
                        rest_bars=0,
                        final_buffer_size=0,
                        newest_close_time=None,
                        error=f"unhandled: {exc}",
                    )
                )
    finished = (now_utc or (lambda: datetime.now(timezone.utc)))()
    return HydrationReport(
        started_at_utc=started,
        finished_at_utc=finished,
        per_pair=tuple(sorted(per_pair, key=lambda r: r.pair)),
        per_pair_h1=tuple(sorted(per_pair_h1, key=lambda r: r.pair)),
    )


__all__ = [
    "HistoryFetcher",
    "HydrationReport",
    "PairBundle",
    "PairHydrationReport",
    "hydrate_pair",
    "hydrate_pairs",
    "parse_ig_history",
]
