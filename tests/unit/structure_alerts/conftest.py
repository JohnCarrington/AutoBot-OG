"""Test fixtures for the structure_alerts suite.

Builders for :class:`StructureState` / :class:`StructureLevel` that
supply sensible defaults for every field, so individual tests can
pin only what they care about (e.g., a HTF_BIAS_CHANGE test passes
``htf_bias=`` and lets everything else default).

Keeping these in conftest rather than inline in each test file means
a field added to the engine dataclasses surfaces once here, not
across a dozen test files.
"""
from __future__ import annotations

from typing import Any, Optional

from structure_engine.types import StructureLevel, StructureState


def make_level(
    *,
    pair: str = "GBPUSD",
    level_type: str = "SUPPORT",
    price: float = 1.30050,
    score: float = 7.0,
    timeframe: str = "H1",
    touch_count: int = 2,
    zone_low: Optional[float] = None,
    zone_high: Optional[float] = None,
    last_touched_ts: Optional[str] = None,
    source: str = "swing_h1",
    debug: Optional[dict[str, Any]] = None,
) -> StructureLevel:
    """Build a :class:`StructureLevel` with sensible defaults.

    ``zone_low`` / ``zone_high`` default to ±4 pips around ``price``
    for non-JPY pairs. Tests that don't care about the band leave
    them at default; tests that gate on band behaviour should
    override explicitly.
    """
    pip = 0.01 if pair.upper().endswith("JPY") else 0.0001
    half_band = 4 * pip
    if zone_low is None:
        zone_low = price - half_band
    if zone_high is None:
        zone_high = price + half_band
    return StructureLevel(
        pair=pair,
        level_type=level_type,  # type: ignore[arg-type]
        price=price,
        zone_low=zone_low,
        zone_high=zone_high,
        timeframe=timeframe,  # type: ignore[arg-type]
        score=score,
        touch_count=touch_count,
        last_touched_ts=last_touched_ts,
        source=source,
        debug=dict(debug) if debug else {},
    )


def make_state(
    *,
    pair: str = "GBPUSD",
    timestamp: str = "2026-05-16T09:00:00+00:00",
    is_valid: bool = True,
    htf_bias: str = "NEUTRAL",
    local_bias: str = "NEUTRAL",
    nearest_support: Optional[StructureLevel] = None,
    nearest_resistance: Optional[StructureLevel] = None,
    liquidity_above: Optional[StructureLevel] = None,
    liquidity_below: Optional[StructureLevel] = None,
    current_reaction: str = "NONE",
    acceptance_state: str = "NONE",
    structure_mode: str = "RANGE_BALANCE",
    confidence: float = 0.7,
    reason: str = "test",
    levels: Optional[list[StructureLevel]] = None,
    debug: Optional[dict[str, Any]] = None,
) -> StructureState:
    """Build a :class:`StructureState` with sensible defaults."""
    return StructureState(
        pair=pair,
        timestamp=timestamp,
        is_valid=is_valid,
        htf_bias=htf_bias,  # type: ignore[arg-type]
        local_bias=local_bias,  # type: ignore[arg-type]
        nearest_support=nearest_support,
        nearest_resistance=nearest_resistance,
        liquidity_above=liquidity_above,
        liquidity_below=liquidity_below,
        current_reaction=current_reaction,  # type: ignore[arg-type]
        acceptance_state=acceptance_state,  # type: ignore[arg-type]
        structure_mode=structure_mode,  # type: ignore[arg-type]
        confidence=confidence,
        reason=reason,
        levels=list(levels) if levels is not None else [],
        debug=dict(debug) if debug else {},
    )
