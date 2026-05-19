"""Typed payloads for the feed layer (Phase 7).

These are the public surface for downstream code that consumes the
live market-data feed:

- :class:`Candle` — a single 5-minute bar with mid-price OHLC, the
  pair, the close time, and a ``source`` tag identifying whether the
  bar came off the Lightstreamer native-5m subscription
  (``"LS_NATIVE_5M"``) or a REST backfill (``"REST"``).
- :class:`FeedEventKind` — enum of the event types the
  :class:`feed.feed_manager.FeedManager` emits.
- :class:`FeedEvent` — the envelope used to deliver an event to a
  registered callback.

All time fields are timezone-aware ``datetime`` in UTC. Pricing is in
instrument units (broker quotes, e.g. ``1.30050`` for GBPUSD) — pip
conversion is the caller's responsibility (see
``config.pair_config.price_to_pips``). The dataclasses are frozen so a
callback cannot mutate a candle and silently corrupt the rolling
buffer or the archive view another consumer is holding.

The ``source`` tag is intentionally minimal: it answers "was this
data produced by the streaming session or backfilled from history?"
The strategy / regime layers don't currently branch on it, but
:class:`feed.feed_manager.FeedManager` uses it to distinguish
gap-fill events (REST) from regular bar closes (LS_NATIVE_5M) when
deciding which :class:`FeedEventKind` to emit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional


# ---------------------------------------------------------------------------
# Candle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candle:
    """A single closed (or in-progress) M5 bar for one pair.

    ``close_time`` is the *bar close* timestamp — for a bar covering
    08:55:00–09:00:00 UTC the value is ``09:00:00``. This matches the
    convention used by every downstream consumer (indicators,
    strategies, regime) and the legacy CSV archive.

    For in-progress bars emitted as :class:`FeedEventKind.BAR_UPDATE`,
    ``close_time`` is the *projected* close — i.e. the next 5-minute
    boundary at or after the current UTM. Consumers should not assume
    a candle is final until they receive a :class:`FeedEventKind.BAR_CLOSE`
    event for the same ``close_time``.

    ``volume`` is the consolidated tick count for the bar
    (``CONS_TICK_COUNT`` on the Lightstreamer feed; bar-tick count on
    the REST historical endpoint). It is *not* a notional / contract
    volume — IG does not publish one for spreadbet markets. Strategies
    that need a "volume" signal should treat this as a relative
    activity proxy, not an absolute size.
    """

    pair: str
    close_time: datetime  # UTC, bar CLOSE time
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: Literal["LS_NATIVE_5M", "REST", "DERIVED"]
    # "DERIVED" identifies candles synthesised inside the bot from
    # bars at a finer timeframe — e.g. the Phase B H1 buffer is
    # populated via _synthesise_h1_from_m5_tail from the M5 buffer's
    # last hour of bars. Distinct from "REST" (history endpoint) and
    # "LS_NATIVE_5M" (live Lightstreamer) so ops can tell at a glance
    # whether an H1 bar came over the wire or was computed locally.


# ---------------------------------------------------------------------------
# Feed events
# ---------------------------------------------------------------------------


class FeedEventKind(Enum):
    """The five event types the feed layer emits.

    - ``BAR_UPDATE`` — an in-progress 5-minute bar got a fresh tick
      payload. The attached :class:`Candle` is the *current state* of
      that bar; the same ``close_time`` may be emitted many times.
      Strategies that only care about closed bars should ignore this.

    - ``BAR_CLOSE`` — the bar at ``close_time`` is now final. This is
      emitted exactly once per bar, on the transition from
      ``CONS_END=0`` to ``CONS_END=1`` (or when a REST backfill bar is
      ingested). The bar has already been written to the rolling
      buffer and the archive by the time the callback fires.

    - ``FEED_STALE`` — the Lightstreamer session has dropped from
      ``CONNECTED`` to a non-connected state (DISCONNECTED or
      STALLED). The feed layer will attempt reconnection
      automatically; consumers should pause trading decisions until a
      ``FEED_RESUMED`` arrives.

    - ``FEED_RESUMED`` — the LS session is back in a CONNECTED state.
      A :class:`GAP_FILLED` event will follow once the REST gap-fill
      completes (if any bars were missed during the outage).

    - ``GAP_FILLED`` — a REST-fetched range of bars has been merged
      into the rolling buffer and archive. The ``candle`` field
      carries the *latest* of the gap-filled bars; full debug
      information (bar count, time range) lives in ``debug``.
    """

    BAR_UPDATE = "BAR_UPDATE"
    BAR_CLOSE = "BAR_CLOSE"
    FEED_STALE = "FEED_STALE"
    FEED_RESUMED = "FEED_RESUMED"
    GAP_FILLED = "GAP_FILLED"


@dataclass(frozen=True)
class FeedEvent:
    """Envelope delivered to every callback registered with FeedManager.

    ``candle`` is ``None`` for connection-state events
    (``FEED_STALE`` / ``FEED_RESUMED``) and populated for bar events.
    ``timestamp`` is the wall-clock time the event was constructed —
    this is *not* the candle's close time. Use ``candle.close_time``
    for that.

    ``debug`` is a free-form bag of diagnostic detail (LS status
    string, REST gap range, dedup decision rationale). It is intended
    for logs and ops dashboards, never for strategy logic — strategies
    consuming this surface must rely only on ``kind``, ``pair``, and
    ``candle``.
    """

    kind: FeedEventKind
    pair: str
    candle: Optional[Candle]
    timestamp: datetime
    debug: dict[str, Any] = field(default_factory=dict)


__all__ = ["Candle", "FeedEvent", "FeedEventKind"]
