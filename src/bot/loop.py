"""BotLoop — the Phase 8 orchestrator wiring all prior phases.

Lifecycle
---------

1. ``hydrate()`` — delegated to :py:meth:`FeedManager.hydrate`. Returns
   the report; the caller (``bot.main``) exits 1 if any pair failed
   AND that pair has no usable degraded cache.
2. ``start()`` — opens the LS subscription via
   :py:meth:`FeedManager.start_live`. After this returns, live
   callbacks fire on the LS reader thread.
3. Event handler ``_handle_feed_event`` (registered via
   :py:meth:`FeedManager.on_event`) routes each :class:`FeedEvent`
   to a state-machine transition or a per-event handler.
4. ``stop()`` — drives the graceful shutdown drain (in-flight broker
   calls finish, persistent state flushes, LS disconnects).

State machine
-------------

``STARTING → NORMAL`` after :py:meth:`start`.
``NORMAL → STALE`` on FEED_STALE.
``STALE → RESUMING`` on FEED_RESUMED.
``RESUMING → NORMAL`` on GAP_FILLED *or* the first live (non-gap-fill)
BAR_CLOSE — covering the "no gap to fill" / "gap exceeded window"
cases where GAP_FILLED never fires.
``* → SHUTTING_DOWN`` on SIGTERM/SIGINT or a 5-strike failure counter.

Signal generation is only allowed in ``NORMAL``. Indicators, structure
and regime update on every BAR_CLOSE — including gap-fill bars — so the
historical view is consistent regardless of feed health. The signal
gate is checked *after* the always-on updates.

Failure isolation
-----------------

Two independent 5-strike counters (event vs periodic). Each one trips
into ``SHUTTING_DOWN`` independently. A flaky 10-min reconciliation
shouldn't kill the bot on the next live bar, and vice versa.

Threading
---------

Callbacks fire on the LS reader thread. The bot does not spawn worker
threads — periodic checks ride the same handler. Broker calls increment
``_inflight_count`` so the main thread's shutdown drain can wait for
them to finish before tearing down the LS connection.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

import pandas as pd

from config.pair_config import pair_from_epic, pip_size_for
from execution.executor import Executor
from execution.position_manager import PositionManager
from execution.reconciliation import ReconciliationOutcome, reconcile
from execution.sl_management import evaluate_sl_amend
from execution.types import ExecutionPosition
from feed.feed_manager import FeedManager
from feed.ig_rest.client import IGClient
from feed.ig_rest.markets import fetch_market_info
from feed.types import Candle, FeedEvent, FeedEventKind
from indicators.atr import add_atr
from indicators.bollinger import add_bollinger
from indicators.ema import add_ema
from indicators.macd import add_macd
from indicators.normalised import add_bb_width_normalised, add_ema_slope_normalised
from regime.engine import RegimeEngine
from risk.guard import RiskGuard
from risk.types import (
    AccountState,
    CandidateTrade,
    ForceCloseOrder,
    MarketSnapshot,
    OpenPosition,
)
from strategies.dispatcher import detect_all_setups
from strategies.signal import Signal
from structure import add_fractal_swings

from .constants import (
    BOT_MAX_CONSECUTIVE_EVENT_FAILURES,
    BOT_MAX_CONSECUTIVE_PERIODIC_FAILURES,
    BOT_RECONCILIATION_INTERVAL_MIN,
)
from .types import BotState, FailureCounter

logger = logging.getLogger(__name__)


# v1 defaults for AccountState. The risk layer needs balance and
# realized PnL; v1 is single-pair fixed-size with no in-session realized
# PnL ledger (Phase 9 will add one). The values are conservative enough
# that the daily-DD circuit breaker won't false-fire from the default.
_DEFAULT_BALANCE = 10_000.0
_DEFAULT_CURRENCY = "GBP"


# ---------------------------------------------------------------------------
# Pair-state cache
# ---------------------------------------------------------------------------


@dataclass
class _PerPairBotState:
    """Bot-internal per-pair tracking.

    Distinct from :class:`feed.feed_manager._PairState` (which tracks
    FEED-layer state) and from :class:`feed.rolling_buffer.RollingBuffer`
    (which is the M5 data). This holds whatever the orchestrator
    needs to thread between events.

    Right now: just the regime engine. Per-pair regime is the v1
    design — each pair has its own engine instance because regime
    detection runs on a single pair's indicator timeline.
    """

    pair: str
    regime_engine: RegimeEngine
    last_h1_open_processed: Optional[datetime] = None


# ---------------------------------------------------------------------------
# BotLoop
# ---------------------------------------------------------------------------


class BotLoop:
    """Top-level orchestrator for Phase 8.

    Parameters
    ----------
    feed_manager, ig_client, executor, risk_guard, position_manager :
        Phase 6/7 components. Constructor is dependency-injection only
        so tests can swap any of them for fakes.
    pairs : tuple[str, ...]
        Configured trading pairs.
    pair_to_epic : dict[str, str]
        Resolver used for the few seams that take an epic
        (``fetch_market_info`` and ``executor.epic_resolver``).
    account_balance, account_currency : float, str
        Static AccountState fields for v1. Phase 9+ will pull these
        from the broker.
    clock : callable, optional
        Test seam — defaults to ``datetime.now(timezone.utc)``.
    """

    def __init__(
        self,
        *,
        feed_manager: FeedManager,
        ig_client: IGClient,
        executor: Executor,
        risk_guard: RiskGuard,
        position_manager: PositionManager,
        pairs: tuple[str, ...],
        pair_to_epic: dict[str, str],
        account_balance: float = _DEFAULT_BALANCE,
        account_currency: str = _DEFAULT_CURRENCY,
        clock: Optional[Callable[[], datetime]] = None,
        regime_engines: Optional[dict[str, RegimeEngine]] = None,
    ) -> None:
        self._feed = feed_manager
        self._client = ig_client
        self._executor = executor
        self._risk = risk_guard
        self._positions = position_manager
        self._pairs = tuple(pairs)
        self._pair_to_epic = dict(pair_to_epic)
        self._account_balance = account_balance
        self._account_currency = account_currency
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        self._state: BotState = BotState.STARTING
        # One regime engine per pair (v1: per-pair regime). Callers
        # (bot.main._build_runtime, tests) MAY supply the engine map
        # so the same instances can also be handed to RiskGuard via
        # an ``engine_for_pair`` callable — that's the C2 fix. When
        # omitted, BotLoop constructs its own; the caller is then
        # responsible for not also wiring a *different* engine into
        # RiskGuard (which is exactly how the C2 bug shipped).
        if regime_engines is not None:
            missing = set(self._pairs) - set(regime_engines.keys())
            if missing:
                raise ValueError(
                    f"regime_engines is missing entries for pairs: "
                    f"{sorted(missing)}"
                )
            engine_map = dict(regime_engines)
        else:
            engine_map = {p: RegimeEngine() for p in self._pairs}
        self._regime_engines: dict[str, RegimeEngine] = engine_map
        self._pair_state: dict[str, _PerPairBotState] = {
            p: _PerPairBotState(pair=p, regime_engine=engine_map[p])
            for p in self._pairs
        }
        self._last_reconciliation_at: datetime = self._clock()

        # Failure counters — two independent 5-strike groups.
        self._event_failures = FailureCounter(
            name="event", threshold=BOT_MAX_CONSECUTIVE_EVENT_FAILURES,
        )
        self._periodic_failures = FailureCounter(
            name="periodic", threshold=BOT_MAX_CONSECUTIVE_PERIODIC_FAILURES,
        )

        # In-flight broker call tracking — incremented before each
        # broker round-trip, decremented in ``finally``. Main thread's
        # shutdown drain waits on this.
        self._inflight_count: int = 0
        self._inflight_lock = threading.Lock()
        self._inflight_zero = threading.Condition(self._inflight_lock)

        # Shutdown signalling.
        self._shutdown_requested = threading.Event()

        # Realized PnL ledger — v1 keeps a running counter; Phase 9+
        # will source from reconciliation events. Starts at 0.
        self._realized_pnl_today_r: float = 0.0
        # M1 (adversarial review 2026-05-15): make the v1 limitation
        # loud on construction. The daily-DD circuit breaker reads
        # account.realized_pnl_today_r — with the counter pinned to
        # zero, the breaker is informational only and will not block
        # a 6th losing trade after 5 in a row. Phase 9+ wires this up
        # via the reconciliation outcome's close events.
        logger.warning(
            "BotLoop v1 limitation: realized_pnl_today_r is hardcoded "
            "to 0.0 — the daily-DD circuit breaker is informational "
            "only until Phase 9+ adds a realized-PnL ledger. See "
            "src/bot/MODULE.md (v1 simplifications)."
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def hydrate(self) -> None:
        report = self._feed.hydrate()
        logger.info(
            "Hydration complete: ok=%s, failed_pairs=%s, degraded_pairs=%s",
            report.ok,
            list(report.failed_pairs),
            list(report.degraded_pairs),
        )
        if not report.ok:
            raise RuntimeError(
                f"Hydration failed for pairs: {list(report.failed_pairs)}"
            )

    def start(self) -> None:
        """Register the event callback and open the LS subscription.

        State remains :data:`BotState.STARTING` after this returns. The
        main thread is expected to run :py:func:`bot.preflight.verify_subscriptions`
        next, and only then call :py:meth:`mark_ready` to flip the
        state to ``NORMAL``. H4 (adversarial review 2026-05-15): the
        old code unconditionally set NORMAL inside ``start()``, which
        meant the bot reported NORMAL during the ~10-second window
        while subscriptions could still be partially or wholly failed.
        """
        self._feed.on_event(self._handle_feed_event)
        self._feed.start_live()
        # State stays STARTING — caller verifies subscriptions, then
        # calls mark_ready() to enter NORMAL.

    def mark_ready(self) -> None:
        """Promote :data:`STARTING` → :data:`NORMAL`.

        Called by :func:`bot.main.main` after
        :py:func:`bot.preflight.verify_subscriptions` confirms every
        configured pair is live. Idempotent; a no-op once the bot has
        already transitioned past ``STARTING``.
        """
        if self._state != BotState.STARTING:
            return
        self._state = BotState.NORMAL
        logger.info("BotLoop entered state NORMAL")

    def request_shutdown(self) -> None:
        """Signal the main thread that a graceful shutdown is in progress.

        Thread-safe (sets a :class:`threading.Event`). The LS reader
        thread continues to fire callbacks until ``stop()`` actually
        disconnects, but those callbacks short-circuit when
        ``_state == SHUTTING_DOWN``.
        """
        if self._state == BotState.SHUTTING_DOWN:
            return
        prev = self._state
        self._state = BotState.SHUTTING_DOWN
        self._shutdown_requested.set()
        logger.warning("Shutdown requested (prev_state=%s)", prev.value)

    def shutdown_event(self) -> threading.Event:
        """Return the threading.Event the main thread blocks on."""
        return self._shutdown_requested

    def stop(
        self,
        *,
        inflight_timeout_sec: float,
        poll_sec: float = 0.1,
    ) -> None:
        """Drain in-flight broker calls, flush state, disconnect LS.

        Caller (``bot.main.main``) supplies the timeout
        (:data:`BOT_SHUTDOWN_INFLIGHT_TIMEOUT_SEC`). Order is:

        1. Wait for ``_inflight_count == 0`` or the timeout — whichever
           comes first. If the timeout trips we log CRITICAL and
           continue: a stuck broker call is better than a stuck
           shutdown that gets SIGKILL'd.
        2. ``position_manager.save_if_dirty()`` — persist any in-memory
           changes.
        3. ``feed_manager.stop()`` — releases the LS connection.
        """
        if self._state != BotState.SHUTTING_DOWN:
            # Allow stop() to be called without an explicit shutdown
            # request (e.g. when the main thread observes a failure
            # threshold trip).
            self.request_shutdown()

        drained = self._drain_inflight(timeout_sec=inflight_timeout_sec, poll_sec=poll_sec)
        if not drained:
            logger.critical(
                "BotLoop.stop: %d broker calls still in flight after %.1fs — "
                "proceeding with shutdown anyway",
                self._inflight_count, inflight_timeout_sec,
            )

        try:
            saved = self._positions.save_if_dirty()
            logger.info("Position state save_if_dirty returned %s", saved)
        except Exception:
            logger.exception("BotLoop.stop: position state flush failed")

        try:
            self._feed.stop()
        except Exception:
            logger.exception("BotLoop.stop: feed_manager.stop raised")

    @property
    def crashed(self) -> bool:
        """``True`` iff shutdown was triggered by a failure threshold trip."""
        return (
            self._event_failures.should_shutdown()
            or self._periodic_failures.should_shutdown()
        )

    @property
    def state(self) -> BotState:
        return self._state

    def regime_engine_for(self, pair: str) -> RegimeEngine:
        """Return the per-pair regime engine — for sharing with RiskGuard.

        bot.main wires ``RiskGuard(engine_for_pair=bot.regime_engine_for)``
        so the risk layer reads the same engine the BotLoop feeds. C2
        (adversarial review 2026-05-15): wiring a separate engine into
        RiskGuard silently disabled the regime-instability circuit
        breaker because nothing ever called ``process_*_close`` on the
        standalone instance.
        """
        return self._regime_engines[pair]

    @property
    def regime_engines(self) -> dict[str, RegimeEngine]:
        """Read-only view of the per-pair regime engine map.

        Returns a shallow copy — callers must not mutate the dict.
        Intended for ops introspection and the ``bot.main`` wiring
        step (handed to ``RiskGuard(engine_for_pair=...)``).
        """
        return dict(self._regime_engines)

    # ------------------------------------------------------------------
    # Event handler
    # ------------------------------------------------------------------

    def _handle_feed_event(self, event: FeedEvent) -> None:
        """Top-level event dispatch. Called on the LS reader thread.

        M6 (adversarial review 2026-05-15): also gate STARTING. The
        original guard only short-circuited SHUTTING_DOWN, which was
        benign for the documented call order but fragile against any
        refactor that inverted ``on_event`` vs ``start_live``.

        M2 (adversarial review 2026-05-15): only ``BAR_CLOSE`` events
        whose pipeline runs to completion reset the event-failure
        counter. The original code reset on every successful dispatch
        — including no-op kinds (``BAR_UPDATE``, status transitions)
        — which let a busy bar's interleaved BAR_UPDATE successes
        wipe out the failure count between failed BAR_CLOSE events.
        """
        if self._state in (BotState.STARTING, BotState.SHUTTING_DOWN):
            return
        is_real_work = event.kind is FeedEventKind.BAR_CLOSE
        try:
            self._dispatch_event(event)
        except Exception as exc:
            self._event_failures.record_failure(exc, now_utc=self._clock())
            logger.exception(
                "BotLoop: event handler raised (kind=%s pair=%s, "
                "consecutive=%d)",
                event.kind.value,
                event.pair,
                self._event_failures.consecutive,
            )
            if self._event_failures.should_shutdown():
                logger.critical(
                    "BotLoop: %d consecutive event failures — requesting "
                    "shutdown",
                    self._event_failures.consecutive,
                )
                self.request_shutdown()
            return
        if is_real_work:
            # Only BAR_CLOSE represents "successful work" — no-op kinds
            # never get to reset the counter (M2).
            self._event_failures.record_success()

    def _dispatch_event(self, event: FeedEvent) -> None:
        kind = event.kind
        if kind is FeedEventKind.FEED_STALE:
            self._transition(BotState.STALE, reason="FEED_STALE")
            return
        if kind is FeedEventKind.FEED_RESUMED:
            self._transition(BotState.RESUMING, reason="FEED_RESUMED")
            return
        if kind is FeedEventKind.GAP_FILLED:
            # Note: the per-bar BAR_CLOSE events generated by gap-fill
            # arrive *before* this summary event. By the time
            # GAP_FILLED lands, the buffer is fully updated.
            self._transition(BotState.NORMAL, reason="GAP_FILLED")
            return
        if kind is FeedEventKind.BAR_UPDATE:
            return  # signals only on close (design decision #1)
        if kind is FeedEventKind.BAR_CLOSE:
            self._handle_bar_close(event)

    def _transition(self, new_state: BotState, *, reason: str) -> None:
        if self._state == new_state:
            return
        if self._state == BotState.SHUTTING_DOWN:
            return
        logger.info(
            "BotLoop state transition: %s -> %s (reason=%s)",
            self._state.value, new_state.value, reason,
        )
        self._state = new_state

    # ------------------------------------------------------------------
    # BAR_CLOSE pipeline
    # ------------------------------------------------------------------

    def _handle_bar_close(self, event: FeedEvent) -> None:
        candle = event.candle
        if candle is None:
            logger.warning("BAR_CLOSE without candle — dropped (pair=%s)", event.pair)
            return
        pair = event.pair
        if pair not in self._pair_state:
            logger.warning(
                "BAR_CLOSE for unconfigured pair %s — dropped", pair,
            )
            return
        is_gap_fill = event.debug.get("reason") == "gap_fill_backfill"

        # 1. Always-on: indicators + structure + regime updates. Even
        #    gap-fill bars feed these so the historical view stays
        #    consistent regardless of feed health.
        df_m5 = self._build_m5_dataframe(pair)
        if df_m5.empty:
            logger.warning("BAR_CLOSE for %s but rolling buffer is empty", pair)
            return
        df_m5_enriched = self._apply_indicators(df_m5)
        df_m5_enriched = add_fractal_swings(df_m5_enriched)
        df_h1_enriched = self._derive_and_enrich_h1(
            df_m5_enriched, m5_close_time=candle.close_time,
        )

        self._update_regime(pair, df_m5_enriched, df_h1_enriched, candle)

        # 2. Periodic tasks. Run before the signal gate so reconciliation
        #    and force-close fire during STALE / RESUMING windows where
        #    they're most useful.
        self._maybe_reconcile()
        self._maybe_force_close_orders()

        # 3. RESUMING → NORMAL on first live BAR_CLOSE if GAP_FILLED
        #    didn't arrive (gap was < 1 bar, or > window).
        if self._state == BotState.RESUMING and not is_gap_fill:
            self._transition(BotState.NORMAL, reason="live_bar_after_resume")

        # 4. Signal-pipeline gate: skip during stale window AND on
        #    gap-fill bars (don't trade on stale data).
        signals_blocked = (
            self._state in (BotState.STALE, BotState.RESUMING, BotState.SHUTTING_DOWN)
            or is_gap_fill
        )
        if not signals_blocked:
            self._run_signal_pipeline(pair, df_m5_enriched, df_h1_enriched)

        # 5. SL evaluation per open position (BAR_CLOSE cadence, design
        #    decision #1 — not BAR_UPDATE, not separate timer).
        self._run_sl_evaluation(pair, df_m5_enriched, candle)

    # ------------------------------------------------------------------
    # DataFrame plumbing
    # ------------------------------------------------------------------

    def _build_m5_dataframe(self, pair: str) -> pd.DataFrame:
        buf = self._feed.buffer_for(pair)
        if buf is None:
            return pd.DataFrame()
        return buf.to_dataframe()

    def _apply_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Apply the v1 indicator stack to ``df``.

        v1 indicators (locked in ``docs/v1_architecture.md`` §3):
        ATR(14), EMA(20), EMA(50), Bollinger(20, 2), MACD(12, 26, 9),
        plus the ATR-normalised EMA-slope and BB-width. The pipeline
        is pure-functional — each ``add_*`` returns a new column on
        a copy of the DataFrame.
        """
        out = add_atr(df, period=14)
        out = add_ema(out, period=20)
        out = add_ema(out, period=50)
        out = add_bollinger(out, period=20, std_mult=2.0)
        out = add_macd(out, fast=12, slow=26, signal=9)
        out = add_ema_slope_normalised(out, period=50, lookback=10, atr_period=14)
        out = add_bb_width_normalised(out, bb_period=20, bb_std=2.0, atr_period=14)
        return out

    def _derive_and_enrich_h1(
        self, df_m5: pd.DataFrame, *, m5_close_time: datetime,
    ) -> pd.DataFrame:
        """Roll M5 → H1 by pandas resample, then apply H1 indicators.

        Phase 7's RollingBuffer is M5-only; the strategy dispatcher and
        regime engine consume H1 inputs. v1 derives H1 from the rolling
        M5 window on the fly. The resample uses ``label="right"`` and
        ``closed="right"`` so each H1 bar is anchored on its close,
        matching the M5 buffer's convention.

        H1 (adversarial review 2026-05-15): at a mid-hour M5 close
        (e.g. 13:35) the resample produces an H1 bar labelled 14:00
        with only 7 of the expected 12 M5 contributions. ``dropna()``
        does not remove it — every OHLC slot is populated. Strategies
        gating on H1 indicators (`bb_reclaim`, `ema_continuation`)
        would then see a value that recomputes on every M5 tick,
        breaking the per-H1-bar stability the strategies assume.
        Fix: drop the trailing forming H1 unless the M5 close that
        triggered this call is exactly on an hour boundary (minute=0,
        meaning the M5 bar closing now also closed an H1).
        """
        if df_m5.empty:
            return df_m5
        agg = df_m5.resample("1h", label="right", closed="right").agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        ).dropna()
        if agg.empty:
            return agg
        if m5_close_time.minute != 0:
            # The latest bin is the in-progress H1 — trim it. Strategies
            # only see fully-closed H1 bars.
            agg = agg.iloc[:-1]
        if agg.empty:
            return agg
        return self._apply_indicators(agg)

    def _h1_for_test(self, pair: str, *, m5_close_time: datetime) -> pd.DataFrame:
        """Test seam returning the H1 dataframe a BAR_CLOSE would produce.

        Mirrors the exact computation in :py:meth:`_handle_bar_close`
        but is reachable from tests without firing a full event. Used
        to pin the H1-trim behaviour without exposing private state.
        """
        df_m5 = self._build_m5_dataframe(pair)
        if df_m5.empty:
            return df_m5
        df_m5 = self._apply_indicators(df_m5)
        df_m5 = add_fractal_swings(df_m5)
        return self._derive_and_enrich_h1(df_m5, m5_close_time=m5_close_time)

    # ------------------------------------------------------------------
    # Regime
    # ------------------------------------------------------------------

    def _update_regime(
        self,
        pair: str,
        df_m5: pd.DataFrame,
        df_h1: pd.DataFrame,
        m5_candle: Candle,
    ) -> None:
        state = self._pair_state[pair]
        engine = state.regime_engine

        # H1 close — only when the M5 bar we just received was the
        # last M5 of an H1 (minute == 0 on close_time means we just
        # completed minute 55→00).
        if m5_candle.close_time.minute == 0 and not df_h1.empty:
            new_h1_open = df_h1.index[-1]
            if state.last_h1_open_processed != new_h1_open:
                prev_h1 = df_h1.iloc[-2] if len(df_h1) >= 2 else None
                engine.process_h1_close(df_h1.iloc[-1], prev_h1)
                state.last_h1_open_processed = new_h1_open

        # Always feed the M5 close.
        if not df_m5.empty:
            engine.process_m5_close(df_m5.iloc[-1])

    # ------------------------------------------------------------------
    # Periodic tasks (inline scheduler)
    # ------------------------------------------------------------------

    def _maybe_reconcile(self) -> None:
        now = self._clock()
        elapsed = now - self._last_reconciliation_at
        if elapsed < timedelta(minutes=BOT_RECONCILIATION_INTERVAL_MIN):
            return
        try:
            self._with_inflight_tracked(self._reconcile_once)
            self._periodic_failures.record_success()
        except Exception as exc:
            self._periodic_failures.record_failure(exc, now_utc=self._clock())
            logger.exception(
                "Reconciliation failed (consecutive=%d)",
                self._periodic_failures.consecutive,
            )
            self._maybe_periodic_shutdown()
        finally:
            # Stamp regardless of outcome so a flaky reconciliation
            # doesn't queue up retries on every subsequent BAR_CLOSE.
            self._last_reconciliation_at = now

    def _reconcile_once(self) -> ReconciliationOutcome:
        broker_positions = self._client.fetch_open_positions()
        outcome = reconcile(
            manager=self._positions,
            broker_positions=broker_positions,
            now_utc=self._clock(),
        )
        # Apply SL updates the reconciler suggests, then drop deal_ids
        # the broker no longer reports.
        for deal_id, new_sl in outcome.actions.apply_sl_updates.items():
            pos = self._positions.get(deal_id)
            if pos is None:
                continue
            self._positions.upsert(
                pos.with_changes(current_sl_price=new_sl)
            )
        for deal_id in outcome.actions.remove_deal_ids:
            self._positions.remove(deal_id)
        return outcome

    def _maybe_force_close_orders(self) -> None:
        """Phase 4 owns the "fire once per day" guard. We just call it.

        :py:meth:`RiskGuard.positions_to_force_close` returns an empty
        list 99% of the time and a non-empty list on the EOD UTC
        boundary (or on regime-transition closes). We execute whatever
        it returns; the rule layer encapsulates the timing.

        H3 (adversarial review 2026-05-15): a clean run records
        success symmetrically with :py:meth:`_maybe_reconcile`. Before
        the fix this method only ever bumped the failure counter,
        creating a pathological pattern where a flaky reconciliation
        could trip the threshold even when every interleaved
        force-close ran cleanly — counter never reset between
        reconciliation failures.
        """
        positions = self._collect_open_positions(latest_prices=self._latest_prices())
        try:
            orders = self._risk.positions_to_force_close(
                positions=positions, now_utc=self._clock(),
            )
        except Exception as exc:
            self._periodic_failures.record_failure(exc, now_utc=self._clock())
            logger.exception(
                "positions_to_force_close raised (consecutive=%d)",
                self._periodic_failures.consecutive,
            )
            self._maybe_periodic_shutdown()
            return
        if not orders:
            # Clean no-op — reset the periodic counter (H3).
            self._periodic_failures.record_success()
            return
        logger.info("RiskGuard returned %d force-close order(s)", len(orders))
        for order in orders:
            try:
                self._with_inflight_tracked(
                    lambda o=order: self._execute_force_close(o)
                )
            except Exception:
                # Per-order isolation — one failed close should not
                # block the others (and shouldn't count as a *periodic*
                # failure either, since the close is broker-IO).
                logger.exception(
                    "Force-close failed for %s (pair=%s reason=%s)",
                    order.position_id, order.pair, order.reason,
                )
        # Iteration completed (regardless of per-order broker failures)
        # — the periodic seam succeeded. Reset the periodic counter.
        self._periodic_failures.record_success()

    def _execute_force_close(self, order: ForceCloseOrder) -> None:
        from feed.ig_rest.types import CloseRequest  # local import keeps top tight
        from regime.labels import Direction

        position = self._positions.get(order.position_id)
        if position is None:
            logger.warning(
                "Force-close requested for unknown deal_id=%s — already gone?",
                order.position_id,
            )
            return
        # C1 (adversarial review 2026-05-15): CloseRequest.position_direction
        # holds the position's OWN direction. The wrapper in
        # feed.ig_rest.positions.close_position inverts internally before
        # talking to IG. Pre-inverting here used to cause double inversion
        # → IG received the position's original side → opened a same-side
        # position instead of closing → doubled exposure on every EOD
        # flatten. Match the executor's emergency-close path
        # (src/execution/executor.py:216-227) which passes the position's
        # own direction directly.
        own_direction = (
            "BUY" if position.direction == Direction.BULLISH else "SELL"
        )
        request = CloseRequest(
            deal_id=position.deal_id,
            epic=self._pair_to_epic[position.pair],
            position_direction=own_direction,
            size=position.size_units,
        )
        confirmation = self._client.close_position(request)
        if confirmation.status == "ACCEPTED":
            self._positions.remove(position.deal_id)
            logger.info(
                "Force-close accepted: deal_id=%s pair=%s reason=%s",
                position.deal_id, position.pair, order.reason,
            )
        else:
            logger.warning(
                "Force-close REJECTED by broker: deal_id=%s pair=%s "
                "reason_local=%s broker_status=%s",
                position.deal_id, position.pair, order.reason,
                confirmation.deal_status,
            )

    def _maybe_periodic_shutdown(self) -> None:
        if self._periodic_failures.should_shutdown():
            logger.critical(
                "BotLoop: %d consecutive periodic failures — requesting "
                "shutdown",
                self._periodic_failures.consecutive,
            )
            self.request_shutdown()

    # ------------------------------------------------------------------
    # Signal pipeline
    # ------------------------------------------------------------------

    def _run_signal_pipeline(
        self, pair: str, df_m5: pd.DataFrame, df_h1: pd.DataFrame,
    ) -> None:
        engine = self._pair_state[pair].regime_engine
        if not engine.is_live():
            return  # regime in TRANSITION — strategies don't fire
        now = self._clock()
        signals = detect_all_setups(
            df_m5=df_m5,
            df_h1=df_h1,
            regime_state=engine.get_state(),
            pair=pair,
            current_time=now,
        )
        for signal in signals:
            self._evaluate_and_execute(signal)

    def _evaluate_and_execute(self, signal: Signal) -> None:
        # Build the risk-layer inputs.
        latest_prices = self._latest_prices()
        positions = self._collect_open_positions(latest_prices=latest_prices)
        candidate = CandidateTrade(
            pair=signal.pair,
            intended_direction=signal.direction,
            intended_regime=signal.regime,
            planned_entry_price=signal.suggested_entry_price,
        )
        account = AccountState(
            balance=self._account_balance,
            currency=self._account_currency,
            realized_pnl_today_r=self._realized_pnl_today_r,
        )
        market = self._build_market_snapshot(signal.pair)
        if market is None:
            logger.warning(
                "Signal for %s skipped: could not build MarketSnapshot",
                signal.pair,
            )
            return
        decision = self._risk.allow_entry(
            candidate=candidate,
            positions=positions,
            account=account,
            market=market,
            now_utc=self._clock(),
        )
        if not decision.allow:
            logger.info(
                "Signal for %s rejected by risk: rule=%s reason=%s",
                signal.pair, decision.rule, decision.reason,
            )
            return
        # Approved — open the position. Tracked via in-flight counter.
        try:
            self._with_inflight_tracked(
                lambda: self._executor.open_from_signal(signal)
            )
        except Exception:
            logger.exception(
                "Executor.open_from_signal raised for pair=%s strategy=%s",
                signal.pair, signal.strategy_name,
            )

    # ------------------------------------------------------------------
    # SL management
    # ------------------------------------------------------------------

    def _run_sl_evaluation(
        self, pair: str, df_m5: pd.DataFrame, latest_candle: Candle,
    ) -> None:
        positions = self._positions.for_pair(pair)
        if not positions:
            return
        current_price = latest_candle.close
        for position in positions:
            try:
                amend = evaluate_sl_amend(position, df_m5, current_price)
            except Exception:
                logger.exception(
                    "evaluate_sl_amend raised for deal_id=%s",
                    position.deal_id,
                )
                continue
            if amend is None:
                continue
            try:
                self._with_inflight_tracked(
                    lambda a=amend: self._executor.apply_amend(a)
                )
            except Exception:
                logger.exception(
                    "Executor.apply_amend raised for deal_id=%s",
                    position.deal_id,
                )

    # ------------------------------------------------------------------
    # Risk-input builders
    # ------------------------------------------------------------------

    def _latest_prices(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for pair in self._pairs:
            candle = self._feed.latest_candle(pair)
            if candle is not None:
                out[pair] = candle.close
        return out

    def _collect_open_positions(
        self, *, latest_prices: dict[str, float],
    ) -> list[OpenPosition]:
        out: list[OpenPosition] = []
        for ep in self._positions.all():
            price = latest_prices.get(ep.pair, ep.entry_price)
            out.append(ep.to_risk_open_position(current_price=price))
        return out

    def _build_market_snapshot(self, pair: str) -> Optional[MarketSnapshot]:
        epic = self._pair_to_epic.get(pair)
        if epic is None:
            logger.error("No epic mapping for pair %s", pair)
            return None
        try:
            market_info = self._with_inflight_tracked(
                lambda: fetch_market_info(self._client.session, epic)
            )
        except Exception:
            logger.exception("fetch_market_info failed for %s/%s", pair, epic)
            return None

        pip = pip_size_for(pair)
        spread_pips = (market_info.offer - market_info.bid) / pip
        atr_m5_pips = self._latest_atr_pips(pair) or 0.0
        return MarketSnapshot(
            current_spread_pips=max(0.0, spread_pips),
            atr_m5_pips=atr_m5_pips,
        )

    def _latest_atr_pips(self, pair: str) -> Optional[float]:
        df = self._build_m5_dataframe(pair)
        if df.empty:
            return None
        df = add_atr(df, period=14)
        if "atr_14" not in df.columns or df["atr_14"].dropna().empty:
            return None
        latest_atr = df["atr_14"].dropna().iloc[-1]
        return float(latest_atr) / pip_size_for(pair)

    # ------------------------------------------------------------------
    # In-flight broker call tracking
    # ------------------------------------------------------------------

    def _with_inflight_tracked(self, fn: Callable[[], Any]) -> Any:
        """Wrap a broker call so the shutdown drain can wait for it."""
        with self._inflight_lock:
            self._inflight_count += 1
        try:
            return fn()
        finally:
            with self._inflight_lock:
                self._inflight_count -= 1
                if self._inflight_count == 0:
                    self._inflight_zero.notify_all()

    def _drain_inflight(
        self, *, timeout_sec: float, poll_sec: float,
    ) -> bool:
        """Block until in-flight broker calls finish or timeout.

        Returns ``True`` if drained cleanly, ``False`` if the timeout
        tripped first.

        Uses :py:func:`time.monotonic` rather than the injected
        ``self._clock`` — the bot's clock is for "what UTC datetime is
        now" (candle close times, log timestamps), but a wall-clock
        wait deadline needs a monotonically-advancing source so the
        loop terminates even when tests inject a fixed clock.
        """
        deadline = time.monotonic() + timeout_sec
        with self._inflight_zero:
            while self._inflight_count > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                # Use min(poll_sec, remaining) so a long timeout still
                # wakes promptly when the count actually hits zero.
                self._inflight_zero.wait(timeout=min(poll_sec, remaining))
        return True

    # ------------------------------------------------------------------
    # Test helpers / introspection
    # ------------------------------------------------------------------

    @property
    def event_failures(self) -> FailureCounter:
        return self._event_failures

    @property
    def periodic_failures(self) -> FailureCounter:
        return self._periodic_failures

    @property
    def inflight_count(self) -> int:
        with self._inflight_lock:
            return self._inflight_count


__all__ = ["BotLoop"]
