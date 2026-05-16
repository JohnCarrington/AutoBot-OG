"""Zone scoring per spec §8.

A :py:class:`CandidateZone` accumulates evidence as the engine
pipelines through swings → sessions → merges → touch counting →
reaction observation. ``score_zone`` reduces those signals to a single
``0..SCORE_CAP`` float that strategies gate on (spec §13:
``score >= STRONG_LEVEL_THRESHOLD``).
"""
from __future__ import annotations

from .constants import (
    INVALIDATION_PENALTY,
    LIQUIDITY_SCORE_EQUAL_HL,
    REACTION_MEDIUM_ATR_MULT,
    REACTION_SCORE_MEDIUM,
    REACTION_SCORE_STRONG,
    REACTION_SCORE_WEAK,
    REACTION_STRONG_ATR_MULT,
    RECENCY_BARS_MEDIUM,
    RECENCY_BARS_RECENT,
    RECENCY_SCORE_MEDIUM,
    RECENCY_SCORE_OLD,
    RECENCY_SCORE_RECENT,
    SCORE_CAP,
    SESSION_SCORE_ASIA,
    SESSION_SCORE_LONDON,
    SESSION_SCORE_NY,
    SESSION_SCORE_PREV_DAY,
    SWING_STRENGTH_WEIGHT,
    TIMEFRAME_SCORE,
    TOUCH_SCORE_1,
    TOUCH_SCORE_2,
    TOUCH_SCORE_3PLUS,
)
from .zone_builder import CandidateZone


_SESSION_SCORE_BY_KIND: dict[str, float] = {
    "prev_day": SESSION_SCORE_PREV_DAY,
    "london": SESSION_SCORE_LONDON,
    "ny": SESSION_SCORE_NY,
    "asia": SESSION_SCORE_ASIA,
}


def score_zone(zone: CandidateZone) -> tuple[float, dict[str, float]]:
    """Return ``(score, components)`` for ``zone``.

    ``components`` is the breakdown the engine surfaces in
    ``StructureState.debug.support_score_components`` /
    ``resistance_score_components`` so operators can explain a level's
    score line-by-line.
    """
    timeframe = TIMEFRAME_SCORE.get(zone.timeframe, 0.0)
    touch = _touch_component(zone.touch_count)
    reaction = _reaction_component(zone.reaction_atr_mult)
    recency = _recency_component(zone.bars_since_last_touch)
    liquidity = LIQUIDITY_SCORE_EQUAL_HL if zone.is_equal_hl_cluster else 0.0
    session = (
        _SESSION_SCORE_BY_KIND.get(zone.session_kind, 0.0)
        if zone.is_session_level and zone.session_kind is not None
        else 0.0
    )
    # Swing strength contribution (M-1 review fix, 2026-05-16). Each
    # member swing carries its own 0–1 strength score from
    # ``swing_detector._compute_strength``; we take the max so a single
    # very-strong swing in the cluster is what's remembered. Multiplied
    # by ``SWING_STRENGTH_WEIGHT`` so the contribution is small relative
    # to timeframe / touch / reaction.
    swing_strength_component = (
        max(zone.swing_strengths) * SWING_STRENGTH_WEIGHT
        if zone.swing_strengths
        else 0.0
    )
    penalty = INVALIDATION_PENALTY if zone.invalidated else 0.0

    raw = (
        timeframe
        + touch
        + reaction
        + recency
        + liquidity
        + session
        + swing_strength_component
        - penalty
    )
    capped = max(0.0, min(SCORE_CAP, raw))

    components = {
        "timeframe": float(timeframe),
        "touches": float(touch),
        "reaction": float(reaction),
        "recency": float(recency),
        "liquidity": float(liquidity),
        "session": float(session),
        "swing_strength": float(swing_strength_component),
        "penalty": float(-penalty),
    }
    return capped, components


def _touch_component(touches: int) -> float:
    if touches <= 0:
        return 0.0
    if touches == 1:
        return TOUCH_SCORE_1
    if touches == 2:
        return TOUCH_SCORE_2
    return TOUCH_SCORE_3PLUS


def _reaction_component(reaction_atr_mult: float) -> float:
    """Map post-touch displacement (in ATRs) to a score band."""
    if reaction_atr_mult <= 0.0:
        return 0.0
    if reaction_atr_mult >= REACTION_STRONG_ATR_MULT:
        return REACTION_SCORE_STRONG
    if reaction_atr_mult >= REACTION_MEDIUM_ATR_MULT:
        return REACTION_SCORE_MEDIUM
    return REACTION_SCORE_WEAK


def _recency_component(bars_since: int | None) -> float:
    if bars_since is None:
        return RECENCY_SCORE_OLD
    if bars_since <= RECENCY_BARS_RECENT:
        return RECENCY_SCORE_RECENT
    if bars_since <= RECENCY_BARS_MEDIUM:
        return RECENCY_SCORE_MEDIUM
    return RECENCY_SCORE_OLD


__all__ = ["score_zone"]
