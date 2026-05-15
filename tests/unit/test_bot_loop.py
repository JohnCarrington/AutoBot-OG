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
    def __init__(self) -> None:
        self.allow_calls: list = []
        self.force_close_calls: list = []
        self.allow_result = None  # set per test
        self.force_close_result: list = []

    def allow_entry(self, **kw):
        self.allow_calls.append(kw)
        if self.allow_result is not None:
            return self.allow_result
        return _Decision(allow=True, rule="ok", reason="", debug={})

    def positions_to_force_close(self, **kw):
        self.force_close_calls.append(kw)
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


def _build(monkeypatch, *, pairs=("GBPUSD",), clock=None) -> tuple[BotLoop, dict]:
    """Construct a BotLoop with all fakes injected, plus a `pieces` dict for assertions."""
    feed = _FakeFeed()
    # Seed with enough candles that resample produces an H1.
    seed = [_candle("GBPUSD", -i) for i in range(60, 0, -1)]
    for pair in pairs:
        feed.add_buffer(pair, seed)

    ig = _FakeIGClient()
    executor = _FakeExecutor()
    pm = _FakePositionManager()
    rg = _FakeRiskGuard()

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
        clock=clock or (lambda: _NOW),
    )
    return bot, {
        "feed": feed, "ig": ig, "executor": executor,
        "positions": pm, "risk": rg,
    }


# ---------------------------------------------------------------------------
# Lifecycle & state machine
# ---------------------------------------------------------------------------


def test_initial_state_is_starting(monkeypatch) -> None:
    bot, _ = _build(monkeypatch)
    assert bot.state == BotState.STARTING


def test_start_registers_callback_and_transitions_to_normal(monkeypatch) -> None:
    bot, pieces = _build(monkeypatch)
    bot.start()
    assert bot.state == BotState.NORMAL
    assert pieces["feed"].started is True
    assert len(pieces["feed"].callbacks) == 1


def test_feed_stale_transitions_to_stale(monkeypatch) -> None:
    bot, pieces = _build(monkeypatch)
    bot.start()
    pieces["feed"].fire(FeedEvent(
        kind=FeedEventKind.FEED_STALE, pair="*", candle=None, timestamp=_NOW,
    ))
    assert bot.state == BotState.STALE


def test_feed_resumed_transitions_to_resuming(monkeypatch) -> None:
    bot, pieces = _build(monkeypatch)
    bot.start()
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
    for kind in (FeedEventKind.FEED_STALE, FeedEventKind.FEED_RESUMED, FeedEventKind.GAP_FILLED):
        pieces["feed"].fire(FeedEvent(
            kind=kind, pair="*", candle=None, timestamp=_NOW,
        ))
    assert bot.state == BotState.NORMAL


def test_resuming_to_normal_on_live_bar_close_no_gap_fill(monkeypatch) -> None:
    """Edge case: no GAP_FILLED arrives (no gap, or gap > window)."""
    bot, pieces = _build(monkeypatch)
    bot.start()
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
    pieces["feed"].fire(_bar_close("GBPUSD", reason="gap_fill_backfill"))
    # Risk + executor never invoked.
    assert pieces["risk"].allow_calls == []
    assert pieces["executor"].opened == []


def test_stale_state_skips_signals(monkeypatch) -> None:
    bot, pieces = _build(monkeypatch)
    bot.start()
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
    pieces["feed"].fire(_bar_close("GBPUSD"))
    # Broker close request landed.
    assert len(pieces["ig"].close_calls) == 1
    assert pieces["ig"].close_calls[0].deal_id == "D1"
    # Position was removed from local state.
    assert pieces["positions"].get("D1") is None


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


def test_event_failure_counter_increments_on_exception(monkeypatch) -> None:
    bot, pieces = _build(monkeypatch)
    bot.start()
    # Make RiskGuard.positions_to_force_close raise on the periodic call.
    pieces["risk"].positions_to_force_close = lambda **kw: (_ for _ in ()).throw(  # type: ignore[assignment]
        RuntimeError("simulated failure")
    )
    # We want to count this against the EVENT counter (raised inside
    # _handle_bar_close → _maybe_force_close_orders → wrapped exception
    # path → bumps periodic_failures actually, not event). The handler
    # never re-raises, so event_failures stays clean.
    pieces["feed"].fire(_bar_close("GBPUSD"))
    assert bot.event_failures.consecutive == 0
    assert bot.periodic_failures.consecutive == 1


def test_periodic_failure_threshold_trips_shutdown(monkeypatch) -> None:
    bot, pieces = _build(monkeypatch)
    pieces["risk"].positions_to_force_close = lambda **kw: (_ for _ in ()).throw(  # type: ignore[assignment]
        RuntimeError("boom")
    )
    bot.start()
    for i in range(6):
        pieces["feed"].fire(_bar_close("GBPUSD", offset_min=i))
    assert bot.state == BotState.SHUTTING_DOWN
    assert bot.crashed


def test_event_failure_path_handles_completely_broken_event(monkeypatch) -> None:
    """If the event dispatch itself crashes (not just periodic), event counter trips."""
    bot, pieces = _build(monkeypatch)
    bot.start()
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
    bot.request_shutdown()
    assert bot.state == BotState.SHUTTING_DOWN
    assert bot.shutdown_event().is_set()


def test_stop_drains_inflight_before_disconnect(monkeypatch) -> None:
    """Verify the shutdown drain blocks until in-flight counter hits 0."""
    bot, pieces = _build(monkeypatch)
    bot.start()
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
    with bot._inflight_lock:  # type: ignore[attr-defined]
        bot._inflight_count = 2  # type: ignore[attr-defined]
    bot.stop(inflight_timeout_sec=0.05, poll_sec=0.01)
    # Despite the timeout, feed_manager.stop() still ran.
    assert pieces["feed"].stopped is True


def test_handler_short_circuits_when_shutting_down(monkeypatch) -> None:
    bot, pieces = _build(monkeypatch)
    bot.start()
    bot.request_shutdown()
    # Fire a BAR_CLOSE after shutdown request — must NOT touch risk.
    pieces["feed"].fire(_bar_close("GBPUSD"))
    assert pieces["risk"].force_close_calls == []
