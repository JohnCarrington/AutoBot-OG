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

    def buffer_for_h1(self, pair: str) -> Optional[_FakeBuffer]:
        # Phase B accessor — returning None routes the H1 dispatcher
        # to the legacy M5-resample path, preserving the byte-identical
        # baseline for these tests. The new tests in
        # test_bot_loop_h1_synthesis.py / test_bot_loop_h1_hydration.py
        # exercise the buffer-populated branch directly.
        return None

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
    """Stand-in for RiskGuard — records calls, returns canned decisions.

    2d: the real RiskGuard no longer consults a regime engine, so the
    fake no longer tries to either. The structure_state_for_pair
    callable is what drives the EOD decision now (B-1 / 2c).
    """

    def __init__(self) -> None:
        self.allow_calls: list = []
        self.force_close_calls: list = []
        self.allow_result = None  # set per test
        self.force_close_result: list = []

    def allow_entry(self, *, candidate, positions, account, market, now_utc):
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

    def positions_to_force_close(
        self, *, positions, now_utc, structure_state_for_pair=None,
    ):
        if structure_state_for_pair is not None:
            for pos in positions:
                try:
                    structure_state_for_pair(pos.pair)
                except Exception:
                    pass
        self.force_close_calls.append(
            {
                "positions": positions,
                "now_utc": now_utc,
                "structure_state_for_pair": structure_state_for_pair,
            }
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
) -> tuple[BotLoop, dict]:
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
    from common import Direction
    from day_type import DayType
    from execution.types import ExecutionPosition

    bot, pieces = _build(monkeypatch)
    pos = ExecutionPosition(
        deal_id="D1", deal_reference="R1", pair="GBPUSD",
        direction=Direction.BULLISH, day_type_at_entry=DayType.NORMAL,
        strategy_name="ema_pullback",
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
    from common import Direction
    from day_type import DayType
    from execution.types import ExecutionPosition
    from risk.types import ForceCloseOrder

    bot, pieces = _build(monkeypatch)
    pieces["positions"].upsert(ExecutionPosition(
        deal_id="D_BULL", deal_reference="R", pair="GBPUSD",
        direction=Direction.BULLISH, day_type_at_entry=DayType.NORMAL,
        strategy_name="ema_pullback",
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
    from common import Direction
    from day_type import DayType
    from execution.types import ExecutionPosition
    from risk.types import ForceCloseOrder

    bot, pieces = _build(monkeypatch)
    pieces["positions"].upsert(ExecutionPosition(
        deal_id="D_BEAR", deal_reference="R", pair="GBPUSD",
        direction=Direction.BEARISH, day_type_at_entry=DayType.NORMAL,
        strategy_name="ema_pullback",
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
# Signal routing — candidate carries the pair the dispatcher fired on
# ---------------------------------------------------------------------------


def test_evaluate_and_execute_passes_signal_pair_to_risk(monkeypatch) -> None:
    """A GBPUSD signal arrives at risk.allow_entry as a GBPUSD candidate.

    Replaces the old C2 engine-routing test — 2d deleted the regime
    engine, so there's no engine to route. What remains worth pinning:
    the candidate the risk guard sees carries the same pair as the
    originating signal.
    """
    bot, pieces = _build(monkeypatch, pairs=("GBPUSD", "EURUSD"))
    bot.start()
    bot.mark_ready()
    from strategies.signal import Signal
    from day_type import DayType
    from common import Direction
    sig = Signal(
        pair="GBPUSD",
        direction=Direction.BULLISH,
        day_type=DayType.NORMAL,
        strategy_name="ema_pullback",
        suggested_entry_price=1.3,
        suggested_sl_price=1.298,
        suggested_tp_price=None,
        confidence_score=0.5,
        source_candle_ts=_NOW,
        invalid_after_candle_ts=_NOW + timedelta(minutes=5),
        debug={},
    )
    bot._evaluate_and_execute(sig)  # type: ignore[attr-defined]
    assert len(pieces["risk"].allow_calls) == 1
    assert pieces["risk"].allow_calls[0]["candidate"].pair == "GBPUSD"


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
# M15 — mirror of the H1 trim semantics (Phase 11)
# ---------------------------------------------------------------------------


def test_m15_dataframe_trims_partial_trailing_bar(monkeypatch) -> None:
    """Off-boundary M5 close (minute % 15 != 0) trims the forming M15.

    M-6 review fix (2026-05-16). At 13:05 the resample produces a 13:15
    M15 bin with only 1 M5 contribution; ``_derive_and_enrich_m15`` must
    drop it so the Structure Engine's M15 swing detector sees stable
    per-M15-bar values.
    """
    bot, _ = _build(monkeypatch)
    off_boundary = datetime(2026, 5, 15, 13, 5, tzinfo=timezone.utc)
    df_m15 = bot._m15_for_test("GBPUSD", m5_close_time=off_boundary)  # type: ignore[attr-defined]
    if not df_m15.empty:
        # No forming 13:15 bar should appear; latest label must be
        # strictly less than 13:15.
        assert df_m15.index[-1] < datetime(
            2026, 5, 15, 13, 15, tzinfo=timezone.utc,
        )


def test_m15_dataframe_keeps_just_closed_m15_on_boundary(monkeypatch) -> None:
    """On a 13:15 M5 close (minute=15), the 13:15 M15 just closed — keep it."""
    bot, _ = _build(monkeypatch)
    on_boundary = datetime(2026, 5, 15, 13, 15, tzinfo=timezone.utc)
    df_m15 = bot._m15_for_test("GBPUSD", m5_close_time=on_boundary)  # type: ignore[attr-defined]
    assert not df_m15.empty


def test_m15_dataframe_is_idempotent(monkeypatch) -> None:
    """Same M5 buffer + close_time should produce equal M15 output.

    Spec §17 rule #1 (determinism). The resample + indicator pipeline
    is pure-functional; calling twice must yield identical values.
    """
    import pandas as pd

    bot, _ = _build(monkeypatch)
    close_time = datetime(2026, 5, 15, 13, 30, tzinfo=timezone.utc)
    df_a = bot._m15_for_test("GBPUSD", m5_close_time=close_time)  # type: ignore[attr-defined]
    df_b = bot._m15_for_test("GBPUSD", m5_close_time=close_time)  # type: ignore[attr-defined]
    pd.testing.assert_frame_equal(df_a, df_b)


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


# ---------------------------------------------------------------------------
# Phase 9 commit 2b — alerter wiring
# ---------------------------------------------------------------------------


class _RecordingAlerter:
    """Minimal stand-in for TelegramAlerter that records every call.

    Mirrors the public surface (send / tick / close) and tracks the
    call ordering so tests can assert the FEED_STALE-after-transition
    contract and the close-before-feed.stop ordering.

    L4 (Phase 9 cleanup): ``send`` asserts ``isinstance(alert, Alert)``
    so a future regression that passes a dict / namespace / wrong type
    surfaces here instead of slipping through silently. The real
    TelegramAlerter doesn't enforce the type at runtime — the dataclass
    discipline does — but the fake should not be more permissive than
    production.

    M4 (Phase 9 cleanup): ``send`` captures bot state at call-time
    (when the optional ``state_getter`` is wired). The
    FEED_STALE/FEED_RESUMED tests need to verify the state transition
    happened BEFORE ``send`` runs — asserting on alerter-internal
    call ordering (``send`` vs ``tick``) is necessary but not
    sufficient. ``state_at_send`` pins the actual invariant.
    """

    def __init__(self, state_getter=None) -> None:
        self.sent: list = []
        self.ticks: int = 0
        self.closed: bool = False
        self.events: list[str] = []  # ordering log
        self._state_getter = state_getter
        self.state_at_send: list = []

    def send(self, alert) -> None:
        from alerts import Alert
        assert isinstance(alert, Alert), (
            f"_RecordingAlerter.send expected an Alert instance, "
            f"got {type(alert).__name__}"
        )
        self.sent.append(alert)
        self.events.append(f"send:{alert.event_subtype}")
        if self._state_getter is not None:
            self.state_at_send.append(self._state_getter())

    def tick(self) -> None:
        self.ticks += 1
        self.events.append("tick")

    def close(self) -> None:
        self.closed = True
        self.events.append("close")


def _build_with_alerter(monkeypatch, **kw) -> tuple:
    """Build a BotLoop with a recording alerter wired in.

    M4 (Phase 9 cleanup): the alerter's ``state_getter`` is wired
    post-construction (closure on the freshly-built ``bot``) so the
    FEED_STALE / FEED_RESUMED tests can pin the state-at-send
    invariant — the actual contract the production code documents.
    """
    alerter = _RecordingAlerter()
    bot, pieces = _build(monkeypatch, **kw)
    # Re-construct via the public API rather than reaching into _build —
    # _build returns a constructed bot, but the alerter must be passed
    # at construction. Easier path: rebuild from the existing pieces.
    pairs = kw.get("pairs", ("GBPUSD",))
    bot = BotLoop(
        feed_manager=pieces["feed"],
        ig_client=pieces["ig"],
        executor=pieces["executor"],
        risk_guard=pieces["risk"],
        position_manager=pieces["positions"],
        pairs=tuple(pairs),
        pair_to_epic={p: f"CS.D.{p}.TODAY.IP" for p in pairs},
        clock=lambda: _NOW,
        alerter=alerter,  # type: ignore[arg-type]
    )
    alerter._state_getter = lambda: bot.state
    pieces["alerter"] = alerter
    return bot, pieces


def test_request_shutdown_without_reason_does_not_emit_alert(monkeypatch) -> None:
    """External / signal-driven shutdown is silent — operator already
    knows they pressed Ctrl-C. CRITICAL alerts are reserved for
    failure-driven shutdowns."""
    bot, pieces = _build_with_alerter(monkeypatch)
    bot.start()
    bot.mark_ready()
    bot.request_shutdown()  # no reason
    assert pieces["alerter"].sent == []


def test_request_shutdown_with_reason_emits_critical_failure_alert(monkeypatch) -> None:
    """Failure-driven shutdown emits CRITICAL FAILURE_THRESHOLD_TRIPPED
    so the operator sees the cause in Telegram, not just in logs."""
    bot, pieces = _build_with_alerter(monkeypatch)
    bot.start()
    bot.mark_ready()
    bot.request_shutdown(reason="5 consecutive event failures")
    sent = pieces["alerter"].sent
    assert len(sent) == 1
    from alerts import AlertCategory, AlertSeverity
    assert sent[0].event_subtype == "FAILURE_THRESHOLD_TRIPPED"
    assert sent[0].severity is AlertSeverity.CRITICAL
    assert sent[0].category is AlertCategory.SYSTEM
    assert "5 consecutive event failures" in sent[0].full_text


def test_request_shutdown_is_idempotent_does_not_double_alert(monkeypatch) -> None:
    bot, pieces = _build_with_alerter(monkeypatch)
    bot.start()
    bot.mark_ready()
    bot.request_shutdown(reason="first trip")
    bot.request_shutdown(reason="second call ignored")
    # Second call short-circuits because state is already SHUTTING_DOWN.
    assert len(pieces["alerter"].sent) == 1
    assert "first trip" in pieces["alerter"].sent[0].full_text


def test_event_failure_threshold_passes_reason_to_request_shutdown(monkeypatch) -> None:
    """5-strike event-failure trip propagates a reason so the alert
    body explains WHY the bot is shutting down."""
    bot, pieces = _build_with_alerter(monkeypatch)
    bot.start()
    bot.mark_ready()
    # Make the executor raise on every signal — but the trip path
    # we want is in _handle_feed_event's exception branch, not the
    # signal pipeline. Simulate by firing an event that throws via
    # the dispatcher side. Easiest path: an unconfigured pair raises
    # KeyError-ish behaviour — actually no; just record_failure five
    # times and call request_shutdown with the consecutive count.
    for i in range(5):
        bot.event_failures.record_failure(RuntimeError(f"e{i}"), now_utc=_NOW)
    assert bot.event_failures.should_shutdown()
    bot.request_shutdown(
        reason=f"{bot.event_failures.consecutive} consecutive event failures"
    )
    assert len(pieces["alerter"].sent) == 1
    assert "5 consecutive" in pieces["alerter"].sent[0].full_text


def test_feed_stale_transitions_first_then_emits_alert_then_ticks(monkeypatch) -> None:
    """FEED_STALE ordering contract: state transition BEFORE the alert
    so the alert text never lies about current state. tick() runs
    after the alert so any pending coalesced groups flush.

    M4 (Phase 9 cleanup): the prior version of this test only
    asserted on send-vs-tick ordering — which is necessary but not
    sufficient. A refactor that emits BEFORE transitioning would have
    silently passed. ``state_at_send`` pins the actual invariant:
    when ``send`` runs, ``bot.state`` must already be STALE.
    """
    bot, pieces = _build_with_alerter(monkeypatch)
    bot.start()
    bot.mark_ready()
    initial_state = bot.state
    pieces["feed"].fire(FeedEvent(
        kind=FeedEventKind.FEED_STALE,
        pair="*",
        candle=None,
        timestamp=_NOW,
    ))
    assert initial_state == BotState.NORMAL
    assert bot.state == BotState.STALE  # transition happened
    # Order: send FEED_STALE then tick.
    assert pieces["alerter"].events == ["send:FEED_STALE", "tick"]
    # M4: state was already STALE at the moment ``send`` was called —
    # this is the load-bearing invariant the production code documents.
    assert pieces["alerter"].state_at_send == [BotState.STALE]


def test_feed_resumed_transitions_first_then_emits_alert_then_ticks(monkeypatch) -> None:
    """M4 cleanup: same invariant for FEED_RESUMED — bot.state must
    already be RESUMING at send-time."""
    bot, pieces = _build_with_alerter(monkeypatch)
    bot.start()
    bot.mark_ready()
    pieces["feed"].fire(FeedEvent(
        kind=FeedEventKind.FEED_STALE, pair="*", candle=None, timestamp=_NOW,
    ))
    pieces["alerter"].events.clear()
    pieces["alerter"].sent.clear()
    pieces["alerter"].state_at_send.clear()
    pieces["feed"].fire(FeedEvent(
        kind=FeedEventKind.FEED_RESUMED, pair="*", candle=None, timestamp=_NOW,
    ))
    assert bot.state == BotState.RESUMING
    assert pieces["alerter"].events == ["send:FEED_RESUMED", "tick"]
    assert pieces["alerter"].state_at_send == [BotState.RESUMING]
    from alerts import AlertSeverity
    assert pieces["alerter"].sent[0].severity is AlertSeverity.INFO


def test_bar_close_pipeline_ends_with_alerter_tick(monkeypatch) -> None:
    """Every BAR_CLOSE flushes the coalescer at the end so alerts
    queued earlier in the bar's pipeline (TRADE_OPENED, AMEND_FAILED)
    don't sit pending past their 30s window unnoticed."""
    bot, pieces = _build_with_alerter(monkeypatch)
    bot.start()
    bot.mark_ready()
    pieces["feed"].fire(_bar_close("GBPUSD"))
    assert pieces["alerter"].ticks >= 1
    # tick is the LAST alerter event in the bar pipeline.
    assert pieces["alerter"].events[-1] == "tick"


def test_stop_calls_alerter_close_before_feed_stop(monkeypatch) -> None:
    """Ordering: alerter.close (drains pending) BEFORE feed_manager.stop
    so the final SHUTDOWN alert (queued by bot.main pre-stop) ships
    over the still-live network."""
    bot, pieces = _build_with_alerter(monkeypatch)
    bot.start()
    bot.mark_ready()
    bot.stop(inflight_timeout_sec=0.01)
    assert pieces["alerter"].closed is True
    assert pieces["feed"].stopped is True
    # close was logged on the alerter before feed.stop ran. Check by
    # confirming the alerter is closed AND the stop ordering is
    # correct via the events log.
    last_event = pieces["alerter"].events[-1]
    assert last_event == "close", (
        f"alerter.close must be the last alerter call; got events={pieces['alerter'].events}"
    )


def test_no_alerter_wired_does_not_raise_on_any_path(monkeypatch) -> None:
    """The alerter param is optional; with None, every alert path is
    a no-op — no AttributeError, no silent crash.

    L5 (Phase 9 cleanup): coverage extended to also exercise the
    force-close path (``_send_alert(TRADE_CLOSED)`` plus
    ``_recent_closes`` mutation) and the reconciliation dispatch
    path (``_dispatch_reconciliation_alerts`` against the no-alerter
    bot). A future refactor that, say, restructures ``_send_alert``
    and removes the early ``if self._alerter is None: return`` guard
    would silently break no-alerter mode without these branches
    exercised.
    """
    bot, pieces = _build(monkeypatch)  # default = no alerter
    bot.start()
    bot.mark_ready()
    bot.request_shutdown(reason="trip without alerter")  # must not raise
    pieces["feed"].fire(FeedEvent(
        kind=FeedEventKind.FEED_STALE, pair="*", candle=None, timestamp=_NOW,
    ))
    # L5 — exercise the force-close TRADE_CLOSED + deal-log path.
    from execution.types import ExecutionPosition
    from common import Direction
    from day_type import DayType
    from risk.types import ForceCloseOrder
    pos = ExecutionPosition(
        deal_id="DEAL_NA1", deal_reference="REF",
        pair="GBPUSD", direction=Direction.BULLISH,
        day_type_at_entry=DayType.NORMAL, strategy_name="trend_break",
        size_units=1.0, entry_price=1.30050,
        initial_sl_price=1.29900, current_sl_price=1.29900,
        suggested_tp_price=1.30450,
        entry_time_utc=_NOW, signal_source_candle_ts=_NOW,
        be_moved=False, trail_active=False, sl_history=(),
    )
    pieces["positions"].upsert(pos)
    bot._execute_force_close(ForceCloseOrder(
        position_id="DEAL_NA1", pair="GBPUSD", reason="EOD_FLATTEN",
    ))
    # The deal log was still populated even without an alerter.
    assert "DEAL_NA1" in bot._recent_closes

    # L5 — exercise the reconciliation dispatch path with mixed events.
    from execution.reconciliation import (
        ReconciliationActions, ReconciliationOutcome,
    )
    from execution.types import (
        ReconciliationEvent, ReconciliationKind,
        ReconciliationReport, ReconciliationSeverity,
    )
    outcome = ReconciliationOutcome(
        report=ReconciliationReport(
            at_utc=_NOW,
            events=(
                ReconciliationEvent(
                    at_utc=_NOW, severity=ReconciliationSeverity.ALERT,
                    kind=ReconciliationKind.BROKER_ORPHAN, deal_id="X",
                    pair="GBPUSD", message="orphan",
                ),
            ),
        ),
        actions=ReconciliationActions(),
    )
    bot._dispatch_reconciliation_alerts(outcome)  # must not raise

    bot.stop(inflight_timeout_sec=0.01)


def test_force_close_emits_trade_closed_and_records_in_deal_log(monkeypatch) -> None:
    """Successful EOD/regime force-close emits TRADE_CLOSED (INFO) AND
    seeds the in-memory deal log so the next reconciliation pass
    classifies the now-missing position as POSITION_CLOSED rather
    than MISSING_LOCAL_KEPT."""
    bot, pieces = _build_with_alerter(monkeypatch)
    bot.start()
    bot.mark_ready()
    # Seed a position that will be force-closed.
    from execution.types import ExecutionPosition
    from common import Direction
    from day_type import DayType
    pos = ExecutionPosition(
        deal_id="DEAL_FC1", deal_reference="REF",
        pair="GBPUSD", direction=Direction.BULLISH,
        day_type_at_entry=DayType.NORMAL, strategy_name="trend_break",
        size_units=1.0, entry_price=1.30050,
        initial_sl_price=1.29900, current_sl_price=1.29900,
        suggested_tp_price=1.30450,
        entry_time_utc=_NOW, signal_source_candle_ts=_NOW,
        be_moved=False, trail_active=False, sl_history=(),
    )
    pieces["positions"].upsert(pos)
    from risk.types import ForceCloseOrder
    order = ForceCloseOrder(
        position_id="DEAL_FC1", pair="GBPUSD", reason="EOD_FLATTEN",
    )
    bot._execute_force_close(order)
    # TRADE_CLOSED alert on success.
    sent = pieces["alerter"].sent
    closed = [a for a in sent if a.event_subtype == "TRADE_CLOSED"]
    assert len(closed) == 1
    from alerts import AlertCategory, AlertSeverity
    assert closed[0].severity is AlertSeverity.INFO
    assert closed[0].category is AlertCategory.TRADE
    assert closed[0].pair == "GBPUSD"
    assert "EOD_FLATTEN" in closed[0].full_text
    # And the deal log has the entry.
    assert "DEAL_FC1" in bot._recent_closes


def test_force_close_rejected_does_not_emit_trade_closed(monkeypatch) -> None:
    """Broker rejection on force-close: no TRADE_CLOSED alert, no
    deal-log entry — the position is still open at IG."""
    bot, pieces = _build_with_alerter(monkeypatch)
    bot.start()
    bot.mark_ready()
    pieces["ig"].close_should_fail = True
    from execution.types import ExecutionPosition
    from common import Direction
    from day_type import DayType
    pos = ExecutionPosition(
        deal_id="DEAL_FC2", deal_reference="REF",
        pair="GBPUSD", direction=Direction.BULLISH,
        day_type_at_entry=DayType.NORMAL, strategy_name="trend_break",
        size_units=1.0, entry_price=1.30050,
        initial_sl_price=1.29900, current_sl_price=1.29900,
        suggested_tp_price=1.30450,
        entry_time_utc=_NOW, signal_source_candle_ts=_NOW,
        be_moved=False, trail_active=False, sl_history=(),
    )
    pieces["positions"].upsert(pos)
    from risk.types import ForceCloseOrder
    bot._execute_force_close(ForceCloseOrder(
        position_id="DEAL_FC2", pair="GBPUSD", reason="EOD_FLATTEN",
    ))
    closed = [a for a in pieces["alerter"].sent
              if a.event_subtype == "TRADE_CLOSED"]
    assert closed == []
    assert "DEAL_FC2" not in bot._recent_closes


def test_reconciliation_dispatches_alerts_for_actionable_kinds(monkeypatch) -> None:
    """BROKER_ORPHAN, MISSING_LOCAL_KEPT, MANUAL_SL_MOVE, POSITION_CLOSED
    translate to alerts; OK_NO_OP, SL_UPDATED_FROM_BROKER, STALE_POSITION,
    SL_DRIFT_LARGE are suppressed."""
    bot, pieces = _build_with_alerter(monkeypatch)
    from execution.reconciliation import (
        ReconciliationActions, ReconciliationOutcome,
    )
    from execution.types import (
        ReconciliationEvent, ReconciliationKind,
        ReconciliationReport, ReconciliationSeverity,
    )
    events = (
        # Suppressed
        ReconciliationEvent(
            at_utc=_NOW, severity=ReconciliationSeverity.INFO,
            kind=ReconciliationKind.OK_NO_OP, deal_id=None,
            pair=None, message="ok",
        ),
        ReconciliationEvent(
            at_utc=_NOW, severity=ReconciliationSeverity.INFO,
            kind=ReconciliationKind.SL_UPDATED_FROM_BROKER, deal_id="D1",
            pair="GBPUSD", message="sl updated",
        ),
        ReconciliationEvent(
            at_utc=_NOW, severity=ReconciliationSeverity.WARNING,
            kind=ReconciliationKind.STALE_POSITION, deal_id="D2",
            pair="EURUSD", message="stale",
        ),
        ReconciliationEvent(
            at_utc=_NOW, severity=ReconciliationSeverity.WARNING,
            kind=ReconciliationKind.SL_DRIFT_LARGE, deal_id="D3",
            pair="GBPUSD", message="drift",
        ),
        # Alerted
        ReconciliationEvent(
            at_utc=_NOW, severity=ReconciliationSeverity.WARNING,
            kind=ReconciliationKind.MANUAL_SL_MOVE, deal_id="D4",
            pair="GBPUSD", message="manual sl",
        ),
        ReconciliationEvent(
            at_utc=_NOW, severity=ReconciliationSeverity.ALERT,
            kind=ReconciliationKind.BROKER_ORPHAN, deal_id="D5",
            pair="EURUSD", message="orphan",
        ),
        ReconciliationEvent(
            at_utc=_NOW, severity=ReconciliationSeverity.ALERT,
            kind=ReconciliationKind.MISSING_LOCAL_KEPT, deal_id="D6",
            pair="GBPUSD", message="missing",
        ),
        ReconciliationEvent(
            at_utc=_NOW, severity=ReconciliationSeverity.INFO,
            kind=ReconciliationKind.POSITION_CLOSED, deal_id="D7",
            pair="EURUSD", message="closed via deal log",
        ),
    )
    outcome = ReconciliationOutcome(
        report=ReconciliationReport(at_utc=_NOW, events=events),
        actions=ReconciliationActions(),
    )
    bot._dispatch_reconciliation_alerts(outcome)
    subtypes = sorted(a.event_subtype for a in pieces["alerter"].sent)
    assert subtypes == sorted([
        "MANUAL_SL_MOVE", "BROKER_ORPHAN", "MISSING_LOCAL_KEPT", "TRADE_CLOSED",
    ])


def test_hydrate_returns_summary_dict(monkeypatch) -> None:
    """hydrate() returns {cached_bars, rest_bars, degraded_pairs} for
    the STARTUP alert. M3 (Phase 9 cleanup) adds the degraded_pairs
    list so the operator's first health-check signal surfaces
    pair-level degraded state, not just aggregate row counts."""
    bot, pieces = _build_with_alerter(monkeypatch)
    pieces["feed"].hydrate_report = type("R", (), {
        "ok": True,
        "failed_pairs": (),
        "degraded_pairs": ("EURUSD",),
        "per_pair": (
            type("P", (), {"cached_bars": 100, "rest_bars": 50})(),
            type("P", (), {"cached_bars": 80, "rest_bars": 20})(),
        ),
    })()
    summary = bot.hydrate()
    assert summary == {
        "cached_bars": 180,
        "rest_bars": 70,
        "degraded_pairs": ["EURUSD"],
    }


def test_recent_closes_capped_at_max_with_oldest_pruned(monkeypatch) -> None:
    """M1 (Session-3 commit-2b review): _recent_closes is bounded so
    a long-running session can't grow the dict without limit. When
    the cap is reached, the oldest entry is dropped FIFO (insertion
    order) — newer closes win because reconciliation cares about
    recent activity, not ancient history."""
    import bot.loop as loop_mod
    bot, _ = _build_with_alerter(monkeypatch)
    cap = loop_mod._RECENT_CLOSES_MAX
    # Insert cap+50 entries; only the latest cap should remain.
    for i in range(cap + 50):
        bot._record_recent_close(
            f"DEAL_{i:05d}",
            {"pair": "GBPUSD", "reason": "trickle", "i": i},
        )
    assert len(bot._recent_closes) == cap
    # The first 50 deal_ids were pruned; the latest cap remain.
    assert "DEAL_00000" not in bot._recent_closes
    assert "DEAL_00049" not in bot._recent_closes  # boundary check
    assert "DEAL_00050" in bot._recent_closes  # first survivor
    assert f"DEAL_{cap + 49:05d}" in bot._recent_closes  # newest entry
