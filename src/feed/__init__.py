"""feed: market data ingress.

Phase 7 surface (live M5 feed):

- :class:`feed.types.Candle`, :class:`feed.types.FeedEvent`,
  :class:`feed.types.FeedEventKind` — the public data types
  callers consume.
- :class:`feed.feed_manager.FeedManager` — the orchestrator that
  hydrates the rolling buffer, opens the Lightstreamer
  subscription, and dispatches :class:`FeedEvent` instances to
  registered callbacks.
- :class:`feed.rolling_buffer.RollingBuffer` — in-memory bounded
  store of recent candles per pair.
- :class:`feed.archive.CandleArchive` — append-only CSV per pair.

Phase 6 surface (IG broker REST, unchanged):
:py:mod:`feed.ig_rest`.
"""
from .archive import CandleArchive
from .feed_manager import FeedManager, PairSetup
from .rolling_buffer import RollingBuffer
from .types import Candle, FeedEvent, FeedEventKind

__all__ = [
    "Candle",
    "CandleArchive",
    "FeedEvent",
    "FeedEventKind",
    "FeedManager",
    "PairSetup",
    "RollingBuffer",
]
