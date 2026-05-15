"""Tests for bot.loop — state machine, BAR_CLOSE pipeline, shutdown drain."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import pytest

from bot.loop import BotLoop
from bot.types import BotState
from feed.types import Candle, FeedEvent, FeedEventKind


_NOW = datetime(2026, 5, 15, 13, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Stand-ins for Phase 1-7 dependencies
# ---------------------------------------------------------------------------


class _FakeBuffer:
    def __init__(self, candles: list[Candle]) -> None:
        self._candles = list(candles)

    def to_dataframe(self):
        import pandas as pd
        if not self._candles:
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume"], dtype="float64",
            ).set_index(pd.DatetimeIndex([], tz="UTC", name="close_time"))
        return pd.DataFrame(
            {
                "open":   [c.open for c in self._candles],
                "high":   [c.high for c in self._candles],
                "low":    [c.low for c in self._candles],
                "close":  [c.close for c in self._candles],
                "volume": [c.volume for c in self._candles],
            },
            index=pd.DatetimeIndex(
                [c.close_time for c in self._candles], name="close_time", tz="UTC",
            ),
        )

    def latest(self) -> Optional[Candle]:
        return self._candles[-1] if self._candles else None


class _FakeFeed:
    def __init__(self) -> None:
        self._buffers: dict[str, _FakeBuffer] = {}
        self.callbacks: list = []
        self.started = False
        self.stopped = False
        self.hydrate_calls = 0
        self.hydrate_report = _HydrationReport(ok=True, failed=(), degraded=())

    def add_buffer(self, pair: str, candles: list[Candle]) -> None:
        self._buffers[pair] = _FakeBuffer(candles)

    def buffer_for(self, pair: str) -> Optional[_FakeBuffer]:
        return self._buffers.get(pair)

    def latest_candle(self, pair: str) -> Optional[Candle]:
        buf = self._buffers.get(pair)
        return buf.latest() if buf else None

    def hydrate(self):
        self.hydrate_calls += 1
        return self.hydrate_report

    def start_live(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def on_event(self, cb) -> None:
        self.callbacks.append(cb)

    def fire(self, event: FeedEvent) -> None:
        for cb in self.callbacks:
            cb(event)


@dataclass
class _HydrationReport:
    ok: bool
    failed: tuple = ()
    degraded: tuple = ()

    @property
    def failed_pairs(self) -> tuple:
        return self.failed

    @property
    def degraded_pairs(self) -> tuple:
        return self.degraded


class _FakeIGClient:
    """Stand-in for IGClient.

    ``close_position`` is the bug-paranoid surface that originally hid
    C1: it now asserts that ``request.position_direction`` is the
    standard "BUY"/"SELL" string and records the value verbatim so
    tests can pin the exact polarity Phase 8 sent. The real wrapper
    inverts internally — Phase 8 must pass the position's own
    direction.
    """

    def __init__(self) -> None:
        self.fetch_open_calls = 0
        self.close_calls: list = []
        self.session = type("S", (), {})()
        self._open_positions: list = []
        self.close_should_fail = False

    def fetch_open_positions(self):
        self.fetch_open_calls += 1
        return list(self._open_positions)

    def close_position(self, request):
        # Contract check — the existence of this assertion (rather than
        # blanket acceptance) is the lesson from C1.
        assert request.position_direction in ("BUY", "SELL"), (
            f"CloseRequest.position_direction must be BUY/SELL; "
            f"got {request.position_direction!r}"
        )
        self.close_calls.append(request)
        from feed.ig_rest.types import DealConfirmation
        return DealConfirmation(
            deal_reference="REF", deal_id=request.deal_id,
            status="REJECTED" if self.close_should_fail else "ACCEPTED",
        )


class _FakeExecutor:
    def __init__(self) -> None:
        self.opened: list = []
        self.amended: list = []
        self.open_should_block_ms = 0
        self.open_should_raise: Optional[BaseException] = None

    def open_from_signal(self, signal):
        if self.open_should_block_ms:
            import time
            time.sleep(self.open_should_block_ms / 1000.0)
        if self.open_should_raise:
            raise self.open_should_raise
        self.opened.append(signal)
        return type("TR", (), {"success": True, "deal_id": "DEAL_X"})()

    def apply_amend(self, amend):
        self.amended.append(amend)
        return type("AR", (), {"success": True})()


class _FakePositionManager:
    def __init__(self) -> None:
        self._positions: list = []
        self.saved = 0

    def for_pair(self, pair: str) -> list:
        return [p for p in self._positions if p.pair == pair]

    def all(self) -> list:
        return list(self._positions)

    def get(self, deal_id: str):
        return next((p for p in self._positions if p.deal_id == deal_id), None)

    def upsert(self, position) -> None:
        for i, p in enumerate(self._positions):
            if p.deal_id == position.deal_id:
                self._positions[i] = position
                return
        self._positions.append(position)

    def remove(self, deal_id: str):
        for i, p in enumerate(self._positions):
            if p.deal_id == deal_id:
                return self._positions.pop(i)
        return None

    def save_if_dirty(self) -> bool:
        self.saved += 1
        return True


class _FakeRiskGuard:
    """Stand-in for RiskGuard that actually consults a regime engine.

    The original C2 bug shipped because the fake just recorded
    ``allow_entry`` calls without ever touching the engine — a stale
    engine couldn't be detected at all. This rewrite mirrors the real
    surface: every entry decision calls ``engine.is_live()`` and
    ``engine.get_recent_emissions(window_minutes, now_utc)``, exactly
    like ``risk.guard.RiskGuard.allow_entry``. Per-pair routing flows
    via ``engine_for_pair`` (Option A from the review prompt).
    """

    def __init__(
        self,
        *,
        engine_for_pair=None,
        engine=None,
    ) -> None:
        self._engine_for_pair = engine_for_pair
        self._engine = engine
        self.allow_calls: list = []
        self.force_close_calls: list = []
        # (pair, is_live, len(recent_emissions)) recorded on every
        # allow_entry call — tests assert the *routing*, not just the
        # decision.
        self.observed_engine_lookups: list = []
        self.allow_result = None  # set per test
        self.force_close_result: list = []

    def _resolve_engine(self, pair: str):
        if self._engine_for_pair is not None:
            return self._engine_for_pair(pair)
        return self._engine

    def allow_entry(self, *, candidate, positions, account, market, now_utc):
        eng = self._resolve_engine(candidate.pair)
        is_live = eng.is_live() if eng is not None else False
        recent = (
            eng.get_recent_emissions(window_minutes=60, now_utc=now_utc)
            if eng is not None
            else []
        )
        self.observed_engine_lookups.append(
            (candidate.pair, is_live, len(recent))
        )
        self.allow_calls.append(
            {
                "candidate": candidate,
                "positions": positions,
                "account": account,
                "market": market,
                "now_utc": now_utc,
            }
        )
        if self.allow_result is not None:
            return self.allow_result
        return _Decision(allow=True, rule="ok", reason="", debug={})

    def positions_to_force_close(self, *, positions, now_utc):
        # Touch the engine per position-pair so a stale engine surfaces.
        for pos in positions:
            eng = self._resolve_engine(pos.pair)
            if eng is not None:
                eng.is_live()
        self.force_close_calls.append(
            {"positions": positions, "now_utc": now_utc}
        )
        return list(self.force_close_result)


@dataclass(frozen=True)
class _Decision:
    allow: bool
    rule: str
    reason: str
    debug: dict


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _candle(pair: str, offset_min: int = 0, **overrides) -> Candle:
    defaults = dict(
        pair=pair,
        close_time=_NOW + timedelta(minutes=5 * offset_min),
        open=1.3000,
        high=1.3010,
        low=1.2990,
        close=1.3005,
        volume=200.0,
        source="LS_NATIVE_5M",
    )
    defaults.update(overrides)
    return Candle(**defaults)


def _bar_close(pair: str, offset_min: int = 0, **debug) -> FeedEvent:
    return FeedEvent(
        kind=FeedEventKind.BAR_CLOSE,
        pair=pair,
        candle=_candle(pair, offset_min),
        timestamp=_NOW,
        debug=debug,
    )


def _build(
    monkeypatch,
    *,
    pairs=("GBPUSD",),
    clock=None,
    regime_engines=None,
) -> tuple[BotLoop, dict]:
    """Construct a BotLoop with all fakes injected, plus a `pieces` dict for assertions.

    ``regime_engines`` lets tests pre-seed the per-pair engine map so
    they can reach into the same instance the BotLoop uses (identity
    asserts) and also pass the corresponding ``engine_for_pair`` lambda
    to ``_FakeRiskGuard``.
    """
    feed = _FakeFeed()
    # Seed with enough candles that resample produces an H1.
    seed = [_candle("GBPUSD", -i) for i in range(60, 0, -1)]
    for pair in pairs:
        feed.add_buffer(pair, seed)

    ig = _FakeIGClient()
    executor = _FakeExecutor()
    pm = _FakePositionManager()

    # Build per-pair engines if not supplied — these become BOTH the
    # BotLoop's internal engines AND the _FakeRiskGuard's routing
    # source. This pairs with the C2 fix: identity must be shared.
    from regime.engine import RegimeEngine
    engines = (
        dict(regime_engines)
        if regime_engines is not None
        else {p: RegimeEngine() for p in pairs}
    )
    rg = _FakeRiskGuard(engine_for_pair=lambda pair: engines[pair])

    # Neutralise fetch_market_info to avoid touching the IG layer.
    import bot.loop as loop_mod
    monkeypatch.setattr(
        loop_mod, "fetch_market_info",
        lambda session, epic: type("MI", (), {"bid": 1.30000, "offer": 1.30020})(),
    )

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
    )
    return bot, {
        "feed": feed, "ig": ig, "executor": executor,
        "positions": pm, "risk": rg, "engines": engines,
    }


# ---------------------------------------------------------------------------
# Lifecycle & state machine
# ---------------------------------------------------------------------------


def test_initial_state_is_starting(monkeypatch) -> None:
    bot, _ = _build(monkeypatch)
    assert bot.state == BotState.STARTING


def test_start_keeps_state_in_starting_until_mark_ready(monkeypatch) -> None:
    """H4 (adversarial review 2026-05-15): start() must not flip to NORMAL.

    The state transition only happens after the caller has verified
    subscriptions and explicitly calls mark_ready(). This guards the
    window where the bot would otherwise report NORMAL while LS
    subscriptions could still be settling or failing.
    """
    bot, pieces = _build(monkeypatch)
    bot.start()
    assert bot.state == BotState.STARTING
    assert pieces["feed"].started is True
    assert len(pieces["feed"].callbacks) == 1


def test_mark_ready_transitions_to_normal(monkeypatch) -> None:
    bot, pieces = _build(monkeypatch)
    bot.start()
    bot.mark_ready()
    assert bot.state == BotState.NORMAL


def test_mark_ready_is_idempotent_and_no_op_after_transition(monkeypatch) -> None:
    bot, _ = _build(monkeypatch)
    bot.start()
    bot.mark_ready()
    # Second call doesn't flip back or raise.
    bot.mark_ready()
    assert bot.state == BotState.NORMAL


def test_event_during_starting_is_dropped(monkeypatch) -> None:
    """M6: events arriving in STARTING are short-circuited."""
    bot, pieces = _build(monkeypatch)
    bot.start()
    # No mark_ready — bot is in STARTING.
    pieces["feed"].fire(_bar_close("GBPUSD"))
    # Risk path never touched.
    assert pieces["risk"].force_close_calls == []


def test_feed_stale_transitions_to_stale(monkeypatch) -> None:
    bot, pieces = _build(monkeypatch)
    bot.start()
    bot.mark_ready()
    pieces["feed"].fire(FeedEvent(
        kind=FeedEventKind.FEED_STALE, pair="*", candle=None, timestamp=_NOW,
    ))
    assert bot.state == BotState.STALE


def test_feed_resumed_transitions_to_resuming(monkeypatch) -> None:
    bot, pieces = _build(monkeypatch)
    bot.start()
    bot.mark_ready()
    pieces["feed"].fire(FeedEvent(
        kind=FeedEventKind.FEED_STALE, pair="*", candle=None, timestamp=_NOW,
    ))
    pieces["feed"].fire(FeedEvent(
        kind=FeedEventKind.FEED_RESUMED, pair="*", candle=None, timestamp=_NOW,
    ))
    assert bot.state == BotState.RESUMING


def test_gap_filled_returns_to_normal(monkeypatch) -> None:
    bot, pieces = _build(monkeypatch)
    bot.start()
    bot.mark_ready()
    for kind in (FeedEventKind.FEED_STALE, FeedEventKind.FEED_RESUMED, FeedEventKind.GAP_FILLED):
        pieces["feed"].fire(FeedEvent(
            kind=kind, pair="*", candle=None, timestamp=_NOW,
        ))
    assert bot.state == BotState.NORMAL


def test_resuming_to_normal_on_live_bar_close_no_gap_fill(monkeypatch) -> None:
    """Edge case: no GAP_FILLED arrives (no gap, or gap > window)."""
    bot, pieces = _build(monkeypatch)
    bot.start()
    bot.mark_ready()
    pieces["feed"].fire(FeedEvent(
        kind=FeedEventKind.FEED_STALE, pair="*", candle=None, timestamp=_NOW,
    ))
    pieces["feed"].fire(FeedEvent(
        kind=FeedEventKind.FEED_RESUMED, pair="*", candle=None, timestamp=_NOW,
    ))
    # First live BAR_CLOSE after RESUMING (no gap_fill_backfill reason).
    pieces["feed"].fire(_bar_close("GBPUSD"))
    assert bot.state == BotState.NORMAL


def test_gap_fill_bar_close_does_not_clear_resuming(monkeypatch) -> None:
    """A gap-fill BAR_CLOSE during RESUMING should NOT transition."""
    bot, pieces = _build(monkeypatch)
    bot.start()
    bot.mark_ready()
    pieces["feed"].fire(FeedEvent(
        kind=FeedEventKind.FEED_STALE, pair="*", candle=None, timestamp=_NOW,
    ))
    pieces["feed"].fire(FeedEvent(
        kind=FeedEventKind.FEED_RESUMED, pair="*", candle=None, timestamp=_NOW,
    ))
    pieces["feed"].fire(_bar_close("GBPUSD", reason="gap_fill_backfill"))
    assert bot.state == BotState.RESUMING


# ---------------------------------------------------------------------------
# Signal-pipeline gating
# ---------------------------------------------------------------------------


def test_gap_fill_bar_close_skips_signal_pipeline(monkeypatch) -> None:
    """Indicators run on gap-fill, but signals/executor must not fire."""
    bot, pieces = _build(monkeypatch)
    bot.start()
    bot.mark_ready()
    pieces["feed"].fire(_bar_close("GBPUSD", reason="gap_fill_backfill"))
    # Risk + executor never invoked.
    assert pieces["risk"].allow_calls == []
    assert pieces["executor"].opened == []


def test_stale_state_skips_signals(monkeypatch) -> None:
    bot, pieces = _build(monkeypatch)
    bot.start()
    bot.mark_ready()
    pieces["feed"].fire(FeedEvent(
        kind=FeedEventKind.FEED_STALE, pair="*", candle=None, timestamp=_NOW,
    ))
    pieces["feed"].fire(_bar_close("GBPUSD"))
    assert pieces["risk"].allow_calls == []
    assert pieces["executor"].opened == []


# ---------------------------------------------------------------------------
# Periodic tasks (inline scheduler)
# ---------------------------------------------------------------------------


def test_reconciliation_fires_on_first_bar_after_threshold(monkeypatch) -> None:
    """First BAR_CLOSE within 10 min of construction → no reconciliation."""
    fake_now = [_NOW]
    bot, pieces = _build(monkeypatch, clock=lambda: fake_now[0])
    bot.start()
    bot.mark_ready()
    pieces["feed"].fire(_bar_close("GBPUSD"))
    assert pieces["ig"].fetch_open_calls == 0
    # Advance past the 10-minute threshold and fire again.
    fake_now[0] = _NOW + timedelta(minutes=11)
    pieces["feed"].fire(_bar_close("GBPUSD", offset_min=2))
    assert pieces["ig"].fetch_open_calls == 1


def test_reconciliation_fires_at_exact_threshold(monkeypatch) -> None:
    fake_now = [_NOW]
    bot, pieces = _build(monkeypatch, clock=lambda: fake_now[0])
    bot.start()
    bot.mark_ready()
    fake_now[0] = _NOW + timedelta(minutes=10)
    pieces["feed"].fire(_bar_close("GBPUSD"))
    assert pieces["ig"].fetch_open_calls == 1


def test_force_close_called_on_every_bar_close(monkeypatch) -> None:
    """RiskGuard.positions_to_force_close is called every BAR_CLOSE.

    Phase 4 owns the "fire once per day" guard — we just hand it the
    decision.
    """
    bot, pieces = _build(monkeypatch)
    bot.start()
    bot.mark_ready()
    pieces["feed"].fire(_bar_close("GBPUSD"))
    pieces["feed"].fire(_bar_close("GBPUSD", offset_min=1))
    assert len(pieces["risk"].force_close_calls) == 2


def test_force_close_order_executes_close(monkeypatch) -> None:
    from regime.labels import Direction, RegimeLabel
    from execution.types import ExecutionPosition

    bot, pieces = _build(monkeypatch)
    pos = ExecutionPosition(
        deal_id="D1", deal_reference="R1", pair="GBPUSD",
        direction=Direction.BULLISH, regime_at_entry=RegimeLabel.TREND,
        strategy_name="ema_continuation",
        size_units=1.0, entry_price=1.30, initial_sl_price=1.298,
        current_sl_price=1.298, suggested_tp_price=None,
        entry_time_utc=_NOW, signal_source_candle_ts=_NOW,
    )
    pieces["positions"].upsert(pos)

    from risk.types import ForceCloseOrder
    pieces["risk"].force_close_result = [
        ForceCloseOrder(position_id="D1", pair="GBPUSD", reason="eod_ny_close")
    ]
    bot.start()
    bot.mark_ready()
    pieces["feed"].fire(_bar_close("GBPUSD"))
    # Broker close request landed.
    assert len(pieces["ig"].close_calls) == 1
    assert pieces["ig"].close_calls[0].deal_id == "D1"
    # Position was removed from local state.
    assert pieces["positions"].get("D1") is None


def test_force_close_passes_position_own_direction_bullish(monkeypatch) -> None:
    """C1 regression: BULLISH position closes with position_direction="BUY"."""
    from regime.labels import Direction, RegimeLabel
    from execution.types import ExecutionPosition
    from risk.types import ForceCloseOrder

    bot, pieces = _build(monkeypatch)
    pieces["positions"].upsert(ExecutionPosition(
        deal_id="D_BULL", deal_reference="R", pair="GBPUSD",
        direction=Direction.BULLISH, regime_at_entry=RegimeLabel.TREND,
        strategy_name="ema_continuation",
        size_units=1.0, entry_price=1.30, initial_sl_price=1.298,
        current_sl_price=1.298, suggested_tp_price=None,
        entry_time_utc=_NOW, signal_source_candle_ts=_NOW,
    ))
    pieces["risk"].force_close_result = [
        ForceCloseOrder(position_id="D_BULL", pair="GBPUSD", reason="eod"),
    ]
    bot.start()
    bot.mark_ready()
    pieces["feed"].fire(_bar_close("GBPUSD"))
    # The wrapper inverts internally — Phase 8 must pass the position's
    # OWN direction. For a BULLISH (BUY) position, position_direction
    # must be "BUY", not the inverted "SELL".
    assert pieces["ig"].close_calls[0].position_direction == "BUY"


def test_force_close_passes_position_own_direction_bearish(monkeypatch) -> None:
    """C1 regression: BEARISH position closes with position_direction="SELL"."""
    from regime.labels import Direction, RegimeLabel
    from execution.types import ExecutionPosition
    from risk.types import ForceCloseOrder

    bot, pieces = _build(monkeypatch)
    pieces["positions"].upsert(ExecutionPosition(
        deal_id="D_BEAR", deal_reference="R", pair="GBPUSD",
        direction=Direction.BEARISH, regime_at_entry=RegimeLabel.TREND,
        strategy_name="ema_continuation",
        size_units=1.0, entry_price=1.30, initial_sl_price=1.302,
        current_sl_price=1.302, suggested_tp_price=None,
        entry_time_utc=_NOW, signal_source_candle_ts=_NOW,
    ))
    pieces["risk"].force_close_result = [
        ForceCloseOrder(position_id="D_BEAR", pair="GBPUSD", reason="eod"),
    ]
    bot.start()
    bot.mark_ready()
    pieces["feed"].fire(_bar_close("GBPUSD"))
    assert pieces["ig"].close_calls[0].position_direction == "SELL"


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


def test_periodic_failure_path_does_not_bump_event_counter(monkeypatch) -> None:
    """A force-close-path exception bumps periodic_failures only.

    The handler swallows it (logs + counts on periodic) so
    event_failures stays clean. Two-counter separation is load-bearing
    — see bot.constants for the rationale.
    """
    bot, pieces = _build(monkeypatch)
    bot.start()
    bot.mark_ready()
    pieces["risk"].positions_to_force_close = lambda **kw: (_ for _ in ()).throw(  # type: ignore[assignment]
        RuntimeError("simulated failure")
    )
    pieces["feed"].fire(_bar_close("GBPUSD"))
    assert bot.event_failures.consecutive == 0
    assert bot.periodic_failures.consecutive == 1


def test_periodic_failure_threshold_trips_shutdown(monkeypatch) -> None:
    bot, pieces = _build(monkeypatch)
    pieces["risk"].positions_to_force_close = lambda **kw: (_ for _ in ()).throw(  # type: ignore[assignment]
        RuntimeError("boom")
    )
    bot.start()
    bot.mark_ready()
    for i in range(6):
        pieces["feed"].fire(_bar_close("GBPUSD", offset_min=i))
    assert bot.state == BotState.SHUTTING_DOWN
    assert bot.crashed


def test_event_failure_path_handles_completely_broken_event(monkeypatch) -> None:
    """If the event dispatch itself crashes (not just periodic), event counter trips."""
    bot, pieces = _build(monkeypatch)
    bot.start()
    bot.mark_ready()
    # Force the buffer to raise when to_dataframe is called.
    feed: _FakeFeed = pieces["feed"]
    class _BrokenBuffer:
        def to_dataframe(self):
            raise RuntimeError("buffer corrupt")
        def latest(self):
            return None
    feed._buffers["GBPUSD"] = _BrokenBuffer()  # type: ignore[assignment]
    for i in range(6):
        pieces["feed"].fire(_bar_close("GBPUSD", offset_min=i))
    assert bot.event_failures.consecutive >= 5
    assert bot.state == BotState.SHUTTING_DOWN


# ---------------------------------------------------------------------------
# Shutdown drain
# ---------------------------------------------------------------------------


def test_request_shutdown_sets_state_and_event(monkeypatch) -> None:
    bot, _ = _build(monkeypatch)
    bot.start()
    bot.mark_ready()
    bot.request_shutdown()
    assert bot.state == BotState.SHUTTING_DOWN
    assert bot.shutdown_event().is_set()


def test_stop_drains_inflight_before_disconnect(monkeypatch) -> None:
    """Verify the shutdown drain blocks until in-flight counter hits 0."""
    bot, pieces = _build(monkeypatch)
    bot.start()
    bot.mark_ready()
    # Manually bump the in-flight counter as if a broker call were live.
    with bot._inflight_lock:  # type: ignore[attr-defined]
        bot._inflight_count = 1  # type: ignore[attr-defined]

    fake_now = [_NOW]
    drain_finished = threading.Event()

    def _drain_in_thread():
        bot.stop(inflight_timeout_sec=0.5, poll_sec=0.05)
        drain_finished.set()

    t = threading.Thread(target=_drain_in_thread)
    t.start()
    # Drain should NOT finish yet — counter is still 1.
    drain_finished.wait(timeout=0.1)
    assert not drain_finished.is_set()
    # Decrement → notify → drain completes.
    with bot._inflight_lock:  # type: ignore[attr-defined]
        bot._inflight_count = 0  # type: ignore[attr-defined]
        bot._inflight_zero.notify_all()  # type: ignore[attr-defined]
    t.join(timeout=1.0)
    assert drain_finished.is_set()
    assert pieces["feed"].stopped is True
    assert pieces["positions"].saved == 1


def test_stop_proceeds_past_inflight_timeout(monkeypatch) -> None:
    """If broker calls hang past the timeout, drain logs CRITICAL and proceeds."""
    bot, pieces = _build(monkeypatch)
    bot.start()
    bot.mark_ready()
    with bot._inflight_lock:  # type: ignore[attr-defined]
        bot._inflight_count = 2  # type: ignore[attr-defined]
    bot.stop(inflight_timeout_sec=0.05, poll_sec=0.01)
    # Despite the timeout, feed_manager.stop() still ran.
    assert pieces["feed"].stopped is True


def test_handler_short_circuits_when_shutting_down(monkeypatch) -> None:
    bot, pieces = _build(monkeypatch)
    bot.start()
    bot.mark_ready()
    bot.request_shutdown()
    # Fire a BAR_CLOSE after shutdown request — must NOT touch risk.
    pieces["feed"].fire(_bar_close("GBPUSD"))
    assert pieces["risk"].force_close_calls == []


# ---------------------------------------------------------------------------
# C2 — RiskGuard / BotLoop regime engine sharing
# ---------------------------------------------------------------------------


def test_risk_guard_routes_to_correct_pair_engine(monkeypatch) -> None:
    """C2 regression: a GBPUSD signal queries the GBPUSD engine, not EURUSD.

    Both engines exist (per-pair map); the routing path is
    engine_for_pair(candidate.pair). The fake's observed_engine_lookups
    records (pair, is_live, recent_count) for each allow_entry call —
    the test asserts the pair matches the candidate.
    """
    from regime.engine import RegimeEngine
    engines = {"GBPUSD": RegimeEngine(), "EURUSD": RegimeEngine()}
    bot, pieces = _build(
        monkeypatch, pairs=("GBPUSD", "EURUSD"), regime_engines=engines,
    )
    bot.start()
    bot.mark_ready()

    # Manually trigger a signal pipeline run with a known pair by
    # invoking _evaluate_and_execute via a constructed Signal. (We
    # don't fire a real BAR_CLOSE because the dispatcher would
    # otherwise return [] in the absence of a real strategy setup.)
    from strategies.signal import Signal
    from regime.labels import Direction, RegimeLabel
    sig = Signal(
        pair="GBPUSD",
        direction=Direction.BULLISH,
        regime=RegimeLabel.TREND,
        strategy_name="ema_continuation",
        suggested_entry_price=1.3,
        suggested_sl_price=1.298,
        suggested_tp_price=None,
        confidence_score=0.5,
        source_candle_ts=_NOW,
        invalid_after_candle_ts=_NOW + timedelta(minutes=5),
        debug={},
    )
    bot._evaluate_and_execute(sig)  # type: ignore[attr-defined]
    # The fake records (pair, is_live, recent_count) per call.
    assert len(pieces["risk"].observed_engine_lookups) == 1
    routed_pair, _, _ = pieces["risk"].observed_engine_lookups[0]
    assert routed_pair == "GBPUSD"


def test_bot_loop_and_risk_guard_share_engine_instance_identity(
    monkeypatch,
) -> None:
    """C2 regression: identity check, not just behaviour.

    Every prior-phase test verified outcomes; this one verifies the
    wiring. The same RegimeEngine instance must be reachable from
    both BotLoop.regime_engine_for and the engine_for_pair callable
    the FakeRiskGuard was constructed with.
    """
    from regime.engine import RegimeEngine
    engines = {"GBPUSD": RegimeEngine()}
    bot, pieces = _build(monkeypatch, regime_engines=engines)
    # BotLoop's per-pair engine IS the dict entry.
    assert bot.regime_engine_for("GBPUSD") is engines["GBPUSD"]
    # The FakeRiskGuard was wired with engine_for_pair=lambda p: engines[p].
    # Resolve via its accessor and confirm same instance.
    resolved = pieces["risk"]._engine_for_pair("GBPUSD")
    assert resolved is engines["GBPUSD"]


def test_risk_guard_falls_back_to_single_engine_when_callable_not_provided() -> None:
    """RiskGuard backward compat: legacy single-engine form still works."""
    from risk.guard import RiskGuard
    from risk.state.circuit_breaker_state import CircuitBreakerState
    from regime.engine import RegimeEngine
    engine = RegimeEngine()
    rg = RiskGuard(engine=engine, state=CircuitBreakerState())
    # engine property returns the legacy reference.
    assert rg.engine is engine
    # Resolver routes any pair to the single engine.
    assert rg._resolve_engine("GBPUSD") is engine  # type: ignore[attr-defined]
    assert rg._resolve_engine("EURUSD") is engine  # type: ignore[attr-defined]


def test_risk_guard_rejects_construction_without_any_engine() -> None:
    from risk.guard import RiskGuard
    with pytest.raises(ValueError, match="engine"):
        RiskGuard()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# H1 — H1 dataframe excludes partial trailing bar
# ---------------------------------------------------------------------------


def test_h1_dataframe_excludes_partial_trailing_bar_mid_hour(monkeypatch) -> None:
    """H1 regression: mid-hour M5 close trims the forming H1.

    At a non-zero-minute M5 close (e.g. 13:35), the resample produces
    an H1 bin labelled 14:00 with only some of the expected M5
    contributions. _derive_and_enrich_h1 must drop it so strategies
    only see fully-closed H1 bars.
    """
    bot, _ = _build(monkeypatch)
    # Use the test seam — at a 13:35 close (minute=35), the forming
    # 14:00 H1 should be trimmed.
    mid_hour = datetime(2026, 5, 15, 13, 35, tzinfo=timezone.utc)
    df_h1 = bot._h1_for_test("GBPUSD", m5_close_time=mid_hour)  # type: ignore[attr-defined]
    if not df_h1.empty:
        # The last H1 timestamp must be strictly less than the
        # forming 14:00 bin label.
        assert df_h1.index[-1] < datetime(
            2026, 5, 15, 14, 0, tzinfo=timezone.utc,
        )


def test_h1_dataframe_includes_just_closed_h1_on_boundary(monkeypatch) -> None:
    """On a 13:00 M5 close (minute=0), the 13:00 H1 just closed — keep it."""
    bot, _ = _build(monkeypatch)
    on_boundary = datetime(2026, 5, 15, 13, 0, tzinfo=timezone.utc)
    df_h1 = bot._h1_for_test("GBPUSD", m5_close_time=on_boundary)  # type: ignore[attr-defined]
    # No trimming — every bar in the resample is genuinely closed.
    # We just verify the function returns something (the test fixture
    # seeds enough M5s to produce at least one H1).
    assert not df_h1.empty


# ---------------------------------------------------------------------------
# H3 — force-close success counter resets
# ---------------------------------------------------------------------------


def test_periodic_failures_reset_on_clean_force_close(monkeypatch) -> None:
    """H3 regression: a clean force-close run resets _periodic_failures."""
    bot, pieces = _build(monkeypatch)
    bot.start()
    bot.mark_ready()
    # Inject a failure into the counter manually (simulating one prior
    # flaky reconcile that didn't trip the threshold).
    bot.periodic_failures.record_failure(
        RuntimeError("flaky earlier"), now_utc=_NOW,
    )
    assert bot.periodic_failures.consecutive == 1
    # Now fire a BAR_CLOSE — force-close has nothing to do (positions
    # is empty, orders returns []) and reconcile won't run because the
    # 10-min threshold hasn't elapsed.
    pieces["feed"].fire(_bar_close("GBPUSD"))
    # The clean no-op force-close path must have reset the counter.
    assert bot.periodic_failures.consecutive == 0


# ---------------------------------------------------------------------------
# M2 — BAR_UPDATE no-op doesn't reset event counter
# ---------------------------------------------------------------------------


def test_bar_update_does_not_reset_event_failure_counter(monkeypatch) -> None:
    """M2 regression: only BAR_CLOSE work resets event_failures."""
    bot, pieces = _build(monkeypatch)
    bot.start()
    bot.mark_ready()
    bot.event_failures.record_failure(RuntimeError("earlier"), now_utc=_NOW)
    assert bot.event_failures.consecutive == 1
    # Fire a BAR_UPDATE — no-op kind, must NOT reset the counter.
    pieces["feed"].fire(FeedEvent(
        kind=FeedEventKind.BAR_UPDATE,
        pair="GBPUSD",
        candle=_candle("GBPUSD"),
        timestamp=_NOW,
    ))
    assert bot.event_failures.consecutive == 1, (
        "BAR_UPDATE should not reset event_failures"
    )
    # And status events also don't reset.
    pieces["feed"].fire(FeedEvent(
        kind=FeedEventKind.FEED_STALE, pair="*", candle=None, timestamp=_NOW,
    ))
    assert bot.event_failures.consecutive == 1
