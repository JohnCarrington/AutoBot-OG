"""FeedManager — the Phase 7 orchestrator.

The :class:`FeedManager` is the single seam Phase 8 plugs strategy
handlers into. It composes the three concerns the feed layer owns:

1. **Cold-start hydration** —
   :py:func:`feed.hydration.hydrate_pairs` populates each pair's
   :class:`feed.rolling_buffer.RollingBuffer` from cache (and REST
   only as needed) before any live subscription opens.
2. **Live feed** —
   :py:class:`feed.lightstreamer.client.LightstreamerSubscriber`
   delivers ``CHART:{epic}:5MINUTE`` updates which the manager
   classifies into :class:`feed.types.FeedEvent` instances.
3. **Persistence** —
   :py:class:`feed.archive.CandleArchive` is the append-only on-disk
   record per pair.

Decisions the manager makes (not the subscriber, not the parsers):

- **BAR_UPDATE vs BAR_CLOSE.** The subscriber forwards every payload
  along with the parsed candle and the ``CONS_END`` flag. The
  manager remembers the most recently emitted close-time per pair.
  Two close-decision paths:

  * Boundary crossing — the new payload's ``close_time`` is strictly
    greater than the last emitted bar's. The previous bar (if any)
    is treated as closed and emitted as ``BAR_CLOSE`` first, then
    the new bar is emitted as ``BAR_UPDATE``.
  * Explicit ``CONS_END=1`` flip — the manager emits ``BAR_CLOSE``
    immediately for the current bar.

  Either path writes to the archive exactly once via the archive's
  own ``_last_ts`` dedup. The first-bar case (no prior emission)
  emits a single ``BAR_UPDATE`` and waits for either path to close
  it.

- **Status changes → FEED_STALE / FEED_RESUMED.** On every status
  transition we map the LS status string to one of three buckets:
  connected / disconnecting / stalled. A move out of the connected
  bucket emits ``FEED_STALE`` (once per outage); the next move
  back into connected emits ``FEED_RESUMED`` and triggers a
  bounded gap-fill via REST.

- **Gap-fill on reconnect.** After ``FEED_RESUMED`` we compute
  ``now - last_bar.close_time`` and, if it's within
  ``FEED_GAP_FILL_WINDOW_MIN`` and at least one bar wide, fetch a
  small REST history slice, merge into the buffer + archive, and
  emit ``GAP_FILLED`` with the latest backfilled bar. A
  longer-than-window outage is logged but no gap is fetched —
  multi-hour holes are operational events that operators inspect.

Threading & callbacks:

- Every callback registered via :py:meth:`on_event` is invoked on
  whatever thread fired the source event. For LS-driven events
  that's the LS reader thread. For ``hydrate``-driven REST events
  (e.g. ``GAP_FILLED``) it's the hydration thread.
- Callbacks are wrapped in ``try/except`` and never escape — a
  buggy strategy handler can't kill the feed.
- :py:meth:`stop` is the only way to wind everything down cleanly.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from .archive import CandleArchive
from .constants import (
    FEED_BACKFILL_BARS,
    FEED_GAP_FILL_WINDOW_MIN,
    FEED_WATCHDOG_STALE_SEC,
)
from .hydration import (
    HistoryFetcher,
    HydrationReport,
    PairBundle,
    hydrate_pairs,
    parse_ig_history,
)
from .lightstreamer.client import LightstreamerSubscriber, SubscriptionSpec
from .rolling_buffer import RollingBuffer
from .types import Candle, FeedEvent, FeedEventKind

logger = logging.getLogger(__name__)


# Lightstreamer status strings:
#   - Connected:   "CONNECTED:HTTP-STREAMING", "CONNECTED:WS-STREAMING",
#                  "CONNECTED:STREAM-SENSING", "CONNECTED:HTTP-POLLING", etc.
#   - Connecting:  "CONNECTING"
#   - Stalled:     "STALLED"
#   - Disconnected: "DISCONNECTED", "DISCONNECTED:WILL-RETRY"
#
# We treat anything matching ``CONNECTED:*`` as connected. Naive
# substring check is wrong because "CONNECTED" is also a substring of
# "DISCONNECTED" — match the prefix instead.
_CONNECTED_PREFIX = "CONNECTED:"

EventCallback = Callable[[FeedEvent], None]


# ---------------------------------------------------------------------------
# Per-pair state (manager-internal)
# ---------------------------------------------------------------------------


@dataclass
class _PairState:
    """Manager-internal bookkeeping for one pair.

    Distinct from :class:`feed.hydration.PairBundle` (which is just
    the static inputs hydration needs) and from
    :class:`feed.rolling_buffer.RollingBuffer` (which is the data).

    ``out_of_order_count`` tracks how many LS payloads we've dropped
    because their ``close_time`` was older than the last-emitted bar.
    H4 (adversarial review 2026-05-15): silent drops at DEBUG hid the
    failure mode; we now log WARNING + bump this counter so ops can
    surface it via :py:meth:`FeedManager.out_of_order_counts`.
    """

    pair: str
    epic: str
    archive: CandleArchive
    buffer: RollingBuffer
    last_emitted_close: Optional[datetime] = None
    last_emitted_was_closed: bool = False
    last_update_time_utc: Optional[datetime] = None
    out_of_order_count: int = 0


# ---------------------------------------------------------------------------
# FeedManager
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PairSetup:
    """Constructor input for :py:meth:`FeedManager.from_pairs`.

    ``epic`` is the IG epic to subscribe to; ``archive`` and
    ``buffer`` are optional pre-built instances (useful in tests).
    Production callers can let the manager construct them.
    """

    pair: str
    epic: str
    archive: Optional[CandleArchive] = None
    buffer: Optional[RollingBuffer] = None


class FeedManager:
    """Orchestrator over hydration + live feed + archive + dispatch.

    Construction is decoupled from the LS subscriber to keep tests
    self-contained: pass either a pre-built subscriber (the
    production path uses
    :py:meth:`feed.lightstreamer.client.LightstreamerSubscriber`) or
    a fake. Hydration uses an injected :py:data:`HistoryFetcher`
    similarly.

    The lifecycle is:

    1. ``hydrate()`` — required, returns the report. No live events
       until this returns.
    2. ``start_live()`` — opens the LS subscription. After this, the
       registered callbacks start firing.
    3. ``stop()`` — releases the LS connection.
    """

    def __init__(
        self,
        pair_states: list[_PairState],
        *,
        history_fetcher: HistoryFetcher,
        subscriber_factory: Callable[
            [Callable[[str, Candle, bool, dict[str, Any]], None],
             Callable[[str, Optional[str]], None]],
            LightstreamerSubscriber,
        ],
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._pair_states: dict[str, _PairState] = {
            s.pair: s for s in pair_states
        }
        self._history_fetcher = history_fetcher
        self._subscriber_factory = subscriber_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._callbacks: list[EventCallback] = []
        self._subscriber: Optional[LightstreamerSubscriber] = None
        self._is_live = False
        self._has_emitted_stale = False
        self._state_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_pairs(
        cls,
        pairs: list[PairSetup],
        *,
        history_fetcher: HistoryFetcher,
        subscriber_factory: Callable[
            [Callable[[str, Candle, bool, dict[str, Any]], None],
             Callable[[str, Optional[str]], None]],
            LightstreamerSubscriber,
        ],
        clock: Optional[Callable[[], datetime]] = None,
    ) -> "FeedManager":
        states: list[_PairState] = []
        for p in pairs:
            archive = p.archive or CandleArchive(p.pair)
            buffer = p.buffer or RollingBuffer(p.pair)
            states.append(
                _PairState(pair=p.pair, epic=p.epic, archive=archive, buffer=buffer)
            )
        return cls(
            states,
            history_fetcher=history_fetcher,
            subscriber_factory=subscriber_factory,
            clock=clock,
        )

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def hydrate(self) -> HydrationReport:
        """Run cache-first hydration across every configured pair.

        Idempotent — calling it twice re-uses the same archives and
        buffers; the buffer's ``bulk_append`` skips already-present
        bars.
        """
        bundles = [
            PairBundle(
                pair=s.pair, epic=s.epic, archive=s.archive, buffer=s.buffer
            )
            for s in self._pair_states.values()
        ]
        report = hydrate_pairs(
            bundles, fetcher=self._history_fetcher, now_utc=self._clock
        )
        # Seed last_emitted_close to the buffer's latest so the first
        # live update doesn't double-emit BAR_CLOSE for the most recent
        # historical bar.
        for state in self._pair_states.values():
            latest = state.buffer.latest()
            if latest is not None:
                state.last_emitted_close = latest.close_time
                # Historical bars from REST/cache are by definition
                # already closed; mark accordingly.
                state.last_emitted_was_closed = True
        return report

    def start_live(self) -> None:
        """Open the Lightstreamer subscription. Idempotent."""
        if self._is_live:
            return
        subscriber = self._subscriber_factory(
            self._on_ls_update, self._on_ls_status
        )
        subscriber.connect()
        for state in self._pair_states.values():
            subscriber.subscribe_pair(SubscriptionSpec(pair=state.pair, epic=state.epic))
        self._subscriber = subscriber
        self._is_live = True

    def stop(self) -> None:
        """Drop the LS subscription and release resources."""
        if self._subscriber is not None:
            try:
                self._subscriber.disconnect()
            except Exception:
                logger.exception("FeedManager.stop: subscriber.disconnect raised")
            self._subscriber = None
        self._is_live = False

    def on_event(self, callback: EventCallback) -> None:
        """Register a callback for every :class:`FeedEvent`.

        Callbacks are invoked synchronously on the thread that
        produced the event (LS reader for live events, the caller's
        thread for ``hydrate``-time events). The order callbacks
        register is the order they fire.
        """
        self._callbacks.append(callback)

    # ------------------------------------------------------------------
    # Public read helpers
    # ------------------------------------------------------------------

    def latest_candle(self, pair: str) -> Optional[Candle]:
        """Latest stored candle for ``pair`` (could be in-progress)."""
        state = self._pair_states.get(pair)
        if state is None:
            return None
        return state.buffer.latest()

    def buffer_for(self, pair: str) -> Optional[RollingBuffer]:
        """Return the rolling buffer for ``pair``, or ``None`` if absent."""
        state = self._pair_states.get(pair)
        return state.buffer if state is not None else None

    def out_of_order_counts(self) -> dict[str, int]:
        """Return per-pair count of LS payloads dropped as out-of-order.

        H4 (adversarial review 2026-05-15): a non-zero count signals a
        wire-protocol regression or an LS SDK ordering anomaly that
        the silent-drop path would otherwise hide. Phase 8 should
        surface this in the ops metrics export.
        """
        return {
            pair: state.out_of_order_count
            for pair, state in self._pair_states.items()
        }

    def watchdog_stale_pairs(self, *, threshold_sec: int = FEED_WATCHDOG_STALE_SEC) -> list[str]:
        """Return pairs whose last LS update is older than ``threshold_sec``.

        Used by an external watchdog loop (Phase 8+). Returns
        :pythoncode:`[]` before live mode is engaged so cold-start
        hydration alone doesn't trip the alarm.
        """
        if not self._is_live:
            return []
        now = self._clock()
        out: list[str] = []
        for pair, state in self._pair_states.items():
            if state.last_update_time_utc is None:
                out.append(pair)
                continue
            if (now - state.last_update_time_utc).total_seconds() > threshold_sec:
                out.append(pair)
        return out

    # ------------------------------------------------------------------
    # LS callback receivers (invoked on the LS reader thread)
    # ------------------------------------------------------------------

    def _on_ls_update(
        self,
        pair: str,
        candle: Candle,
        cons_end: bool,
        payload: dict[str, Any],
    ) -> None:
        state = self._pair_states.get(pair)
        if state is None:
            logger.warning("LS update for unconfigured pair %s — dropped", pair)
            return
        state.last_update_time_utc = self._clock()

        with self._state_lock:
            prev_close = state.last_emitted_close
            prev_was_closed = state.last_emitted_was_closed

            # Path 1 — boundary crossing. New bar's close_time is past
            # the last one we tracked. If the prior bar wasn't yet
            # emitted as closed, close it now.
            if prev_close is not None and candle.close_time > prev_close:
                if not prev_was_closed:
                    # Close the previous bar using whatever is in the
                    # buffer for that timestamp. The buffer's latest()
                    # IS the previous bar at this point — the manager
                    # has not yet pushed the new one.
                    prior = state.buffer.latest()
                    if prior is not None and prior.close_time == prev_close:
                        state.archive.append(prior)
                        self._dispatch(
                            FeedEventKind.BAR_CLOSE,
                            pair,
                            prior,
                            {"reason": "boundary_crossing"},
                        )
                state.last_emitted_close = candle.close_time
                state.last_emitted_was_closed = False
                state.buffer.push(candle)
                self._dispatch(
                    FeedEventKind.BAR_UPDATE,
                    pair,
                    candle,
                    {"cons_end": cons_end},
                )
                if cons_end:
                    # Same-tick close — emit the BAR_CLOSE immediately.
                    state.archive.append(candle)
                    state.last_emitted_was_closed = True
                    self._dispatch(
                        FeedEventKind.BAR_CLOSE,
                        pair,
                        candle,
                        {"reason": "cons_end_on_first_tick"},
                    )
                return

            # Path 2 — same bar, ongoing updates.
            if prev_close is None or candle.close_time == prev_close:
                state.last_emitted_close = candle.close_time
                state.buffer.push(candle)
                self._dispatch(
                    FeedEventKind.BAR_UPDATE,
                    pair,
                    candle,
                    {"cons_end": cons_end},
                )
                if cons_end and not prev_was_closed:
                    state.archive.append(candle)
                    state.last_emitted_was_closed = True
                    self._dispatch(
                        FeedEventKind.BAR_CLOSE,
                        pair,
                        candle,
                        {"reason": "cons_end_flip"},
                    )
                return

            # Path 3 — out-of-order (stale) update. H4 (adversarial
            # review 2026-05-15): a DEBUG drop was invisible at
            # production logging level (INFO) and could let a wire-
            # protocol regression silently corrupt the buffer over
            # time. Now: WARNING log + bump the per-pair counter so
            # ops can surface the count via out_of_order_counts().
            state.out_of_order_count += 1
            logger.warning(
                "LS %s: out-of-order payload close_time=%s < "
                "last_emitted=%s — dropped (count=%d)",
                pair,
                candle.close_time.isoformat(),
                prev_close.isoformat(),
                state.out_of_order_count,
            )

    def _on_ls_status(self, new_status: str, prev_status: Optional[str]) -> None:
        new_connected = _is_connected(new_status)
        prev_connected = _is_connected(prev_status) if prev_status else False
        if not new_connected and (prev_connected or not self._has_emitted_stale):
            # Edge: transition out of CONNECTED, OR initial connection
            # failure before we ever saw a healthy state. Emit STALE
            # at most once per outage.
            if not self._has_emitted_stale:
                self._has_emitted_stale = True
                self._dispatch(
                    FeedEventKind.FEED_STALE,
                    pair="*",
                    candle=None,
                    debug={"ls_status": new_status, "prev": prev_status},
                )
            return
        if new_connected and self._has_emitted_stale:
            # Reconnect path. Clear the latch, emit FEED_RESUMED, then
            # try a bounded gap-fill across all pairs.
            self._has_emitted_stale = False
            self._dispatch(
                FeedEventKind.FEED_RESUMED,
                pair="*",
                candle=None,
                debug={"ls_status": new_status, "prev": prev_status},
            )
            self._gap_fill_on_resume()

    # ------------------------------------------------------------------
    # Gap fill
    # ------------------------------------------------------------------

    def _gap_fill_on_resume(self) -> None:
        now = self._clock()
        for state in self._pair_states.values():
            latest = state.buffer.latest()
            if latest is None:
                continue
            gap = now - latest.close_time
            if gap < timedelta(minutes=5):
                # Less than a bar wide — nothing to fetch.
                continue
            if gap > timedelta(minutes=FEED_GAP_FILL_WINDOW_MIN):
                logger.warning(
                    "FeedManager: gap-fill window exceeded for %s "
                    "(gap=%s, window=%dm) — skipping REST backfill",
                    state.pair, gap, FEED_GAP_FILL_WINDOW_MIN,
                )
                continue
            try:
                raw = self._history_fetcher(state.epic, "MINUTE_5", FEED_BACKFILL_BARS)
                fresh = parse_ig_history(state.pair, raw)
                missing = [
                    c for c in fresh if c.close_time > latest.close_time
                ]
                if not missing:
                    continue
                state.buffer.bulk_append(missing)
                state.archive.append_many(missing)
                state.last_emitted_close = missing[-1].close_time
                state.last_emitted_was_closed = True
                # H2 (adversarial review 2026-05-15): strategies that
                # listen on BAR_CLOSE need one event per backfilled bar
                # to re-run their decision pipeline correctly. Emitting
                # only a single GAP_FILLED at the end silently starves
                # them of intermediate closes.
                for bar in missing:
                    self._dispatch(
                        FeedEventKind.BAR_CLOSE,
                        pair=state.pair,
                        candle=bar,
                        debug={
                            "reason": "gap_fill_backfill",
                            "source": "REST",
                        },
                    )
                # Final summary event for ops dashboards / alerts that
                # care about reconnect gaps but not per-bar closes.
                self._dispatch(
                    FeedEventKind.GAP_FILLED,
                    pair=state.pair,
                    candle=missing[-1],
                    debug={
                        "bars_filled": len(missing),
                        "first_close": missing[0].close_time.isoformat(),
                        "last_close": missing[-1].close_time.isoformat(),
                    },
                )
            except Exception:
                logger.exception(
                    "FeedManager: gap-fill failed for %s", state.pair,
                )

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch(
        self,
        kind: FeedEventKind,
        pair: str,
        candle: Optional[Candle],
        debug: dict[str, Any],
    ) -> None:
        event = FeedEvent(
            kind=kind,
            pair=pair,
            candle=candle,
            timestamp=self._clock(),
            debug=dict(debug),
        )
        for cb in list(self._callbacks):
            try:
                cb(event)
            except Exception:
                logger.exception(
                    "FeedManager: callback raised for event kind=%s pair=%s",
                    kind.value, pair,
                )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_connected(status: Optional[str]) -> bool:
    if not status:
        return False
    return status.upper().startswith(_CONNECTED_PREFIX)


__all__ = ["EventCallback", "FeedManager", "PairSetup"]
