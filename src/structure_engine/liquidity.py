"""Liquidity-pool selection per spec §12.

Liquidity levels are *targets*, not entry levels. They sit above (for
shorts) or below (for longs) the current price and represent obvious
clusters of resting orders / stops. Sources:

- Equal-high / equal-low swing clusters (already merged into a single
  zone with ``is_equal_hl_cluster=True``).
- Previous-day high / low.
- London / NY session highs / lows.
- Recent H1 / M15 swing-high/low clusters.

A level can carry **both** roles — being marked as
:py:attr:`StructureLevel.level_type` ``SUPPORT`` doesn't prevent it
from also surfacing as ``liquidity_below`` when it qualifies (this is
locked decision #6).
"""
from __future__ import annotations

from typing import Optional

from .zone_builder import CandidateZone


def pick_liquidity_above(
    *, current_price: float, zones: list[CandidateZone]
) -> Optional[CandidateZone]:
    """Return the nearest HIGH-side zone above ``current_price``.

    Preference order (first non-empty):

    1. Equal-high cluster zones (``is_equal_hl_cluster``).
    2. Session levels (``is_session_level``).
    3. Any HIGH-side zone.

    Within each preference tier we pick the nearest by absolute distance.
    """
    above = [z for z in zones if z.side == "HIGH" and z.price > current_price]
    if not above:
        return None
    return _pick_with_preferences(current_price, above)


def pick_liquidity_below(
    *, current_price: float, zones: list[CandidateZone]
) -> Optional[CandidateZone]:
    """Return the nearest LOW-side zone below ``current_price``."""
    below = [z for z in zones if z.side == "LOW" and z.price < current_price]
    if not below:
        return None
    return _pick_with_preferences(current_price, below)


def _pick_with_preferences(
    current_price: float, candidates: list[CandidateZone]
) -> Optional[CandidateZone]:
    by_pref = [
        [z for z in candidates if z.is_equal_hl_cluster],
        [z for z in candidates if z.is_session_level],
        candidates,
    ]
    for tier in by_pref:
        if tier:
            return min(tier, key=lambda z: abs(z.price - current_price))
    return None


__all__ = ["pick_liquidity_above", "pick_liquidity_below"]
