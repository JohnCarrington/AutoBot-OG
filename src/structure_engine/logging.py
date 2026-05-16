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

from .constants import STRUCTURE_LOG_ENABLED, STRUCTURE_LOG_PATH
from .types import StructureLevel, StructureState


_logger = logging.getLogger(__name__)
_lock = threading.Lock()


def log_structure_state(state: StructureState) -> None:
    """Append a one-line JSON record describing ``state``.

    A no-op when ``STRUCTURE_LOG_ENABLED`` is false. All filesystem
    errors are caught and logged via the standard logger — this writer
    must never crash the BAR_CLOSE pipeline.
    """
    if not STRUCTURE_LOG_ENABLED:
        return
    payload = _to_payload(state)
    line = json.dumps(payload, separators=(",", ":"))
    try:
        path = STRUCTURE_LOG_PATH
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with _lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError as exc:  # pragma: no cover — defensive
        _logger.warning(
            "structure_engine: failed to write %s: %s", STRUCTURE_LOG_PATH, exc
        )


def _to_payload(state: StructureState) -> dict[str, Any]:
    return {
        "timestamp": state.timestamp,
        "pair": state.pair,
        "is_valid": state.is_valid,
        "htf_bias": state.htf_bias,
        "local_bias": state.local_bias,
        "nearest_support": _level_price(state.nearest_support),
        "nearest_resistance": _level_price(state.nearest_resistance),
        "liquidity_above": _level_price(state.liquidity_above),
        "liquidity_below": _level_price(state.liquidity_below),
        "current_reaction": state.current_reaction,
        "acceptance_state": state.acceptance_state,
        "structure_mode": state.structure_mode,
        "confidence": round(state.confidence, 4),
        "reason": state.reason,
    }


def _level_price(level: StructureLevel | None) -> float | None:
    if level is None:
        return None
    return level.price


__all__ = ["log_structure_state"]
