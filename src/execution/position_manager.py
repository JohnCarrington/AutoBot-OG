"""PositionManager — in-memory ``ExecutionPosition`` tracking + persistence.

The manager owns three pieces of state:

1. A primary :py:class:`PositionsState` (deal-id → position).
2. A by-pair index (pair → set of deal-ids) for cap-counting.
3. A by-signal-source index ((pair, strategy, source_ts) → deal-id)
   for the idempotency check the executor performs before opening.

Indices are maintained in lock-step with the primary store inside
:py:meth:`upsert` / :py:meth:`remove`. They are *not* persisted —
rebuilt from the position list on load.

Every mutation calls :py:meth:`PositionsState.save_if_dirty` so a
process crash never loses more than the last unsaved change.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from .state.positions_state import PositionsState
from .types import ExecutionPosition


_IdempotencyKey = tuple[str, str, datetime]


class PositionManager:
    """Composes :py:class:`PositionsState` with derived indices.

    Construct with an existing :py:class:`PositionsState` (test-time
    convenience) or call :py:meth:`load_from_path` to read from disk.
    """

    def __init__(self, state: PositionsState) -> None:
        self._state = state
        self._by_pair: dict[str, set[str]] = {}
        self._by_signal_source: dict[_IdempotencyKey, str] = {}
        self._rebuild_indices()

    # --- Factory ------------------------------------------------------------

    @classmethod
    def load_from_path(
        cls, path: Optional[Path] = None
    ) -> "PositionManager":
        """Load persisted state from disk (or default location)."""
        return cls(PositionsState.load(path))

    # --- Read API -----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._state)

    def get(self, deal_id: str) -> Optional[ExecutionPosition]:
        return self._state.get(deal_id)

    def all(self) -> list[ExecutionPosition]:
        """Return a *copy* of the current position list (caller-owned)."""
        return self._state.values()

    def for_pair(self, pair: str) -> list[ExecutionPosition]:
        """All positions on a given pair (caller-owned copy)."""
        ids = self._by_pair.get(pair.upper(), set())
        return [self._state.get(did) for did in ids if self._state.get(did)]  # type: ignore[misc]

    def by_signal_source(
        self,
        pair: str,
        strategy_name: str,
        source_candle_ts: datetime,
    ) -> Optional[ExecutionPosition]:
        """Idempotency lookup — returns an existing position for the same
        ``(pair, strategy, source_ts)`` triple, or ``None``.

        The triple is the locked idempotency key (§6.11). A duplicate
        signal arriving for a position that is *already* open silently
        re-uses the existing position rather than double-opening.
        """
        key = (pair.upper(), strategy_name, source_candle_ts)
        deal_id = self._by_signal_source.get(key)
        return self._state.get(deal_id) if deal_id else None

    def count_for_pair(self, pair: str) -> int:
        return len(self._by_pair.get(pair.upper(), set()))

    # --- Mutations ----------------------------------------------------------

    def upsert(self, position: ExecutionPosition) -> None:
        """Insert or replace a position and refresh derived indices."""
        previous = self._state.get(position.deal_id)
        if previous is not None:
            self._index_remove(previous)
        self._state.upsert(position)
        self._index_add(position)
        self._state.save_if_dirty()

    def remove(self, deal_id: str) -> Optional[ExecutionPosition]:
        """Remove and return a position; persist if changed."""
        removed = self._state.remove(deal_id)
        if removed is not None:
            self._index_remove(removed)
            self._state.save_if_dirty()
        return removed

    # --- Persistence pass-through ------------------------------------------

    def save_if_dirty(self) -> bool:
        return self._state.save_if_dirty()

    @property
    def state(self) -> PositionsState:
        """Underlying :py:class:`PositionsState` — exposed for diagnostics."""
        return self._state

    # --- Index helpers ------------------------------------------------------

    def _rebuild_indices(self) -> None:
        self._by_pair = {}
        self._by_signal_source = {}
        for pos in self._state.values():
            self._index_add(pos)

    def _index_add(self, position: ExecutionPosition) -> None:
        pair = position.pair.upper()
        self._by_pair.setdefault(pair, set()).add(position.deal_id)
        key = (pair, position.strategy_name, position.signal_source_candle_ts)
        self._by_signal_source[key] = position.deal_id

    def _index_remove(self, position: ExecutionPosition) -> None:
        pair = position.pair.upper()
        bucket = self._by_pair.get(pair)
        if bucket is not None:
            bucket.discard(position.deal_id)
            if not bucket:
                self._by_pair.pop(pair, None)
        key = (pair, position.strategy_name, position.signal_source_candle_ts)
        existing = self._by_signal_source.get(key)
        # Defensive: only clear if the index still points at this deal_id
        # (a future upsert with the same key may have already overwritten).
        if existing == position.deal_id:
            self._by_signal_source.pop(key, None)


__all__ = ["PositionManager"]
