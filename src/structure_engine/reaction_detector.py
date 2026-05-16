"""Reaction classification at the nearest S/R zones (spec §10).

Strict 3-bar lookback (locked decision #4):

- bar **N-2** = setup / sweep / pierce candidate
- bar **N-1** = rejection / reclaim candle
- bar **N**   = confirmation candle (the just-closed bar)

There is no "current candle" ambiguity — the engine runs once per
BAR_CLOSE and only reads closed bars. The same candle window always
produces the same reaction.

Eight reaction types from spec §10 A–H. The detector tries each in
priority order:

1. acceptance breaks (clearest directional signal)
2. failed reclaims (continuation triggers)
3. sweep / reclaims
4. plain rejections
5. NONE
"""
from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from .constants import ACCEPTANCE_MIN_CLOSES, REACTION_LOOKBACK_BARS
from .types import AcceptanceState, ReactionType
from .zone_builder import CandidateZone


def classify_reaction(
    *,
    df_m5: pd.DataFrame,
    nearest_support: Optional[CandidateZone],
    nearest_resistance: Optional[CandidateZone],
) -> tuple[ReactionType, AcceptanceState, str]:
    """Return ``(reaction, acceptance_state, reason)``.

    The reason is a short human-readable string surfaced in
    ``StructureState.debug.reaction_reason``.
    """
    if df_m5 is None or len(df_m5) < REACTION_LOOKBACK_BARS:
        return "NONE", "NONE", "insufficient_bars"

    bars = df_m5.iloc[-REACTION_LOOKBACK_BARS:]
    setup = bars.iloc[0]
    rejection = bars.iloc[1]
    confirmation = bars.iloc[-1]

    # Failed reclaims first — most specific pattern (break + retest + fail).
    # Spec §10G calls this "one of the best bearish continuation triggers";
    # in practice the bars that produce a failed reclaim also satisfy the
    # acceptance-break shape, so failed-reclaim wins the tie.
    if nearest_support is not None:
        result = _detect_failed_reclaim_below_support(
            setup=setup,
            rejection=rejection,
            confirmation=confirmation,
            support=nearest_support,
        )
        if result is not None:
            return result + ("failed_reclaim_below_support",)
    if nearest_resistance is not None:
        result = _detect_failed_reclaim_above_resistance(
            setup=setup,
            rejection=rejection,
            confirmation=confirmation,
            resistance=nearest_resistance,
        )
        if result is not None:
            return result + ("failed_reclaim_above_resistance",)

    # Then acceptance breaks — directional but less specific than failed reclaim.
    if nearest_support is not None:
        result = _detect_support_acceptance_break(
            setup=setup,
            rejection=rejection,
            confirmation=confirmation,
            support=nearest_support,
        )
        if result is not None:
            return result + ("acceptance_below_support",)
    if nearest_resistance is not None:
        result = _detect_resistance_acceptance_break(
            setup=setup,
            rejection=rejection,
            confirmation=confirmation,
            resistance=nearest_resistance,
        )
        if result is not None:
            return result + ("acceptance_above_resistance",)

    # Sweep / reclaim.
    if nearest_support is not None:
        result = _detect_support_sweep_reclaim(
            setup=setup,
            rejection=rejection,
            confirmation=confirmation,
            support=nearest_support,
        )
        if result is not None:
            return result + ("support_swept_and_reclaimed",)
    if nearest_resistance is not None:
        result = _detect_resistance_sweep_reclaim(
            setup=setup,
            rejection=rejection,
            confirmation=confirmation,
            resistance=nearest_resistance,
        )
        if result is not None:
            return result + ("resistance_swept_and_reclaimed",)

    # Plain rejections.
    if nearest_support is not None:
        result = _detect_support_rejection(
            rejection=rejection,
            confirmation=confirmation,
            support=nearest_support,
        )
        if result is not None:
            return result + ("wick_into_support_then_close_above",)
    if nearest_resistance is not None:
        result = _detect_resistance_rejection(
            rejection=rejection,
            confirmation=confirmation,
            resistance=nearest_resistance,
        )
        if result is not None:
            return result + ("wick_into_resistance_then_close_below",)

    # Inside-range fallback when both bounds exist.
    if nearest_support is not None and nearest_resistance is not None:
        c = _safe_float(confirmation.get("close"))
        if (
            not math.isnan(c)
            and c > nearest_support.zone_high
            and c < nearest_resistance.zone_low
        ):
            return "NONE", "INSIDE_RANGE", "between_support_and_resistance"

    return "NONE", "NONE", "no_reaction_detected"


# ---------------------------------------------------------------------------
# Per-reaction detectors. Each returns ``(reaction, acceptance_state)`` or
# ``None``. Implemented as plain functions so unit tests can target each one.
# ---------------------------------------------------------------------------


def _detect_support_rejection(
    *, rejection, confirmation, support: CandidateZone
) -> Optional[tuple[ReactionType, AcceptanceState]]:
    """Spec §10A: wick into support zone, close above midpoint, hold."""
    rej_low = _safe_float(rejection.get("low"))
    rej_close = _safe_float(rejection.get("close"))
    rej_open = _safe_float(rejection.get("open"))
    conf_close = _safe_float(confirmation.get("close"))
    if any(math.isnan(v) for v in (rej_low, rej_close, rej_open, conf_close)):
        return None
    midpoint = (support.zone_low + support.zone_high) / 2.0
    if not (rej_low <= support.zone_high):
        return None
    if rej_close <= midpoint:
        return None
    # Lower wick on the rejection bar.
    body_bottom = min(rej_open, rej_close)
    if body_bottom - rej_low <= 0:
        return None
    # Confirmation holds above the zone midpoint.
    if conf_close <= midpoint:
        return None
    return "SUPPORT_REJECTION", "INSIDE_RANGE"


def _detect_resistance_rejection(
    *, rejection, confirmation, resistance: CandidateZone
) -> Optional[tuple[ReactionType, AcceptanceState]]:
    """Spec §10B: wick into resistance zone, close below midpoint, hold."""
    rej_high = _safe_float(rejection.get("high"))
    rej_close = _safe_float(rejection.get("close"))
    rej_open = _safe_float(rejection.get("open"))
    conf_close = _safe_float(confirmation.get("close"))
    if any(math.isnan(v) for v in (rej_high, rej_close, rej_open, conf_close)):
        return None
    midpoint = (resistance.zone_low + resistance.zone_high) / 2.0
    if not (rej_high >= resistance.zone_low):
        return None
    if rej_close >= midpoint:
        return None
    body_top = max(rej_open, rej_close)
    if rej_high - body_top <= 0:
        return None
    if conf_close >= midpoint:
        return None
    return "RESISTANCE_REJECTION", "INSIDE_RANGE"


def _detect_support_sweep_reclaim(
    *, setup, rejection, confirmation, support: CandidateZone
) -> Optional[tuple[ReactionType, AcceptanceState]]:
    """Spec §10C: low pierces support zone_low, close reclaims, confirm bullish."""
    setup_low = _safe_float(setup.get("low"))
    rej_low = _safe_float(rejection.get("low"))
    rej_close = _safe_float(rejection.get("close"))
    conf_close = _safe_float(confirmation.get("close"))
    conf_open = _safe_float(confirmation.get("open"))
    if any(math.isnan(v) for v in (rej_close, conf_close, conf_open)):
        return None
    midpoint = (support.zone_low + support.zone_high) / 2.0
    swept = (not math.isnan(setup_low) and setup_low < support.zone_low) or (
        not math.isnan(rej_low) and rej_low < support.zone_low
    )
    if not swept:
        return None
    # Reclaim: rejection close back above zone_high or midpoint.
    if rej_close < midpoint:
        return None
    # Confirmation bullish: close > open AND remains above support.
    if conf_close <= conf_open:
        return None
    if conf_close <= support.zone_low:
        return None
    return "SUPPORT_SWEEP_RECLAIM", "INSIDE_RANGE"


def _detect_resistance_sweep_reclaim(
    *, setup, rejection, confirmation, resistance: CandidateZone
) -> Optional[tuple[ReactionType, AcceptanceState]]:
    """Spec §10D: high pierces resistance zone_high, close reclaims back below."""
    setup_high = _safe_float(setup.get("high"))
    rej_high = _safe_float(rejection.get("high"))
    rej_close = _safe_float(rejection.get("close"))
    conf_close = _safe_float(confirmation.get("close"))
    conf_open = _safe_float(confirmation.get("open"))
    if any(math.isnan(v) for v in (rej_close, conf_close, conf_open)):
        return None
    midpoint = (resistance.zone_low + resistance.zone_high) / 2.0
    swept = (not math.isnan(setup_high) and setup_high > resistance.zone_high) or (
        not math.isnan(rej_high) and rej_high > resistance.zone_high
    )
    if not swept:
        return None
    if rej_close > midpoint:
        return None
    if conf_close >= conf_open:
        return None
    if conf_close >= resistance.zone_high:
        return None
    return "RESISTANCE_SWEEP_RECLAIM", "INSIDE_RANGE"


def _detect_support_acceptance_break(
    *, setup, rejection, confirmation, support: CandidateZone
) -> Optional[tuple[ReactionType, AcceptanceState]]:
    """Spec §10E: ``ACCEPTANCE_MIN_CLOSES`` consecutive closes below zone_low."""
    closes = []
    for bar in (setup, rejection, confirmation):
        c = _safe_float(bar.get("close"))
        if math.isnan(c):
            return None
        closes.append(c)
    # Use the trailing closes — count how many are below zone_low and
    # the latest must be below.
    below = [c < support.zone_low for c in closes]
    if closes[-1] >= support.zone_low:
        return None
    # Must have at least ACCEPTANCE_MIN_CLOSES consecutive trailing closes below.
    trailing = 0
    for flag in reversed(below):
        if flag:
            trailing += 1
        else:
            break
    if trailing < ACCEPTANCE_MIN_CLOSES:
        return None
    # M-4 review fix (2026-05-16): the previous version also required
    # a bearish body on the confirmation bar (``closes[-1] < conf_open``).
    # Spec §10E describes acceptance as "N consecutive closes below the
    # zone" with no requirement on the confirmation bar's body shape —
    # the body check was missing valid acceptance breaks (e.g. small
    # bullish pullback bar still below ``zone_low``). Dropped.
    return "SUPPORT_ACCEPTANCE_BREAK", "ACCEPTED_BELOW_SUPPORT"


def _detect_resistance_acceptance_break(
    *, setup, rejection, confirmation, resistance: CandidateZone
) -> Optional[tuple[ReactionType, AcceptanceState]]:
    """Spec §10F: ``ACCEPTANCE_MIN_CLOSES`` consecutive closes above zone_high."""
    closes = []
    for bar in (setup, rejection, confirmation):
        c = _safe_float(bar.get("close"))
        if math.isnan(c):
            return None
        closes.append(c)
    above = [c > resistance.zone_high for c in closes]
    if closes[-1] <= resistance.zone_high:
        return None
    trailing = 0
    for flag in reversed(above):
        if flag:
            trailing += 1
        else:
            break
    if trailing < ACCEPTANCE_MIN_CLOSES:
        return None
    # Body requirement dropped per spec §10F (M-4 review fix, 2026-05-16).
    # See _detect_support_acceptance_break for the rationale.
    return "RESISTANCE_ACCEPTANCE_BREAK", "ACCEPTED_ABOVE_RESISTANCE"


def _detect_failed_reclaim_below_support(
    *, setup, rejection, confirmation, support: CandidateZone
) -> Optional[tuple[ReactionType, AcceptanceState]]:
    """Spec §10G: break, retest from below, fail, bearish continuation."""
    setup_close = _safe_float(setup.get("close"))
    rej_high = _safe_float(rejection.get("high"))
    rej_close = _safe_float(rejection.get("close"))
    conf_close = _safe_float(confirmation.get("close"))
    conf_open = _safe_float(confirmation.get("open"))
    if any(math.isnan(v) for v in (setup_close, rej_high, rej_close, conf_close, conf_open)):
        return None
    # Setup: prior break below support zone.
    if setup_close >= support.zone_low:
        return None
    # Rejection: retest from underneath — high touches the zone but close
    # fails to reclaim above the zone.
    if rej_high < support.zone_low:
        return None
    if rej_close >= support.zone_low:
        return None
    # Confirmation: bearish bar that closes further below.
    if conf_close >= conf_open:
        return None
    if conf_close >= rej_close:
        return None
    return "FAILED_RECLAIM_BELOW_SUPPORT", "REJECTED_BELOW_SUPPORT"


def _detect_failed_reclaim_above_resistance(
    *, setup, rejection, confirmation, resistance: CandidateZone
) -> Optional[tuple[ReactionType, AcceptanceState]]:
    """Spec §10H: break, retest from above, fail, bullish continuation."""
    setup_close = _safe_float(setup.get("close"))
    rej_low = _safe_float(rejection.get("low"))
    rej_close = _safe_float(rejection.get("close"))
    conf_close = _safe_float(confirmation.get("close"))
    conf_open = _safe_float(confirmation.get("open"))
    if any(math.isnan(v) for v in (setup_close, rej_low, rej_close, conf_close, conf_open)):
        return None
    if setup_close <= resistance.zone_high:
        return None
    if rej_low > resistance.zone_high:
        return None
    if rej_close <= resistance.zone_high:
        return None
    if conf_close <= conf_open:
        return None
    if conf_close <= rej_close:
        return None
    return "FAILED_RECLAIM_ABOVE_RESISTANCE", "REJECTED_ABOVE_RESISTANCE"


def _safe_float(value) -> float:
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


__all__ = ["classify_reaction"]
