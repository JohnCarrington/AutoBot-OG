"""Structure Engine dataclasses and type aliases (Phase 11).

The Structure Engine emits a single ``StructureState`` per BAR_CLOSE
that strategies read instead of doing their own pattern detection. The
contract here is the only shape strategies see — see
``analyze_structure`` in :mod:`structure_engine.structure_state` for
the producer.

All dataclasses are frozen and JSON-serialisable so ``logging.py``
can write each snapshot to ``data/structure/structure_state.jsonl``
without bespoke encoders.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional


Direction = Literal["BULLISH", "BEARISH", "NEUTRAL"]

ReactionType = Literal[
    "NONE",
    "SUPPORT_REJECTION",
    "RESISTANCE_REJECTION",
    "SUPPORT_SWEEP_RECLAIM",
    "RESISTANCE_SWEEP_RECLAIM",
    "SUPPORT_ACCEPTANCE_BREAK",
    "RESISTANCE_ACCEPTANCE_BREAK",
    "FAILED_RECLAIM_BELOW_SUPPORT",
    "FAILED_RECLAIM_ABOVE_RESISTANCE",
    "RANGE_ROTATION",
]

AcceptanceState = Literal[
    "NONE",
    "ACCEPTED_ABOVE_RESISTANCE",
    "ACCEPTED_BELOW_SUPPORT",
    "REJECTED_ABOVE_RESISTANCE",
    "REJECTED_BELOW_SUPPORT",
    "INSIDE_RANGE",
]

StructureMode = Literal[
    "TREND_CONTINUATION",
    "RANGE_BALANCE",
    "VOLATILE_SWEEP_ZONE",
    "TRANSITION",
    "UNKNOWN",
]

LevelType = Literal["SUPPORT", "RESISTANCE", "LIQUIDITY_HIGH", "LIQUIDITY_LOW"]

Timeframe = Literal["H1", "M15", "M5"]


@dataclass(frozen=True)
class StructureLevel:
    """A single scored zone-of-interest in the order book of structure.

    A level may carry both an S/R role and a liquidity role — the engine
    classifies the *primary* role via ``level_type`` and exposes the
    full level list via :py:attr:`StructureState.levels` so strategies
    can re-classify per their own needs.

    ``zone_low`` / ``zone_high`` define an ATR-padded band around
    ``price`` (per spec §6). Touches are tested against the band, not
    the bare price.
    """

    pair: str
    level_type: LevelType
    price: float
    zone_low: float
    zone_high: float
    timeframe: Timeframe
    score: float
    touch_count: int
    last_touched_ts: Optional[str]
    source: str
    debug: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionState:
    """Session-window snapshot consumed by the Structure Engine.

    Phase 11 ships a stub: BotLoop passes ``session_state=None`` and the
    engine falls back to swing-derived levels only. The shape is defined
    here so a follow-up phase can wire a real ``SessionTracker`` without
    re-touching the engine API.

    All highs/lows are absolute prices (not pip deltas). ``None`` means
    "tracker hasn't observed enough of that session yet" — the engine
    treats it as "skip that level source".
    """

    current_time: datetime
    is_london: bool
    is_new_york: bool
    is_overlap: bool
    asia_high: Optional[float] = None
    asia_low: Optional[float] = None
    london_high: Optional[float] = None
    london_low: Optional[float] = None
    new_york_high: Optional[float] = None
    new_york_low: Optional[float] = None
    previous_day_high: Optional[float] = None
    previous_day_low: Optional[float] = None


@dataclass(frozen=True)
class StructureState:
    """The Structure Engine's per-bar output.

    Produced by :py:func:`analyze_structure`; consumed by every Phase 5
    strategy and (read-only) by Phase 11 debug logging. The ``debug``
    dict is mutable in shape — callers should not depend on specific
    keys beyond what the strategy spec gates on.
    """

    pair: str
    timestamp: str
    is_valid: bool

    htf_bias: Direction
    local_bias: Direction

    nearest_support: Optional[StructureLevel]
    nearest_resistance: Optional[StructureLevel]

    liquidity_above: Optional[StructureLevel]
    liquidity_below: Optional[StructureLevel]

    current_reaction: ReactionType
    acceptance_state: AcceptanceState
    structure_mode: StructureMode

    confidence: float
    reason: str
    levels: list[StructureLevel]
    debug: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Swing:
    """Raw swing point emitted by :py:func:`detect_swings`.

    Internal use only — the engine wraps these into :py:class:`StructureLevel`
    after zone-building and scoring. Kept as a separate type so the
    swing detector can be unit-tested in isolation from the rest of
    the pipeline.
    """

    type: Literal["HIGH", "LOW"]
    price: float
    timestamp: datetime
    timeframe: Timeframe
    strength: float
    bar_index: int


__all__ = [
    "AcceptanceState",
    "Direction",
    "LevelType",
    "ReactionType",
    "SessionState",
    "StructureLevel",
    "StructureMode",
    "StructureState",
    "Swing",
    "Timeframe",
]
