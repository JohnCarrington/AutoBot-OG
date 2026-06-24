"""JSON-backed :py:class:`ExecutionPosition` persistence.

Mirrors the v1 risk-layer state pattern
(``src/risk/state/circuit_breaker_state.py``):

- Plain-JSON file at :data:`DEFAULT_STATE_PATH` (gitignored
  ``data/execution/`` subtree).
- Load is **fail-open**: corrupt or missing file → empty state with a
  WARNING log, so a one-off disk-corruption event doesn't take the bot
  offline.
- Save is **atomic**: tempfile + ``os.replace``, so a partially-written
  file is never readable by a concurrent reader.
- Schema-versioned so future shape changes can migrate cleanly.

The class is a dumb container — orchestration (load → mutate →
save_if_dirty) lives in :py:class:`execution.position_manager.PositionManager`.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from day_type import DayType
from common import Direction

from ..constants import EXECUTION_STATE_PATH
from ..types import ExecutionPosition, SLAmendment


logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH: Path = Path(EXECUTION_STATE_PATH)

# v2 (2a clean-swap): ``regime_at_entry`` renamed to ``day_type_at_entry``
# and re-typed to :class:`day_type.DayType`. Any pre-v2 positions.json on
# disk fails the version check in ``_deserialize`` → ``PositionsStateSchemaError``
# → ``load`` returns empty state (fail-open). Demo bot, no migration.
_CURRENT_SCHEMA_VERSION = 2


class PositionsStateSchemaError(RuntimeError):
    """Raised when persisted JSON is parseable but has an unknown schema."""


# ---------------------------------------------------------------------------
# Top-level container
# ---------------------------------------------------------------------------


class PositionsState:
    """In-memory map keyed by ``deal_id`` with JSON persistence.

    The container is a thin wrapper around ``dict[str, ExecutionPosition]``;
    callers iterate, look up, or mutate via :py:meth:`upsert` /
    :py:meth:`remove`. Each mutation marks the container dirty so
    :py:meth:`save_if_dirty` can no-op when nothing changed.
    """

    def __init__(
        self,
        *,
        positions: Optional[dict[str, ExecutionPosition]] = None,
        path: Optional[Path] = None,
    ) -> None:
        self._positions: dict[str, ExecutionPosition] = (
            dict(positions) if positions else {}
        )
        self._path: Path = path or DEFAULT_STATE_PATH
        self._dirty: bool = False

    # --- Accessors ----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._positions)

    def __contains__(self, deal_id: object) -> bool:
        return deal_id in self._positions

    def get(self, deal_id: str) -> Optional[ExecutionPosition]:
        return self._positions.get(deal_id)

    def values(self) -> list[ExecutionPosition]:
        """Return a *copy* of the current position list (caller-owned)."""
        return list(self._positions.values())

    def keys(self) -> list[str]:
        return list(self._positions.keys())

    @property
    def path(self) -> Path:
        return self._path

    @property
    def dirty(self) -> bool:
        return self._dirty

    # --- Mutations ----------------------------------------------------------

    def upsert(self, position: ExecutionPosition) -> None:
        """Insert or replace a position by ``deal_id``."""
        if not position.deal_id:
            raise ValueError("ExecutionPosition.deal_id must be non-empty")
        self._positions[position.deal_id] = position
        self._dirty = True

    def remove(self, deal_id: str) -> Optional[ExecutionPosition]:
        """Remove and return a position by ``deal_id`` (``None`` if absent)."""
        removed = self._positions.pop(deal_id, None)
        if removed is not None:
            self._dirty = True
        return removed

    def mark_dirty(self) -> None:
        """Force a future :py:meth:`save_if_dirty` to write."""
        self._dirty = True

    # --- Persistence --------------------------------------------------------

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "PositionsState":
        """Read state from ``path``. Returns empty state on any error."""
        resolved = path or DEFAULT_STATE_PATH
        if not resolved.exists():
            return cls(path=resolved)
        try:
            text = resolved.read_text(encoding="utf-8")
            data = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "PositionsState load failed (%s); starting from empty state. "
                "Inspect %s manually to confirm.",
                exc,
                resolved,
            )
            return cls(path=resolved)

        try:
            positions = _deserialize(data)
        except PositionsStateSchemaError as exc:
            logger.warning(
                "PositionsState schema error (%s) loading %s; starting empty. "
                "Inspect file manually before clearing.",
                exc,
                resolved,
            )
            return cls(path=resolved)

        return cls(positions=positions, path=resolved)

    def save_if_dirty(self) -> bool:
        """Persist atomically iff :py:attr:`dirty` is True. Returns wrote-flag."""
        if not self._dirty:
            return False
        self._save_atomic()
        self._dirty = False
        return True

    def save(self) -> None:
        """Force a write irrespective of the dirty flag."""
        self._save_atomic()
        self._dirty = False

    # --- Internals ----------------------------------------------------------

    def _save_atomic(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = _serialize(self._positions)
        # tempfile + os.replace = atomic on POSIX. Even if the process
        # crashes mid-write, the previous file remains intact.
        fd, tmp_path_str = tempfile.mkstemp(
            prefix=".positions_",
            suffix=".tmp",
            dir=str(self._path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path_str, self._path)
        except Exception:
            # Clean up the temp file on any failure — never leave
            # half-written debris behind.
            try:
                os.unlink(tmp_path_str)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# (De)serialisation
# ---------------------------------------------------------------------------


def _serialize(positions: dict[str, ExecutionPosition]) -> dict:
    return {
        "schema_version": _CURRENT_SCHEMA_VERSION,
        "saved_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        "positions": [_position_to_dict(p) for p in positions.values()],
    }


def _position_to_dict(p: ExecutionPosition) -> dict:
    return {
        "deal_id": p.deal_id,
        "deal_reference": p.deal_reference,
        "pair": p.pair,
        "direction": p.direction.value,
        "day_type_at_entry": p.day_type_at_entry.value,
        "strategy_name": p.strategy_name,
        "size_units": p.size_units,
        "entry_price": p.entry_price,
        "initial_sl_price": p.initial_sl_price,
        "current_sl_price": p.current_sl_price,
        "suggested_tp_price": p.suggested_tp_price,
        "entry_time_utc": p.entry_time_utc.isoformat(),
        "signal_source_candle_ts": p.signal_source_candle_ts.isoformat(),
        "be_moved": p.be_moved,
        "trail_active": p.trail_active,
        "sl_history": [_amendment_to_dict(a) for a in p.sl_history],
    }


def _amendment_to_dict(a: SLAmendment) -> dict:
    return {
        "at_utc": a.at_utc.isoformat(),
        "from_price": a.from_price,
        "to_price": a.to_price,
        "reason": a.reason,
        "deal_id_or_reference": a.deal_id_or_reference,
    }


def _deserialize(data: object) -> dict[str, ExecutionPosition]:
    if not isinstance(data, dict):
        raise PositionsStateSchemaError(
            f"top-level payload must be dict, got {type(data).__name__}"
        )
    version = data.get("schema_version")
    if version != _CURRENT_SCHEMA_VERSION:
        raise PositionsStateSchemaError(
            f"unsupported schema_version={version!r}; "
            f"expected {_CURRENT_SCHEMA_VERSION}"
        )
    raw_positions = data.get("positions") or []
    if not isinstance(raw_positions, list):
        raise PositionsStateSchemaError(
            f"positions field must be list, got {type(raw_positions).__name__}"
        )
    out: dict[str, ExecutionPosition] = {}
    for entry in raw_positions:
        if not isinstance(entry, dict):
            raise PositionsStateSchemaError(
                f"position entry must be dict, got {type(entry).__name__}"
            )
        pos = _dict_to_position(entry)
        out[pos.deal_id] = pos
    return out


def _dict_to_position(entry: dict) -> ExecutionPosition:
    try:
        history = tuple(
            _dict_to_amendment(a) for a in (entry.get("sl_history") or [])
        )
        return ExecutionPosition(
            deal_id=str(entry["deal_id"]),
            deal_reference=str(entry["deal_reference"]),
            pair=str(entry["pair"]),
            direction=Direction(entry["direction"]),
            day_type_at_entry=DayType(entry["day_type_at_entry"]),
            strategy_name=str(entry["strategy_name"]),  # type: ignore[arg-type]
            size_units=float(entry["size_units"]),
            entry_price=float(entry["entry_price"]),
            initial_sl_price=float(entry["initial_sl_price"]),
            current_sl_price=float(entry["current_sl_price"]),
            suggested_tp_price=(
                float(entry["suggested_tp_price"])
                if entry.get("suggested_tp_price") is not None
                else None
            ),
            entry_time_utc=datetime.fromisoformat(entry["entry_time_utc"]),
            signal_source_candle_ts=datetime.fromisoformat(
                entry["signal_source_candle_ts"]
            ),
            be_moved=bool(entry.get("be_moved", False)),
            trail_active=bool(entry.get("trail_active", False)),
            sl_history=history,
        )
    except (KeyError, ValueError) as exc:
        raise PositionsStateSchemaError(
            f"invalid position entry: {exc}"
        ) from exc


def _dict_to_amendment(entry: dict) -> SLAmendment:
    return SLAmendment(
        at_utc=datetime.fromisoformat(entry["at_utc"]),
        from_price=float(entry["from_price"]),
        to_price=float(entry["to_price"]),
        reason=str(entry["reason"]),
        deal_id_or_reference=str(entry["deal_id_or_reference"]),
    )


__all__ = [
    "DEFAULT_STATE_PATH",
    "PositionsState",
    "PositionsStateSchemaError",
]
