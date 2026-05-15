"""Tests for feed.feed_manager — orchestrator dispatch + state machine."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from feed.archive import CandleArchive
from feed.feed_manager import FeedManager, PairSetup
from feed.lightstreamer.client import (
    LightstreamerSubscriber,
    SubscriptionSpec,
)
from feed.rolling_buffer import RollingBuffer
from feed.types import Candle, FeedEvent, FeedEventKind


_NOW = datetime(2026, 5, 15, 13, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeSubscriber:
    """Stands in for LightstreamerSubscriber in unit tests.

    Exposes the manager-provided callbacks (``_on_update``, ``_on_status``)
    so tests can directly invoke them — bypassing the LS SDK entirely.
    """

    def __init__(
        self,
        on_update: Callable[[str, Candle, bool, dict[str, Any]], None],
        on_status: Callable[[str, Optional[str]], None],
    ) -> None:
        self.on_update = on_update
        self.on_status = on_status
        self.connected = False
        self.subscribed: list[SubscriptionSpec] = []
        self.disconnect_calls = 0

    def connect(self) -> str:
        self.connected = True
        return "CONNECTED:HTTP-STREAMING"

    def subscribe_pair(self, spec: SubscriptionSpec) -> None:
        self.subscribed.append(spec)

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False


def _candle(pair: str, offset_min: int = 0, **overrides) -> Candle:
    defaults = dict(
        pair=pair,
        close_time=_NOW + timedelta(minutes=5 * offset_min),
        open=1.30000,
        high=1.30100,
        low=1.29950,
        close=1.30050 + 0.0001 * offset_min,
        volume=200.0,
        source="LS_NATIVE_5M",
    )
    defaults.update(overrides)
    return Candle(**defaults)


def _build_manager(
    tmp_path: Path,
    pairs: list[str] = ("GBPUSD",),
    history_fetcher: Optional[Callable] = None,
) -> tuple[FeedManager, list[FakeSubscriber], list[FeedEvent]]:
    fakes: list[FakeSubscriber] = []

    def subscriber_factory(on_update, on_status):
        f = FakeSubscriber(on_update, on_status)
        fakes.append(f)
        return f  # FakeSubscriber duck-types LightstreamerSubscriber

    setups = [
        PairSetup(
            pair=p,
            epic=f"CS.D.{p}.TODAY.IP",
            archive=CandleArchive(p, base_dir=tmp_path),
            buffer=RollingBuffer(p, capacity=200),
        )
        for p in pairs
    ]
    fetcher = history_fetcher or (lambda *a, **kw: {"prices": []})
    fm = FeedManager.from_pairs(
        setups,
        history_fetcher=fetcher,
        subscriber_factory=subscriber_factory,  # type: ignore[arg-type]
        clock=lambda: _NOW,
    )
    received: list[FeedEvent] = []
    fm.on_event(received.append)
    return fm, fakes, received


# ---------------------------------------------------------------------------
# Hydration + start_live
# ---------------------------------------------------------------------------


def test_hydrate_uses_history_fetcher_and_seeds_buffer(tmp_path: Path) -> None:
    open_t = _NOW - timedelta(minutes=10)

    def fetcher(*_a, **_kw):
        return {
            "prices": [
                {
                    "snapshotTimeUTC": open_t.strftime("%Y-%m-%dT%H:%M:%S"),
                    "openPrice": {"bid": 1.3, "ask": 1.3002},
                    "highPrice": {"bid": 1.3005, "ask": 1.3007},
                    "lowPrice":  {"bid": 1.2995, "ask": 1.2997},
                    "closePrice": {"bid": 1.3001, "ask": 1.3003},
                    "lastTradedVolume": 100,
                }
            ]
        }

    fm, _, _ = _build_manager(tmp_path, history_fetcher=fetcher)
    report = fm.hydrate()
    assert report.ok
    latest = fm.latest_candle("GBPUSD")
    assert latest is not None
    # Hydration seeds last_emitted_close so subsequent live updates work.


def test_start_live_connects_and_subscribes_each_pair(tmp_path: Path) -> None:
    fm, fakes, _ = _build_manager(tmp_path, pairs=("GBPUSD", "EURUSD"))
    fm.start_live()
    assert len(fakes) == 1
    fake = fakes[0]
    assert fake.connected is True
    assert {s.pair for s in fake.subscribed} == {"GBPUSD", "EURUSD"}


def test_start_live_is_idempotent(tmp_path: Path) -> None:
    fm, fakes, _ = _build_manager(tmp_path)
    fm.start_live()
    fm.start_live()
    assert len(fakes) == 1  # only one subscriber ever made


# ---------------------------------------------------------------------------
# BAR_UPDATE / BAR_CLOSE dispatch
# ---------------------------------------------------------------------------


def test_first_update_emits_bar_update_only(tmp_path: Path) -> None:
    fm, fakes, events = _build_manager(tmp_path)
    fm.start_live()
    fakes[0].on_update("GBPUSD", _candle("GBPUSD", 0), False, {"CONS_END": "0"})
    kinds = [e.kind for e in events]
    assert kinds == [FeedEventKind.BAR_UPDATE]


def test_cons_end_flip_emits_bar_close(tmp_path: Path) -> None:
    fm, fakes, events = _build_manager(tmp_path)
    fm.start_live()
    fakes[0].on_update("GBPUSD", _candle("GBPUSD", 0), False, {"CONS_END": "0"})
    fakes[0].on_update("GBPUSD", _candle("GBPUSD", 0, close=1.31000), True, {"CONS_END": "1"})
    kinds = [e.kind for e in events]
    assert kinds == [FeedEventKind.BAR_UPDATE, FeedEventKind.BAR_UPDATE, FeedEventKind.BAR_CLOSE]
    close_event = events[-1]
    assert close_event.candle is not None
    assert close_event.candle.close == 1.31000


def test_boundary_crossing_closes_previous_and_opens_new(tmp_path: Path) -> None:
    fm, fakes, events = _build_manager(tmp_path)
    fm.start_live()
    # Bar 0 — first tick, in progress.
    fakes[0].on_update("GBPUSD", _candle("GBPUSD", 0), False, {})
    # Bar 1 — new boundary; bar 0 should close, bar 1 opens.
    fakes[0].on_update("GBPUSD", _candle("GBPUSD", 1), False, {})
    kinds = [e.kind for e in events]
    assert kinds == [FeedEventKind.BAR_UPDATE, FeedEventKind.BAR_CLOSE, FeedEventKind.BAR_UPDATE]


def test_archive_dedups_duplicate_close(tmp_path: Path) -> None:
    fm, fakes, _ = _build_manager(tmp_path)
    fm.start_live()
    fakes[0].on_update("GBPUSD", _candle("GBPUSD", 0), True, {"CONS_END": "1"})
    arc = CandleArchive("GBPUSD", base_dir=tmp_path).load()
    # Single row written.
    assert len(arc) == 1


def test_latest_candle_returns_in_progress_bar(tmp_path: Path) -> None:
    fm, fakes, _ = _build_manager(tmp_path)
    fm.start_live()
    fakes[0].on_update("GBPUSD", _candle("GBPUSD", 0, close=1.30050), False, {})
    fakes[0].on_update("GBPUSD", _candle("GBPUSD", 0, close=1.30100), False, {})
    latest = fm.latest_candle("GBPUSD")
    assert latest is not None
    assert latest.close == 1.30100


# ---------------------------------------------------------------------------
# Connection state events
# ---------------------------------------------------------------------------


def test_status_drop_emits_feed_stale_once(tmp_path: Path) -> None:
    fm, fakes, events = _build_manager(tmp_path)
    fm.start_live()
    fakes[0].on_status("DISCONNECTED", "CONNECTED:HTTP-STREAMING")
    fakes[0].on_status("STALLED", "DISCONNECTED")
    stale = [e for e in events if e.kind == FeedEventKind.FEED_STALE]
    assert len(stale) == 1


def test_reconnect_emits_resumed_and_attempts_gap_fill(tmp_path: Path) -> None:
    open_t = _NOW - timedelta(minutes=5)  # 1 bar gap

    def fetcher(epic, resolution, n):
        return {
            "prices": [
                {
                    # Use snapshotTimeUTC so the timestamp is literally
                    # UTC (no London-tz round-trip). H3 made
                    # snapshotTime broker-local.
                    "snapshotTimeUTC": open_t.strftime("%Y-%m-%dT%H:%M:%S"),
                    "openPrice": {"bid": 1.3, "ask": 1.3002},
                    "highPrice": {"bid": 1.3005, "ask": 1.3007},
                    "lowPrice":  {"bid": 1.2995, "ask": 1.2997},
                    "closePrice": {"bid": 1.3001, "ask": 1.3003},
                    "lastTradedVolume": 100,
                }
            ]
        }

    fm, fakes, events = _build_manager(tmp_path, history_fetcher=fetcher)
    fm.start_live()
    # Seed buffer with a bar that's 10 minutes old.
    fakes[0].on_update("GBPUSD", _candle("GBPUSD", offset_min=-2), True, {"CONS_END": "1"})
    # Drop and recover.
    fakes[0].on_status("DISCONNECTED", "CONNECTED:HTTP-STREAMING")
    fakes[0].on_status("CONNECTED:HTTP-STREAMING", "DISCONNECTED")
    kinds = [e.kind for e in events]
    assert FeedEventKind.FEED_RESUMED in kinds
    assert FeedEventKind.GAP_FILLED in kinds


def test_gap_fill_emits_bar_close_per_backfilled_bar(tmp_path: Path) -> None:
    """H2: multi-bar gap fill must emit one BAR_CLOSE per backfilled bar.

    Strategies subscribed to BAR_CLOSE re-run their decision pipeline
    on every close. Emitting only a final GAP_FILLED would silently
    skip the intermediate bars.
    """
    # Three 5-min bars: open at _NOW-15, _NOW-10, _NOW-5.
    fetcher_open_times = [
        _NOW - timedelta(minutes=15),
        _NOW - timedelta(minutes=10),
        _NOW - timedelta(minutes=5),
    ]

    def fetcher(epic, resolution, n):
        return {
            "prices": [
                {
                    # snapshotTimeUTC — see comment in
                    # test_reconnect_emits_resumed_and_attempts_gap_fill.
                    "snapshotTimeUTC": t.strftime("%Y-%m-%dT%H:%M:%S"),
                    "openPrice": {"bid": 1.3, "ask": 1.3002},
                    "highPrice": {"bid": 1.3005, "ask": 1.3007},
                    "lowPrice":  {"bid": 1.2995, "ask": 1.2997},
                    "closePrice": {"bid": 1.3001, "ask": 1.3003},
                    "lastTradedVolume": 100,
                }
                for t in fetcher_open_times
            ]
        }

    fm, fakes, events = _build_manager(tmp_path, history_fetcher=fetcher)
    fm.start_live()
    # Seed buffer with a bar that's 20 minutes old (close_time = _NOW - 20).
    fakes[0].on_update(
        "GBPUSD", _candle("GBPUSD", offset_min=-4), True, {"CONS_END": "1"},
    )
    # Drop and reconnect — triggers gap-fill for 3 missing bars.
    fakes[0].on_status("DISCONNECTED", "CONNECTED:HTTP-STREAMING")
    fakes[0].on_status("CONNECTED:HTTP-STREAMING", "DISCONNECTED")

    # Filter to events generated by the gap-fill path only.
    gap_bar_closes = [
        e for e in events
        if e.kind == FeedEventKind.BAR_CLOSE
        and e.debug.get("reason") == "gap_fill_backfill"
    ]
    gap_summary = [e for e in events if e.kind == FeedEventKind.GAP_FILLED]
    assert len(gap_bar_closes) == 3, (
        f"expected 3 BAR_CLOSE per backfilled bar, got {len(gap_bar_closes)}: "
        f"{[e.candle.close_time for e in gap_bar_closes]}"
    )
    assert len(gap_summary) == 1
    # Per-bar BAR_CLOSE events are in time order (oldest → newest).
    bar_close_times = [e.candle.close_time for e in gap_bar_closes]
    assert bar_close_times == sorted(bar_close_times)


def test_long_gap_skips_rest_backfill(tmp_path: Path) -> None:
    called = {"n": 0}

    def fetcher(*_a, **_kw):
        called["n"] += 1
        return {"prices": []}

    fm, fakes, events = _build_manager(tmp_path, history_fetcher=fetcher)
    fm.start_live()
    # Seed buffer with a bar 90 minutes old → outside FEED_GAP_FILL_WINDOW_MIN.
    fakes[0].on_update(
        "GBPUSD", _candle("GBPUSD", offset_min=-18), True, {"CONS_END": "1"},
    )
    fakes[0].on_status("DISCONNECTED", "CONNECTED:HTTP-STREAMING")
    fakes[0].on_status("CONNECTED:HTTP-STREAMING", "DISCONNECTED")
    # The fetcher must not be called for the over-window gap.
    assert called["n"] == 0
    assert not any(e.kind == FeedEventKind.GAP_FILLED for e in events)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def test_callback_exception_does_not_break_dispatch(tmp_path: Path) -> None:
    fm, fakes, events = _build_manager(tmp_path)
    fm.start_live()
    # Bad callback raises on every event.
    def bad(_evt: FeedEvent) -> None:
        raise RuntimeError("strategy bug")
    fm.on_event(bad)
    fakes[0].on_update("GBPUSD", _candle("GBPUSD", 0), False, {})
    fakes[0].on_update("GBPUSD", _candle("GBPUSD", 1), False, {})
    # The good callback (events.append) keeps receiving events.
    kinds = [e.kind for e in events]
    assert FeedEventKind.BAR_UPDATE in kinds and FeedEventKind.BAR_CLOSE in kinds


def test_update_for_unconfigured_pair_dropped(tmp_path: Path) -> None:
    fm, fakes, events = _build_manager(tmp_path, pairs=("GBPUSD",))
    fm.start_live()
    fakes[0].on_update("USDJPY", _candle("USDJPY", 0), False, {})
    assert events == []


def test_stop_disconnects_subscriber(tmp_path: Path) -> None:
    fm, fakes, _ = _build_manager(tmp_path)
    fm.start_live()
    fm.stop()
    assert fakes[0].disconnect_calls == 1


def test_watchdog_returns_empty_before_live(tmp_path: Path) -> None:
    fm, _, _ = _build_manager(tmp_path)
    assert fm.watchdog_stale_pairs() == []


def test_watchdog_skips_saturday_reports_wednesday(tmp_path: Path) -> None:
    """M8: watchdog must short-circuit when the FX market is closed.

    The existing watchdog tests happen to use ``_NOW = 2026-05-15``
    which is a Friday. This test pins the weekend gating directly: on
    a Saturday UTC the method returns ``[]`` regardless of staleness;
    on a weekday it surfaces stale pairs as before.
    """
    saturday = datetime(2026, 5, 16, 13, 0, tzinfo=timezone.utc)
    wednesday = datetime(2026, 5, 13, 13, 0, tzinfo=timezone.utc)
    assert saturday.isoweekday() == 6
    assert wednesday.isoweekday() == 3

    clock_ref = {"now": saturday}

    def subscriber_factory(on_update, on_status):
        return FakeSubscriber(on_update, on_status)

    setups = [
        PairSetup(
            pair="GBPUSD",
            epic="CS.D.GBPUSD.TODAY.IP",
            archive=CandleArchive("GBPUSD", base_dir=tmp_path),
            buffer=RollingBuffer("GBPUSD", capacity=200),
        )
    ]
    fm = FeedManager.from_pairs(
        setups,
        history_fetcher=lambda *a, **kw: {"prices": []},
        subscriber_factory=subscriber_factory,  # type: ignore[arg-type]
        clock=lambda: clock_ref["now"],
    )
    fm.start_live()  # is_live=True so the live-gate doesn't short-circuit
    # last_update_time_utc is None → would normally surface as stale.

    # Saturday: market closed → empty list regardless of staleness.
    assert fm.watchdog_stale_pairs() == []

    # Wednesday: market open → the pair surfaces (no LS update ever arrived).
    clock_ref["now"] = wednesday
    assert fm.watchdog_stale_pairs() == ["GBPUSD"]


# ---------------------------------------------------------------------------
# H4 — out-of-order observability
# ---------------------------------------------------------------------------


def test_out_of_order_payload_logs_warning_and_bumps_counter(
    tmp_path: Path, caplog
) -> None:
    """H4: stale-payload drop logs WARNING + increments per-pair counter."""
    import logging

    fm, fakes, events = _build_manager(tmp_path)
    fm.start_live()
    # Establish a baseline bar (offset_min=5).
    fakes[0].on_update("GBPUSD", _candle("GBPUSD", offset_min=5), True, {"CONS_END": "1"})
    # Now ship a stale payload (offset_min=0, older than the baseline).
    with caplog.at_level(logging.WARNING, logger="feed.feed_manager"):
        fakes[0].on_update(
            "GBPUSD", _candle("GBPUSD", offset_min=0), False, {"CONS_END": "0"},
        )
    assert fm.out_of_order_counts() == {"GBPUSD": 1}
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("out-of-order" in r.getMessage() for r in warns), (
        f"expected WARNING log for out-of-order drop, got {warns}"
    )
    # Stale payload must NOT have produced a BAR_UPDATE / BAR_CLOSE event.
    new_after_baseline = [
        e for e in events
        if e.kind in (FeedEventKind.BAR_UPDATE, FeedEventKind.BAR_CLOSE)
        and e.candle is not None
        and e.candle.close_time < _NOW + timedelta(minutes=5)
    ]
    assert not new_after_baseline


def test_out_of_order_counts_starts_zero_for_each_pair(tmp_path: Path) -> None:
    fm, _, _ = _build_manager(tmp_path, pairs=("GBPUSD", "EURUSD"))
    counts = fm.out_of_order_counts()
    assert counts == {"GBPUSD": 0, "EURUSD": 0}
