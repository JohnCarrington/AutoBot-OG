"""Per-pair rolling buffer of recent M5 candles (Phase 7).

The :class:`RollingBuffer` is the in-memory snapshot of the most
recent ``FEED_BUFFER_CAPACITY`` candles for a single pair. It is the
read-side the strategy / indicator layers consume during a live
session — neither layer should reach back to the CSV archive while
trading.

Properties:

- Bounded — a :class:`collections.deque` with ``maxlen``, so writes
  silently evict the oldest bar once capacity is hit. This makes
  worst-case memory predictable (12 fields × 600 rows × 4 pairs ≈
  a few hundred KB, even with Python object overhead).
- Thread-safe — :class:`feed.lightstreamer.client.LightstreamerSubscriber`
  delivers updates on the LS reader thread while a strategy
  callback may be reading the buffer from the main loop. A single
  :class:`threading.Lock` guards every mutation and every read; the
  buffer is small so contention is irrelevant.
- Append-ordered, latest-last — :py:meth:`to_dataframe` returns a
  :class:`pandas.DataFrame` indexed by ``close_time``, sorted
  oldest→newest. Indicator pipelines expect that order.
- Update-or-append on collision — pushing a candle whose
  ``close_time`` matches the most-recent bar replaces it (the live
  feed re-emits the in-progress bar on every tick). Pushing an
  older ``close_time`` is rejected as a bug; the feed manager
  performs gap-fill via :py:meth:`bulk_append`, not :py:meth:`push`.

The buffer does NOT:
- Persist anything — see :py:mod:`feed.archive` for the on-disk
  store.
- Compute indicators — see :py:mod:`indicators`. The buffer is the
  *input* to indicator pipelines.
- Decide ``BAR_UPDATE`` vs ``BAR_CLOSE`` — see
  :py:mod:`feed.feed_manager`. The buffer accepts every push;
  dedup-by-timestamp lives in the manager.
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime
from typing import Iterable, Optional

import pandas as pd

from .constants import FEED_BUFFER_CAPACITY
from .types import Candle


class RollingBuffer:
    """Thread-safe deque of recent :class:`Candle` instances for one pair.

    Parameters
    ----------
    pair : str
        The symbol this buffer holds (e.g. ``"GBPUSD"``). Used only for
        validation — pushing a candle with a mismatched ``pair`` raises
        :class:`ValueError`.
    capacity : int, optional
        Override the default :data:`feed.constants.FEED_BUFFER_CAPACITY`.
        Useful in tests for forcing capacity-overflow scenarios.
    """

    def __init__(self, pair: str, capacity: int = FEED_BUFFER_CAPACITY) -> None:
        if capacity <= 0:
            raise ValueError(
                f"RollingBuffer capacity must be positive, got {capacity}"
            )
        self._pair = pair
        self._capacity = capacity
        self._candles: deque[Candle] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def pair(self) -> str:
        return self._pair

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        with self._lock:
            return len(self._candles)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def push(self, candle: Candle) -> None:
        """Append ``candle`` or replace the newest bar in place.

        Replacement happens when ``candle.close_time`` equals the
        latest stored bar's close time — this is the live-update case
        where the feed re-emits an in-progress bar on every tick.

        Raises
        ------
        ValueError
            If ``candle.pair`` does not match this buffer's pair, or
            if ``candle.close_time`` is older than the newest stored
            bar (use :py:meth:`bulk_append` for backfills).
        """
        if candle.pair != self._pair:
            raise ValueError(
                f"RollingBuffer for {self._pair!r} got candle for "
                f"{candle.pair!r}"
            )
        with self._lock:
            if self._candles:
                latest = self._candles[-1]
                if candle.close_time == latest.close_time:
                    # In-place replacement preserves deque ordering and
                    # capacity semantics — pop-then-append would be a
                    # no-op for the deque but exercises maxlen edge
                    # cases unnecessarily.
                    self._candles[-1] = candle
                    return
                if candle.close_time < latest.close_time:
                    raise ValueError(
                        f"RollingBuffer rejects out-of-order push: "
                        f"new close_time {candle.close_time.isoformat()} "
                        f"< latest {latest.close_time.isoformat()}"
                    )
            self._candles.append(candle)

    def bulk_append(self, candles: Iterable[Candle]) -> int:
        """Append a sorted iterable of candles, returning the count added.

        ``candles`` must be sorted oldest→newest and contiguous with
        the existing tail (no overlap with stored bars). Used by the
        hydration path on cold start and by the gap-fill path on
        reconnect. Out-of-order or duplicate timestamps are silently
        dropped — the caller has already merged history with cache,
        so duplicates here are a sign of overlap, not a bug.
        """
        added = 0
        with self._lock:
            latest_ts: Optional[datetime] = (
                self._candles[-1].close_time if self._candles else None
            )
            for candle in candles:
                if candle.pair != self._pair:
                    raise ValueError(
                        f"RollingBuffer for {self._pair!r} got candle for "
                        f"{candle.pair!r} in bulk_append"
                    )
                if latest_ts is not None and candle.close_time <= latest_ts:
                    # Already covered — skip without raising. Hydration
                    # is allowed to overlap by one bar to verify the
                    # join, and gap-fill is allowed to overlap if the
                    # disconnect happened mid-bar.
                    continue
                self._candles.append(candle)
                latest_ts = candle.close_time
                added += 1
        return added

    def clear(self) -> None:
        """Drop every stored candle. Used by tests and shutdown."""
        with self._lock:
            self._candles.clear()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def latest(self) -> Optional[Candle]:
        """Return the newest candle, or ``None`` if the buffer is empty."""
        with self._lock:
            return self._candles[-1] if self._candles else None

    def snapshot(self) -> list[Candle]:
        """Return a list copy of every stored candle, oldest→newest.

        The copy is shallow but safe to mutate: :class:`Candle` is
        frozen, so callers cannot affect buffer state via the returned
        list.
        """
        with self._lock:
            return list(self._candles)

    def to_dataframe(self) -> pd.DataFrame:
        """Return the buffer as a tz-aware DataFrame indexed by close_time.

        Column dtypes:

        - ``open``, ``high``, ``low``, ``close``, ``volume`` — float64
        - index ``close_time`` — :class:`pandas.DatetimeIndex` (UTC)

        Empty buffer returns an empty DataFrame with the expected
        schema, so downstream code can rely on the columns being
        present even before any data has arrived.
        """
        snapshot = self.snapshot()
        if not snapshot:
            empty = pd.DataFrame(
                columns=["open", "high", "low", "close", "volume"],
                dtype="float64",
            )
            empty.index = pd.DatetimeIndex([], tz="UTC", name="close_time")
            return empty
        df = pd.DataFrame(
            {
                "open": [c.open for c in snapshot],
                "high": [c.high for c in snapshot],
                "low": [c.low for c in snapshot],
                "close": [c.close for c in snapshot],
                "volume": [c.volume for c in snapshot],
            },
            index=pd.DatetimeIndex(
                [c.close_time for c in snapshot], name="close_time"
            ),
            dtype="float64",
        )
        # Indexing constructed from datetime objects keeps tz info; be
        # explicit so a buffer with naive datetimes (shouldn't happen,
        # but tests sometimes seed loose data) still surfaces as UTC.
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        return df


__all__ = ["RollingBuffer"]
