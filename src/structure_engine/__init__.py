"""Structure Engine (Phase 11).

Public surface:

- :py:func:`analyze_structure` — main entry point. Reads enriched
  candles + regime/session snapshots, returns a single
  :py:class:`StructureState`.
- :py:class:`StructureState`, :py:class:`StructureLevel`,
  :py:class:`SessionState` — public dataclasses strategies / loggers
  consume.
- :py:func:`log_structure_state` — env-gated JSONL writer.

The engine is **non-destructive** with respect to the legacy
``structure`` module: ``src/structure/fractals.py`` and
``src/structure/state.py`` continue to serve Phase 3 regime classifier
and Phase 6 SL trailing. A follow-up phase will migrate those callers
and retire the legacy module.

See ``src/structure_engine/MODULE.md`` for module ownership and the
EMA warm-up / regime-vs-mode-mode design notes.
"""
from .logging import log_structure_state
from .structure_state import analyze_structure
from .types import (
    AcceptanceState,
    Direction,
    LevelType,
    ReactionType,
    SessionState,
    StructureLevel,
    StructureMode,
    StructureState,
    Timeframe,
)

__all__ = [
    "AcceptanceState",
    "Direction",
    "LevelType",
    "ReactionType",
    "SessionState",
    "StructureLevel",
    "StructureMode",
    "StructureState",
    "Timeframe",
    "analyze_structure",
    "log_structure_state",
]
