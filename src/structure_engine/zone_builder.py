"""Level → zone construction and merging (spec §6).

Every raw level (swing price, session high/low, previous-day high/low,
equal-high/low cluster) is wrapped in an ATR-padded zone before scoring.
Nearby zones merge into one weighted-average zone so a cluster of
swings within a few pips is represented as a single level — not three
overlapping ones.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from config.pair_config import pip_to_price

from .constants import (
    DEFAULT_MIN_ZONE_PIPS,
    PAIR_MIN_ZONE_PIPS,
    TIMEFRAME_SCORE,
    ZONE_ATR_FRACTION,
    ZONE_MERGE_MULT,
)
from .types import Timeframe


@dataclass
class CandidateZone:
    """Pre-scoring zone — internal to the engine.

    Wraps a level price with an ATR-padded band and tracks the
    metadata the scorer will consume. Multiple :py:class:`CandidateZone`
    instances may merge into one via :py:func:`merge_zones`.

    Mutable on purpose: ``merge`` rewrites fields in-place during
    clustering. The engine wraps the final merged set into immutable
    :py:class:`StructureLevel` objects after scoring.
    """

    pair: str
    side: str  # "HIGH" / "LOW" — used during merge to keep sides separate
    price: float
    zone_low: float
    zone_high: float
    timeframe: Timeframe
    sources: list[str] = field(default_factory=list)
    touch_count: int = 0
    last_touched_ts: Optional[str] = None
    reaction_atr_mult: float = 0.0  # best-observed reaction strength, in ATR
    bars_since_last_touch: Optional[int] = None
    invalidated: bool = False
    is_equal_hl_cluster: bool = False
    is_session_level: bool = False
    session_kind: Optional[str] = None  # "prev_day" / "london" / "ny" / "asia"
    swing_strengths: list[float] = field(default_factory=list)
    debug: dict = field(default_factory=dict)
    # Cached output of ``score_zone`` — populated on the first call by
    # ``_score_with_cache`` to avoid recomputing the same zone up to 5×
    # per analysis cycle (L-5 review fix, 2026-05-16).
    _cached_score: Optional[tuple[float, dict]] = None


def half_width_for(pair: str, atr_m5: float) -> float:
    """Return ATR-padded zone half-width in price units.

    ``half_width = max(min_pips_in_price, atr_m5 * ZONE_ATR_FRACTION)``
    per spec §6. ``atr_m5`` of zero / NaN falls through to the pip
    floor — the engine never raises on missing ATR, callers can simply
    feed the latest M5 ATR.
    """
    min_pips = PAIR_MIN_ZONE_PIPS.get(pair.upper(), DEFAULT_MIN_ZONE_PIPS)
    pip_floor = pip_to_price(pair, min_pips)
    if atr_m5 is None or atr_m5 != atr_m5 or atr_m5 <= 0:  # NaN-safe
        return pip_floor
    return max(pip_floor, atr_m5 * ZONE_ATR_FRACTION)


def make_zone(
    *,
    pair: str,
    side: str,
    price: float,
    timeframe: Timeframe,
    half_width: float,
    source: str,
    swing_strength: Optional[float] = None,
) -> CandidateZone:
    """Wrap a single price into a CandidateZone."""
    z = CandidateZone(
        pair=pair.upper(),
        side=side,
        price=float(price),
        zone_low=float(price) - half_width,
        zone_high=float(price) + half_width,
        timeframe=timeframe,
        sources=[source],
    )
    if swing_strength is not None:
        z.swing_strengths.append(float(swing_strength))
    return z


def merge_zones(zones: list[CandidateZone]) -> list[CandidateZone]:
    """Cluster overlapping / near-adjacent zones into weighted averages.

    Two zones are clustered when:

    1. They share the same ``side`` (don't merge a swing high with a
       swing low even if they sit at the same price), and
    2. Their bands overlap OR sit within
       ``ZONE_MERGE_MULT * (half_a + half_b)`` of each other.

    Merged price is a timeframe-score-weighted average of all member
    prices — H1 levels anchor the merged price more than M5 noise
    (spec §6 "weighted average by timeframe score").
    """
    if not zones:
        return []

    # Sort by side then price so adjacent merges are O(N) once per side.
    sorted_zones = sorted(zones, key=lambda z: (z.side, z.price))
    out: list[CandidateZone] = []
    for z in sorted_zones:
        if not out:
            out.append(z)
            continue
        cur = out[-1]
        if cur.side != z.side:
            out.append(z)
            continue
        if _should_merge(cur, z):
            out[-1] = _merge_pair(cur, z)
        else:
            out.append(z)
    return out


def _should_merge(a: CandidateZone, b: CandidateZone) -> bool:
    half_a = (a.zone_high - a.zone_low) / 2.0
    half_b = (b.zone_high - b.zone_low) / 2.0
    distance = abs(a.price - b.price)
    threshold = ZONE_MERGE_MULT * (half_a + half_b)
    return distance <= threshold


def _merge_pair(a: CandidateZone, b: CandidateZone) -> CandidateZone:
    """Combine two CandidateZones using timeframe-weighted averaging."""
    weights = (TIMEFRAME_SCORE.get(a.timeframe, 1.0), TIMEFRAME_SCORE.get(b.timeframe, 1.0))
    total_w = weights[0] + weights[1]
    new_price = (a.price * weights[0] + b.price * weights[1]) / total_w
    new_low = min(a.zone_low, b.zone_low)
    new_high = max(a.zone_high, b.zone_high)
    # Use the higher-priority timeframe as the merged label.
    new_tf = a.timeframe if weights[0] >= weights[1] else b.timeframe
    merged = CandidateZone(
        pair=a.pair,
        side=a.side,
        price=new_price,
        zone_low=new_low,
        zone_high=new_high,
        timeframe=new_tf,
        sources=sorted(set(a.sources + b.sources)),
        touch_count=a.touch_count + b.touch_count,
        last_touched_ts=_pick_latest_ts(a.last_touched_ts, b.last_touched_ts),
        reaction_atr_mult=max(a.reaction_atr_mult, b.reaction_atr_mult),
        bars_since_last_touch=_pick_min(
            a.bars_since_last_touch, b.bars_since_last_touch
        ),
        invalidated=a.invalidated or b.invalidated,
        is_equal_hl_cluster=a.is_equal_hl_cluster or b.is_equal_hl_cluster,
        is_session_level=a.is_session_level or b.is_session_level,
        session_kind=a.session_kind or b.session_kind,
        swing_strengths=a.swing_strengths + b.swing_strengths,
    )
    return merged


def _pick_latest_ts(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if a is None:
        return b
    if b is None:
        return a
    # ISO-8601 strings sort lexicographically.
    return a if a >= b else b


def _pick_min(a: Optional[int], b: Optional[int]) -> Optional[int]:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


__all__ = ["CandidateZone", "half_width_for", "make_zone", "merge_zones"]
