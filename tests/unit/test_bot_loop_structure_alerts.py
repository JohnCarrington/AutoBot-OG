"""C-6 integration tests for Phase 12 structure alerting inside BotLoop.

Wires a real :class:`BotLoop` with the fakes from
``test_bot_loop.py`` and a record-only alerter. Asserts the
six contracts from the C-6 brief:

1. First post-startup bar produces zero structure alerts (cold-start
   contract — prev is None for every pair).
2. A subsequent bar that flips ``htf_bias`` produces exactly one
   ``HTF_BIAS_CHANGE`` alert at the recording alerter.
3. A bar where ``close_time.minute == 0`` produces a
   ``HOURLY_SUMMARY`` alert per configured pair.
4. ``self._previous_structure[pair]`` is updated after every bar
   close (so the next bar's diff has a non-None prev).
5. ``BotLoop.hydrate()`` populates ``_previous_structure`` from the
   engine jsonl path.
6. An exception inside ``process_structure_alerts`` is caught and
   logged but does NOT trip the BAR_CLOSE event-failure counter.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot.loop import BotLoop
from feed.types import Candle, FeedEvent, FeedEventKind

# Reuse the well-tested fake fixtures from the main BotLoop suite.
from tests.unit.test_bot_loop import (
    _FakeBuffer,
    _FakeExecutor,
    _FakeFeed,
    _FakeIGClient,
    _FakePositionManager,
    _FakeRiskGuard,
    _HydrationReport,
    _RecordingAlerter,
)


_NOW = datetime(2026, 5, 15, 13, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Local builders — mirror the test_bot_loop shape but expose the
# Phase 12 surface (alerter + hooks into self._previous_structure).
# ---------------------------------------------------------------------------


def _candle(pair: str, close_time: datetime, **overrides) -> Candle:
    defaults = dict(
        pair=pair,
        close_time=close_time,
        open=1.3000,
        high=1.3010,
        low=1.2990,
        close=1.3005,
        volume=200.0,
        source="LS_NATIVE_5M",
    )
    defaults.update(overrides)
    return Candle(**defaults)


def _bar_close(pair: str, close_time: datetime) -> FeedEvent:
    return FeedEvent(
        kind=FeedEventKind.BAR_CLOSE,
        pair=pair,
        candle=_candle(pair, close_time),
        timestamp=close_time,
        debug={},
    )


def _seed_candles(pair: str, n: int = 60) -> list[Candle]:
    """Produce ``n`` synthetic 5-minute candles ending just before _NOW.

    The structure engine needs ~50+ M5 candles to validate (per
    MIN_CANDLES_M5). We seed 60 so the engine returns ``is_valid=True``
    on the first bar the test fires.
    """
    return [
        _candle(pair, _NOW - timedelta(minutes=5 * (n - i)))
        for i in range(n)
    ]


def _build_bot_with_alerter(
    monkeypatch, pairs=("GBPUSD",), clock=None,
) -> tuple[BotLoop, _RecordingAlerter, _FakeFeed]:
    """Construct a BotLoop with all fakes + recording alerter wired."""
    feed = _FakeFeed()
    for pair in pairs:
        feed.add_buffer(pair, _seed_candles(pair))

    ig = _FakeIGClient()
    executor = _FakeExecutor()
    pm = _FakePositionManager()

    from regime.engine import RegimeEngine
    engines = {p: RegimeEngine() for p in pairs}
    rg = _FakeRiskGuard(engine_for_pair=lambda pair: engines[pair])

    import bot.loop as loop_mod
    monkeypatch.setattr(
        loop_mod, "fetch_market_info",
        lambda session, epic: type(
            "MI", (), {"bid": 1.30000, "offer": 1.30020}
        )(),
    )

    alerter = _RecordingAlerter()
    bot = BotLoop(
        feed_manager=feed,           # type: ignore[arg-type]
        ig_client=ig,                # type: ignore[arg-type]
        executor=executor,           # type: ignore[arg-type]
        risk_guard=rg,               # type: ignore[arg-type]
        position_manager=pm,         # type: ignore[arg-type]
        pairs=tuple(pairs),
        pair_to_epic={p: f"CS.D.{p}.TODAY.IP" for p in pairs},
        regime_engines=engines,
        clock=clock or (lambda: _NOW),
        alerter=alerter,             # type: ignore[arg-type]
    )
    return bot, alerter, feed


# ---------------------------------------------------------------------------
# Contract 1: cold-start — first bar produces no structure alerts
# ---------------------------------------------------------------------------


def test_first_bar_post_startup_produces_no_structure_alerts(monkeypatch) -> None:
    """The cold-start contract: prev is None for every pair on the
    first bar, so :func:`compute_structure_diff` returns []. No
    HTF_BIAS_CHANGE / MODE_CHANGE / etc. should reach the alerter.

    HOURLY_SUMMARY may or may not fire depending on close_time.minute
    — this test uses a bar whose minute != 0 so the assertion is
    "exactly zero structure alerts".
    """
    bot, alerter, feed = _build_bot_with_alerter(monkeypatch)
    bot.start()
    bot.mark_ready()
    # Close at 13:05 (minute != 0 → no hourly summary fires).
    bar_time = _NOW + timedelta(minutes=5)
    feed.fire(_bar_close("GBPUSD", bar_time))

    structure_alerts = [
        a for a in alerter.sent
        if a.category.value == "STRUCTURE"
    ]
    assert structure_alerts == []


def test_first_bar_updates_previous_structure_cache(monkeypatch) -> None:
    """Contract 4 (first-bar half): even though no alert fires on
    bar 1, the structure_state must be cached so bar 2's diff works."""
    bot, _alerter, feed = _build_bot_with_alerter(monkeypatch)
    bot.start()
    bot.mark_ready()
    bar_time = _NOW + timedelta(minutes=5)
    feed.fire(_bar_close("GBPUSD", bar_time))

    cached = bot._previous_structure.get("GBPUSD")
    assert cached is not None
    assert cached.pair == "GBPUSD"


# ---------------------------------------------------------------------------
# Contract 2: bar that flips htf_bias produces one HTF_BIAS_CHANGE
# ---------------------------------------------------------------------------


def test_bar_after_bias_flip_produces_htf_bias_change_alert(monkeypatch) -> None:
    """Inject a pre-cached previous-structure with a different bias,
    then fire one bar and assert HTF_BIAS_CHANGE reaches the alerter.

    Pre-caching ``_previous_structure`` is the post-hydration state —
    the same path BotLoop.hydrate() takes. This isolates the test to
    the diff/dispatch leg without needing two seeded BAR_CLOSE events.
    """
    from structure_alerts.diff import compute_structure_diff
    from structure_engine.types import StructureState

    bot, alerter, feed = _build_bot_with_alerter(monkeypatch)
    bot.start()
    bot.mark_ready()

    # Seed a prev with BEARISH bias so the next analyze_structure
    # output (which is computed from neutral seed candles) flips.
    prev = StructureState(
        pair="GBPUSD",
        timestamp="2026-05-15T12:55:00+00:00",
        is_valid=True,
        htf_bias="BEARISH",
        local_bias="NEUTRAL",
        nearest_support=None,
        nearest_resistance=None,
        liquidity_above=None,
        liquidity_below=None,
        current_reaction="NONE",
        acceptance_state="NONE",
        structure_mode="RANGE_BALANCE",
        confidence=0.5,
        reason="seeded",
        levels=[],
        debug={"seeded": True},
    )
    bot._previous_structure["GBPUSD"] = prev

    # Fire a bar whose minute != 0 so HOURLY_SUMMARY doesn't muddy
    # the assertion.
    bar_time = _NOW + timedelta(minutes=5)
    feed.fire(_bar_close("GBPUSD", bar_time))

    structure_alerts = [
        a for a in alerter.sent
        if a.category.value == "STRUCTURE"
    ]
    # Exactly HTF_BIAS_CHANGE fires (engine produces NEUTRAL bias
    # from flat seed candles, prev was BEARISH → transition). Pinning
    # the full subtype list (not `in`) is intentional: it surfaces any
    # diff-layer regression that introduces an extra event on a bar
    # whose seed is "empty levels, populated bias" — which is exactly
    # the H1-shaped scenario (pre-refinement-A hydration) where a
    # spurious NEW_MAJOR_LEVEL would otherwise slide through.
    subtypes = [a.event_subtype for a in structure_alerts]
    assert subtypes == ["HTF_BIAS_CHANGE"], (
        f"Expected exactly one HTF_BIAS_CHANGE; got: {subtypes}"
    )
    htf_alert = structure_alerts[0]
    assert htf_alert.severity.value == "WARNING"
    assert htf_alert.pair == "GBPUSD"
    # Dedupe-key injected into debug per the translator contract.
    assert "dedupe_key" in htf_alert.debug
    assert htf_alert.debug["dedupe_key"].startswith("GBPUSD_HTF_BIAS_")


# ---------------------------------------------------------------------------
# Contract 3: top-of-hour bar fires HOURLY_SUMMARY per pair
# ---------------------------------------------------------------------------


def test_top_of_hour_bar_fires_hourly_summary(monkeypatch) -> None:
    """A bar with ``close_time.minute == 0`` triggers a HOURLY_SUMMARY
    alert. Multi-pair version (`...per_pair`) below."""
    bot, alerter, feed = _build_bot_with_alerter(monkeypatch)
    bot.start()
    bot.mark_ready()
    # _NOW = 13:00 — exact top of hour.
    feed.fire(_bar_close("GBPUSD", _NOW))

    summaries = [
        a for a in alerter.sent
        if a.event_subtype == "HOURLY_SUMMARY"
    ]
    assert len(summaries) == 1
    assert summaries[0].pair == "GBPUSD"
    assert summaries[0].severity.value == "INFO"
    assert summaries[0].category.value == "STRUCTURE"
    # Dedupe key carries the hour bucket.
    assert "HOURLY_SUMMARY_" in summaries[0].debug["dedupe_key"]


def test_top_of_hour_bar_fires_one_hourly_summary_per_pair(monkeypatch) -> None:
    bot, alerter, feed = _build_bot_with_alerter(
        monkeypatch, pairs=("GBPUSD", "EURUSD"),
    )
    bot.start()
    bot.mark_ready()
    feed.fire(_bar_close("GBPUSD", _NOW))
    feed.fire(_bar_close("EURUSD", _NOW))

    summaries = [
        a for a in alerter.sent
        if a.event_subtype == "HOURLY_SUMMARY"
    ]
    pairs = {a.pair for a in summaries}
    assert pairs == {"GBPUSD", "EURUSD"}


def test_non_top_of_hour_bar_does_not_fire_hourly_summary(monkeypatch) -> None:
    bot, alerter, feed = _build_bot_with_alerter(monkeypatch)
    bot.start()
    bot.mark_ready()
    # 13:05 — minute != 0.
    feed.fire(_bar_close("GBPUSD", _NOW + timedelta(minutes=5)))

    summaries = [
        a for a in alerter.sent
        if a.event_subtype == "HOURLY_SUMMARY"
    ]
    assert summaries == []


# ---------------------------------------------------------------------------
# Contract 4 (continued): _previous_structure updated every bar
# ---------------------------------------------------------------------------


def test_previous_structure_updates_across_consecutive_bars(monkeypatch) -> None:
    bot, _alerter, feed = _build_bot_with_alerter(monkeypatch)
    bot.start()
    bot.mark_ready()
    feed.fire(_bar_close("GBPUSD", _NOW + timedelta(minutes=5)))
    first = bot._previous_structure["GBPUSD"]

    # Append a new candle so the buffer has fresh data for the second bar.
    feed._buffers["GBPUSD"]._candles.append(
        _candle("GBPUSD", _NOW + timedelta(minutes=10), close=1.3020)
    )
    feed.fire(_bar_close("GBPUSD", _NOW + timedelta(minutes=10)))
    second = bot._previous_structure["GBPUSD"]

    assert first is not None
    assert second is not None
    assert second.timestamp != first.timestamp


# ---------------------------------------------------------------------------
# Contract 5: hydrate() populates _previous_structure from jsonl
# ---------------------------------------------------------------------------


def _full_hydration_report(pairs: tuple[str, ...]):
    """Build a per-pair-shaped HydrationReport for tests that exercise
    ``bot.hydrate()``. The shared ``_HydrationReport`` fake in
    ``test_bot_loop.py`` omits ``per_pair`` because no existing test
    calls ``hydrate()``; C-6 is the first integration that does.
    """
    from dataclasses import dataclass
    from datetime import datetime, timezone

    @dataclass(frozen=True)
    class _PerPair:
        pair: str
        cached_bars: int = 50
        rest_bars: int = 0
        mode: str = "cache_only"

    @dataclass(frozen=True)
    class _Aggregate:
        per_pair: tuple
        failed_pairs: tuple = ()
        degraded_pairs: tuple = ()
        ok: bool = True

    return _Aggregate(per_pair=tuple(_PerPair(pair=p) for p in pairs))


def test_hydrate_populates_previous_structure_from_jsonl(
    monkeypatch, tmp_path,
) -> None:
    """End-to-end: write a Phase-11-compatible jsonl, point
    STRUCTURE_LOG_PATH at it, run hydrate(), assert
    _previous_structure carries the rehydrated state."""
    jsonl = tmp_path / "structure_state.jsonl"
    rec = {
        "timestamp": "2026-05-15T12:55:00+00:00",
        "pair": "GBPUSD",
        "is_valid": True,
        "htf_bias": "BEARISH",
        "local_bias": "NEUTRAL",
        "nearest_support": 1.30000,
        "nearest_support_score": 7.5,
        "nearest_resistance": 1.31000,
        "nearest_resistance_score": 8.0,
        "liquidity_above": None,
        "liquidity_below": None,
        "current_reaction": "NONE",
        "acceptance_state": "NONE",
        "structure_mode": "RANGE_BALANCE",
        "confidence": 0.7,
        "reason": "prior",
        "levels": [
            {"p": 1.30000, "s": "SUPPORT", "sc": 7.5, "tf": "H1"},
            {"p": 1.31000, "s": "RESISTANCE", "sc": 8.0, "tf": "M15"},
        ],
    }
    jsonl.write_text(json.dumps(rec) + "\n")
    monkeypatch.setenv("STRUCTURE_LOG_PATH", str(jsonl))
    monkeypatch.setenv("STRUCTURE_LOG_ENABLED", "1")  # M2: hydration is gated

    bot, _alerter, feed = _build_bot_with_alerter(monkeypatch)
    feed.hydrate_report = _full_hydration_report(("GBPUSD",))
    bot.hydrate()

    cached = bot._previous_structure.get("GBPUSD")
    assert cached is not None
    assert cached.htf_bias == "BEARISH"
    assert cached.structure_mode == "RANGE_BALANCE"
    assert cached.nearest_support is not None
    assert cached.nearest_support.price == 1.30000
    assert cached.nearest_support.timeframe == "H1"  # refinement A


def test_hydrate_missing_jsonl_leaves_cache_empty(monkeypatch, tmp_path) -> None:
    """No jsonl on disk = cold start. hydrate() must not raise."""
    monkeypatch.setenv(
        "STRUCTURE_LOG_PATH", str(tmp_path / "nonexistent.jsonl"),
    )
    monkeypatch.setenv("STRUCTURE_LOG_ENABLED", "1")  # M2: hydration is gated
    bot, _alerter, feed = _build_bot_with_alerter(monkeypatch)
    feed.hydrate_report = _full_hydration_report(("GBPUSD",))
    bot.hydrate()
    assert bot._previous_structure == {}


def test_hydrate_skipped_when_structure_log_disabled(
    monkeypatch, tmp_path, caplog,
) -> None:
    """M2: when STRUCTURE_LOG_ENABLED is false, hydration is skipped
    entirely (does not even scan the jsonl path). Without this gate,
    a "logging-on -> logging-off -> restart" config sequence would
    leave the engine silent this session but rehydrate from a stale
    prior-session jsonl, bursting spurious WARNING events on the
    first post-restart bar.
    """
    jsonl = tmp_path / "structure_state.jsonl"
    rec = {
        "timestamp": "2026-05-15T12:55:00+00:00",
        "pair": "GBPUSD",
        "is_valid": True,
        "htf_bias": "BEARISH",
        "local_bias": "NEUTRAL",
        "nearest_support": 1.30000,
        "nearest_resistance": 1.31000,
        "liquidity_above": None,
        "liquidity_below": None,
        "current_reaction": "NONE",
        "acceptance_state": "NONE",
        "structure_mode": "RANGE_BALANCE",
        "confidence": 0.7,
        "reason": "stale",
        "levels": [],
    }
    jsonl.write_text(json.dumps(rec) + "\n")
    monkeypatch.setenv("STRUCTURE_LOG_PATH", str(jsonl))
    monkeypatch.delenv("STRUCTURE_LOG_ENABLED", raising=False)

    bot, _alerter, feed = _build_bot_with_alerter(monkeypatch)
    feed.hydrate_report = _full_hydration_report(("GBPUSD",))
    with caplog.at_level("INFO", logger="bot.loop"):
        bot.hydrate()

    # No state was rehydrated despite the jsonl on disk.
    assert bot._previous_structure == {}
    # Operator gets a clear log explaining cold-start.
    assert any(
        "STRUCTURE_LOG_ENABLED is off" in r.getMessage()
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Contract 6: exception in processor logs but doesn't trip bar-close
# ---------------------------------------------------------------------------


def test_processor_exception_does_not_crash_bar_close(
    monkeypatch, caplog,
) -> None:
    """A bug inside process_structure_alerts must not bubble out of
    _handle_bar_close. The BAR_CLOSE pipeline must keep running for
    the rest of the bar's work (signal pipeline, SL evaluation, etc.).

    Patches process_structure_alerts to raise; asserts no exception
    propagates and a WARNING is logged.
    """
    bot, alerter, feed = _build_bot_with_alerter(monkeypatch)
    bot.start()
    bot.mark_ready()

    import bot.loop as loop_mod
    def _boom(**kwargs):
        raise RuntimeError("simulated processor failure")
    monkeypatch.setattr(loop_mod, "process_structure_alerts", _boom)

    bar_time = _NOW + timedelta(minutes=5)
    with caplog.at_level("ERROR", logger="bot.loop"):
        feed.fire(_bar_close("GBPUSD", bar_time))

    # Exception was caught and logged.
    matching = [
        r for r in caplog.records
        if "structure_alerts processor failed" in r.getMessage()
    ]
    assert len(matching) == 1
    # No alerts dispatched for this bar.
    assert [a for a in alerter.sent if a.category.value == "STRUCTURE"] == []
    # _previous_structure still updates so the next bar's diff has
    # a fresh prev (the engine snapshot itself isn't broken).
    assert bot._previous_structure["GBPUSD"] is not None


def test_alerter_send_exception_does_not_crash_bar_close(
    monkeypatch, caplog,
) -> None:
    """A raise inside Phase 9 alerter.send must not bubble out either.
    Defensive — alerter has its own three-layer guards but the C-6
    dispatch helper adds another."""
    from structure_alerts.diff import compute_structure_diff
    from structure_engine.types import StructureState

    bot, alerter, feed = _build_bot_with_alerter(monkeypatch)
    bot.start()
    bot.mark_ready()

    # Seed prev so an HTF_BIAS_CHANGE fires.
    bot._previous_structure["GBPUSD"] = StructureState(
        pair="GBPUSD",
        timestamp="2026-05-15T12:55:00+00:00",
        is_valid=True,
        htf_bias="BEARISH",  # engine will produce NEUTRAL → transition
        local_bias="NEUTRAL",
        nearest_support=None,
        nearest_resistance=None,
        liquidity_above=None,
        liquidity_below=None,
        current_reaction="NONE",
        acceptance_state="NONE",
        structure_mode="RANGE_BALANCE",
        confidence=0.5,
        reason="seeded",
        levels=[],
        debug={},
    )

    # Replace alerter.send with a raiser.
    def _boom(_alert):
        raise RuntimeError("simulated telegram failure")
    alerter.send = _boom  # type: ignore[assignment]

    bar_time = _NOW + timedelta(minutes=5)
    with caplog.at_level("ERROR", logger="bot.loop"):
        feed.fire(_bar_close("GBPUSD", bar_time))

    matching = [
        r for r in caplog.records
        if "structure alert dispatch failed" in r.getMessage()
    ]
    assert len(matching) >= 1


def test_persistence_non_oserror_does_not_crash_bar_close(
    monkeypatch, caplog,
) -> None:
    """M3: append_event_to_jsonl swallows OSError internally; the
    outer Exception catch in _dispatch_structure_alerts handles the
    residual cases (TypeError on JSON encode, ValueError on bad
    datetime). Symmetric coverage with
    test_processor_exception_does_not_crash_bar_close and
    test_alerter_send_exception_does_not_crash_bar_close — completes
    the failure-isolation test trio.
    """
    from structure_engine.types import StructureState

    bot, alerter, feed = _build_bot_with_alerter(monkeypatch)
    bot.start()
    bot.mark_ready()

    # Seed prev so HTF_BIAS_CHANGE fires (produces at least one event
    # to persist).
    bot._previous_structure["GBPUSD"] = StructureState(
        pair="GBPUSD",
        timestamp="2026-05-15T12:55:00+00:00",
        is_valid=True,
        htf_bias="BEARISH",
        local_bias="NEUTRAL",
        nearest_support=None,
        nearest_resistance=None,
        liquidity_above=None,
        liquidity_below=None,
        current_reaction="NONE",
        acceptance_state="NONE",
        structure_mode="RANGE_BALANCE",
        confidence=0.5,
        reason="seeded",
        levels=[],
        debug={},
    )

    # Monkey-patch the persistence shim to raise a non-OSError that
    # the inner function would NOT swallow.
    import bot.loop as loop_mod
    def _boom(event, path):
        raise TypeError("simulated non-encodable payload")
    monkeypatch.setattr(loop_mod, "append_event_to_jsonl", _boom)

    bar_time = _NOW + timedelta(minutes=5)
    with caplog.at_level("ERROR", logger="bot.loop"):
        feed.fire(_bar_close("GBPUSD", bar_time))

    # Outer catch fired with the expected log message.
    matching = [
        r for r in caplog.records
        if "structure_alerts jsonl write failed" in r.getMessage()
    ]
    assert len(matching) >= 1
    # Dispatch still happened (persistence runs AFTER dispatch per
    # the locked decision).
    structure_alerts = [
        a for a in alerter.sent
        if a.category.value == "STRUCTURE"
    ]
    assert len(structure_alerts) >= 1
    # Bar-close pipeline didn't crash — prev cache still updated.
    assert bot._previous_structure["GBPUSD"] is not None


# ---------------------------------------------------------------------------
# M5: single clock capture per bar
# ---------------------------------------------------------------------------


def test_dispatch_uses_single_clock_capture_per_bar(monkeypatch) -> None:
    """M5: ``_clock()`` is called exactly once per bar; every downstream
    timestamp / dedupe-clock site in the structure-alerts pipeline uses
    that captured value. Pre-fix, the same bar's diff event and hourly
    summary carried slightly different timestamps (μs of skew from
    repeated ``self._clock()`` calls) — production-visible impact was
    zero, but the locked decision was one shared ``now`` per bar.

    Test setup fires a top-of-hour bar with a seeded prev so the bar
    produces BOTH a diff event (HTF_BIAS_CHANGE) and a HOURLY_SUMMARY.
    The clock is monkey-patched to increment on every call; under the
    fixed code, the dispatched Alerts share one timestamp. Under the
    pre-fix code, the summary's timestamp would be a later value
    than the diff event's.
    """
    from structure_engine.types import StructureState

    # Stepping clock — every call returns a strictly later datetime
    # so even μs-level skew is visible.
    base = _NOW.replace(microsecond=0)
    counter = {"n": 0}

    def stepping_clock() -> datetime:
        counter["n"] += 1
        return base + timedelta(microseconds=counter["n"])

    bot, alerter, feed = _build_bot_with_alerter(
        monkeypatch, clock=stepping_clock,
    )
    bot.start()
    bot.mark_ready()

    # Seed prev so HTF_BIAS_CHANGE fires on this bar.
    bot._previous_structure["GBPUSD"] = StructureState(
        pair="GBPUSD",
        timestamp="2026-05-15T12:55:00+00:00",
        is_valid=True,
        htf_bias="BEARISH",
        local_bias="NEUTRAL",
        nearest_support=None,
        nearest_resistance=None,
        liquidity_above=None,
        liquidity_below=None,
        current_reaction="NONE",
        acceptance_state="NONE",
        structure_mode="RANGE_BALANCE",
        confidence=0.5,
        reason="seeded",
        levels=[],
        debug={},
    )

    # _NOW = 13:00 — top of hour, fires both kinds.
    feed.fire(_bar_close("GBPUSD", _NOW))

    structure_alerts = [
        a for a in alerter.sent if a.category.value == "STRUCTURE"
    ]
    # Both a diff event and a summary must have dispatched for this
    # test to mean anything.
    subtypes = {a.event_subtype for a in structure_alerts}
    assert "HTF_BIAS_CHANGE" in subtypes
    assert "HOURLY_SUMMARY" in subtypes

    # Single capture: every STRUCTURE alert from this bar shares one
    # timestamp. (Translator inherits AlertEvent.timestamp, which is
    # the ``now`` passed into processor / summary builder.) Pre-fix,
    # the summary's timestamp ran a few μs ahead of the diff event's
    # because _dispatch_structure_alerts and _dispatch_hourly_summary
    # each called self._clock() independently.
    timestamps = {a.timestamp for a in structure_alerts}
    assert len(timestamps) == 1, (
        f"Expected one shared timestamp across bar's STRUCTURE alerts; "
        f"got {len(timestamps)}: {sorted(timestamps)}"
    )


# ---------------------------------------------------------------------------
# Persistence integration
# ---------------------------------------------------------------------------


def test_structure_alert_persists_to_jsonl_audit_log(
    monkeypatch, tmp_path,
) -> None:
    """Each surviving alert event lands as one line in the audit
    jsonl. Both diff-driven events and the hourly summary."""
    audit_path = tmp_path / "structure_alerts.jsonl"
    # M4: bot.loop reads the path via structure_alerts_log_path() per
    # call, which reads STRUCTURE_ALERTS_LOG_PATH from env each time.
    monkeypatch.setenv("STRUCTURE_ALERTS_LOG_PATH", str(audit_path))

    from structure_engine.types import StructureState

    bot, _alerter, feed = _build_bot_with_alerter(monkeypatch)
    bot.start()
    bot.mark_ready()

    # Seed prev so HTF_BIAS_CHANGE fires.
    bot._previous_structure["GBPUSD"] = StructureState(
        pair="GBPUSD",
        timestamp="2026-05-15T12:55:00+00:00",
        is_valid=True,
        htf_bias="BEARISH",
        local_bias="NEUTRAL",
        nearest_support=None,
        nearest_resistance=None,
        liquidity_above=None,
        liquidity_below=None,
        current_reaction="NONE",
        acceptance_state="NONE",
        structure_mode="RANGE_BALANCE",
        confidence=0.5,
        reason="seeded",
        levels=[],
        debug={},
    )

    # Top-of-hour so HOURLY_SUMMARY also fires.
    feed.fire(_bar_close("GBPUSD", _NOW))

    assert audit_path.exists()
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    payloads = [json.loads(line) for line in lines]
    kinds = [p["kind"] for p in payloads]
    # Exact set + count: a bias-flip + top-of-hour bar must produce
    # exactly HTF_BIAS_CHANGE and HOURLY_SUMMARY in the audit log.
    # Pinning the count (not `in` containment) means a future
    # diff-layer regression that adds a spurious event on this
    # seed (empty levels, populated bias) turns this test red.
    assert sorted(kinds) == ["HOURLY_SUMMARY", "HTF_BIAS_CHANGE"], (
        f"Expected exactly HTF_BIAS_CHANGE + HOURLY_SUMMARY; got: {kinds}"
    )


# ---------------------------------------------------------------------------
# Dedupe interaction across bars
# ---------------------------------------------------------------------------


def test_repeated_bias_change_within_cooldown_blocks_second_alert(
    monkeypatch,
) -> None:
    """Two consecutive bars both showing BEARISH (after a BULLISH prev
    state was seeded) should produce only ONE HTF_BIAS_CHANGE alert
    — the second is within the WARNING 1h cooldown window."""
    from structure_engine.types import StructureState

    bot, alerter, feed = _build_bot_with_alerter(monkeypatch)
    bot.start()
    bot.mark_ready()
    bot._previous_structure["GBPUSD"] = StructureState(
        pair="GBPUSD",
        timestamp="2026-05-15T12:55:00+00:00",
        is_valid=True,
        htf_bias="BEARISH",  # engine produces NEUTRAL → transition
        local_bias="NEUTRAL",
        nearest_support=None,
        nearest_resistance=None,
        liquidity_above=None,
        liquidity_below=None,
        current_reaction="NONE",
        acceptance_state="NONE",
        structure_mode="RANGE_BALANCE",
        confidence=0.5,
        reason="seeded",
        levels=[],
        debug={},
    )
    feed.fire(_bar_close("GBPUSD", _NOW + timedelta(minutes=5)))

    # Flip the prev back to BEARISH and fire again 5 minutes later —
    # the diff would emit HTF_BIAS_CHANGE again, but dedupe should
    # block it (WARNING cooldown is 1h).
    bot._previous_structure["GBPUSD"] = StructureState(
        pair="GBPUSD",
        timestamp="2026-05-15T13:00:00+00:00",
        is_valid=True,
        htf_bias="BEARISH",
        local_bias="NEUTRAL",
        nearest_support=None,
        nearest_resistance=None,
        liquidity_above=None,
        liquidity_below=None,
        current_reaction="NONE",
        acceptance_state="NONE",
        structure_mode="RANGE_BALANCE",
        confidence=0.5,
        reason="seeded2",
        levels=[],
        debug={},
    )
    feed.fire(_bar_close("GBPUSD", _NOW + timedelta(minutes=10)))

    htf_alerts = [
        a for a in alerter.sent
        if a.event_subtype == "HTF_BIAS_CHANGE"
    ]
    assert len(htf_alerts) == 1
