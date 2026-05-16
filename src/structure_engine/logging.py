"""JSON-Lines logging for the Structure Engine (spec §15).

One line per analysis cycle into ``data/structure/structure_state.jsonl``.
The line carries the same condensed payload the spec lists — no full
DataFrames, just the key fields a human / dashboard would scan to
explain why a strategy fired (or didn't).

Logging is **gated** on ``STRUCTURE_LOG_ENABLED`` (env var). The
default is off — Phase 11 ships with the engine wired but logging
silent until ops flip the flag.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from .constants import (
    STRUCTURE_LEVELS_MAX_PER_RECORD,
    STRUCTURE_LOG_ENABLED,
    STRUCTURE_LOG_PATH,
)
from .types import StructureLevel, StructureState


_logger = logging.getLogger(__name__)
_lock = threading.Lock()


def _is_enabled() -> bool:
    """Read the toggle at call time.

    L-2 review fix (2026-05-16). Importing constants bound the env var
    once at startup; reading per-call lets ops flip the flag on a
    running process via env update. The module-level ``STRUCTURE_LOG_ENABLED``
    is kept as the fallback so tests that monkeypatch the attribute
    directly still work.
    """
    raw = os.getenv("STRUCTURE_LOG_ENABLED")
    if raw is None:
        return STRUCTURE_LOG_ENABLED
    return raw.lower() in ("1", "true", "yes")


def _log_path() -> str:
    """Read the log path at call time (L-2 review fix)."""
    return os.getenv("STRUCTURE_LOG_PATH", STRUCTURE_LOG_PATH)


def log_structure_state(state: StructureState) -> None:
    """Append a one-line JSON record describing ``state``.

    A no-op when ``STRUCTURE_LOG_ENABLED`` is false. All filesystem
    errors are caught and logged via the standard logger — this writer
    must never crash the BAR_CLOSE pipeline.
    """
    if not _is_enabled():
        return
    payload = _to_payload(state)
    line = json.dumps(payload, separators=(",", ":"))
    try:
        path = _log_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with _lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError as exc:  # pragma: no cover — defensive
        _logger.warning(
            "structure_engine: failed to write %s: %s", _log_path(), exc
        )


def _to_payload(state: StructureState) -> dict[str, Any]:
    """Render :class:`StructureState` as a JSON-friendly dict.

    Phase 12 refinement A (2026-05-16): adds ``nearest_support_score``,
    ``nearest_resistance_score``, and a compact ``levels`` list so
    :func:`structure_alerts.hydration.load_latest_structure_state_per_pair`
    can rebuild a high-fidelity ``StructureState`` after a restart.
    The compact levels list carries ``{p, s, sc, tf}`` per entry and
    is capped at :data:`STRUCTURE_LEVELS_MAX_PER_RECORD` (default 30)
    sorted by score descending. DataFrame references and per-zone
    debug dicts remain stripped (spec §15).
    """
    return {
        "timestamp": state.timestamp,
        "pair": state.pair,
        "is_valid": state.is_valid,
        "htf_bias": state.htf_bias,
        "local_bias": state.local_bias,
        "nearest_support": _level_price(state.nearest_support),
        "nearest_support_score": _level_score(state.nearest_support),
        "nearest_resistance": _level_price(state.nearest_resistance),
        "nearest_resistance_score": _level_score(state.nearest_resistance),
        "liquidity_above": _level_price(state.liquidity_above),
        "liquidity_below": _level_price(state.liquidity_below),
        "current_reaction": state.current_reaction,
        "acceptance_state": state.acceptance_state,
        "structure_mode": state.structure_mode,
        "confidence": round(state.confidence, 4),
        "reason": state.reason,
        "levels": _compact_levels(state.levels),
    }


def _level_price(level: StructureLevel | None) -> float | None:
    if level is None:
        return None
    return level.price


def _level_score(level: StructureLevel | None) -> float | None:
    if level is None:
        return None
    return round(level.score, 4)


def _compact_levels(levels: list[StructureLevel]) -> list[dict[str, Any]]:
    """Top-N levels by score in compact form (Phase 12 refinement A).

    Each entry is ``{"p": price, "s": level_type, "sc": score, "tf":
    timeframe}``. Sorted by score descending (stable — ties keep
    engine-determined order) and capped at
    :data:`STRUCTURE_LEVELS_MAX_PER_RECORD`. Empty input produces an
    empty list.

    No zone_low / zone_high / touch_count / debug — those bloat the
    record without helping the C-4 hydration consumer
    (:mod:`structure_alerts.hydration` only reads price / side /
    score / timeframe for diff-layer purposes).
    """
    if not levels:
        return []
    top = sorted(levels, key=lambda l: l.score, reverse=True)[
        :STRUCTURE_LEVELS_MAX_PER_RECORD
    ]
    return [
        {
            "p": level.price,
            "s": level.level_type,
            "sc": round(level.score, 4),
            "tf": level.timeframe,
        }
        for level in top
    ]


__all__ = ["log_structure_state"]
