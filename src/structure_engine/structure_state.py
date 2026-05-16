"""Public entry point: ``analyze_structure``.

Runs the spec §4 sequence:

1. Validate input candle length
2. Detect swings on H1, M15, M5
3. Build candidate levels from swings + session + previous day
4. Merge nearby zones
5. Touch / reaction / recency accounting
6. Score each zone
7. Identify nearest support / resistance
8. Identify liquidity above / below
9. Detect current reaction at nearest levels
10. Determine HTF / local bias
11. Classify structure mode
12. Return :py:class:`StructureState`

The function is pure: same inputs → same output. No file I/O, no
broker calls. Logging is the caller's responsibility (see
``structure_engine.logging``).
"""
from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from .bias_detector import detect_htf_bias, detect_local_bias
from .constants import (
    EQUAL_HL_MIN_COUNT,
    MIN_CANDLES_H1,
    MIN_CANDLES_M15,
    MIN_CANDLES_M5,
    NEAR_LIQUIDITY_ATR_MULT,
)
from .liquidity import pick_liquidity_above, pick_liquidity_below
from .mode_classifier import classify_mode
from .reaction_detector import classify_reaction
from .scoring import score_zone
from .swing_detector import detect_swings
from .types import (
    Direction,
    SessionState,
    StructureLevel,
    StructureState,
    Swing,
    Timeframe,
)
from .zone_builder import CandidateZone, half_width_for, make_zone, merge_zones


def analyze_structure(
    pair: str,
    candles_m5: pd.DataFrame,
    candles_m15: pd.DataFrame,
    candles_h1: pd.DataFrame,
    regime_state: dict,
    session_state: Optional[SessionState] = None,
) -> StructureState:
    """Produce a :py:class:`StructureState` snapshot for ``pair`` at the
    latest M5 close in ``candles_m5``.

    ``regime_state`` is accepted for symmetry with the dispatcher
    signature and to seed future mode-classifier inputs; v1 does not
    gate any decision on it directly (regime routing is the
    dispatcher's job).

    ``session_state`` may be ``None`` — Phase 11 ships with a stub.
    When ``None`` the engine skips session-level sources and relies on
    swings only.
    """
    pair_upper = pair.upper()
    timestamp = _latest_timestamp(candles_m5)

    # 1. Validate.
    if not _has_min_candles(candles_m5, MIN_CANDLES_M5):
        return _invalid_state(
            pair_upper, timestamp, reason="insufficient_candles_m5"
        )

    atr_m5 = _latest_atr(candles_m5)
    half_width = half_width_for(pair_upper, atr_m5)

    # 2. Swings.
    swings_h1 = (
        detect_swings(candles_h1, "H1")
        if _has_min_candles(candles_h1, MIN_CANDLES_H1)
        else []
    )
    swings_m15 = (
        detect_swings(candles_m15, "M15")
        if _has_min_candles(candles_m15, MIN_CANDLES_M15)
        else []
    )
    swings_m5 = detect_swings(candles_m5, "M5")

    # 3 + 4. Build + merge candidate zones.
    raw_zones: list[CandidateZone] = []
    raw_zones.extend(_zones_from_swings(pair_upper, swings_h1, half_width))
    raw_zones.extend(_zones_from_swings(pair_upper, swings_m15, half_width))
    raw_zones.extend(_zones_from_swings(pair_upper, swings_m5, half_width))
    raw_zones.extend(
        _zones_from_session(pair_upper, session_state, half_width)
    )

    merged = merge_zones(raw_zones)
    _mark_equal_hl_clusters(merged)
    _accumulate_touches(merged, candles_m5)

    # Current price = latest M5 close.
    current_price = _latest_close(candles_m5)

    # 5. Identify nearest support / resistance (after scoring).
    levels = _wrap_levels(merged)
    nearest_support_zone = _nearest_zone(
        merged, side="LOW", current_price=current_price, prefer_below=True
    )
    nearest_resistance_zone = _nearest_zone(
        merged, side="HIGH", current_price=current_price, prefer_below=False
    )

    nearest_support = (
        _zone_to_level(pair_upper, nearest_support_zone, level_type="SUPPORT")
        if nearest_support_zone is not None
        else None
    )
    nearest_resistance = (
        _zone_to_level(
            pair_upper, nearest_resistance_zone, level_type="RESISTANCE"
        )
        if nearest_resistance_zone is not None
        else None
    )

    # 6. Liquidity.
    liquidity_above_zone = pick_liquidity_above(
        current_price=current_price, zones=merged
    )
    liquidity_below_zone = pick_liquidity_below(
        current_price=current_price, zones=merged
    )
    liquidity_above = (
        _zone_to_level(
            pair_upper, liquidity_above_zone, level_type="LIQUIDITY_HIGH"
        )
        if liquidity_above_zone is not None
        else None
    )
    liquidity_below = (
        _zone_to_level(
            pair_upper, liquidity_below_zone, level_type="LIQUIDITY_LOW"
        )
        if liquidity_below_zone is not None
        else None
    )

    # 7. Reaction.
    reaction, acceptance, reaction_reason = classify_reaction(
        df_m5=candles_m5,
        nearest_support=nearest_support_zone,
        nearest_resistance=nearest_resistance_zone,
    )

    # M-5 review fix (2026-05-16): wire invalidation_penalty. An
    # ACCEPTANCE_BREAK is by definition "the zone has been broken and
    # accepted beyond" — the broken zone loses score on the next cycle.
    # Re-wrap the affected level after marking so the score reflected
    # in StructureState.nearest_* matches the penalty.
    if reaction == "SUPPORT_ACCEPTANCE_BREAK" and nearest_support_zone is not None:
        nearest_support_zone.invalidated = True
        nearest_support_zone._cached_score = None  # invalidate cache
        nearest_support = _zone_to_level(
            pair_upper, nearest_support_zone, level_type="SUPPORT"
        )
    elif reaction == "RESISTANCE_ACCEPTANCE_BREAK" and nearest_resistance_zone is not None:
        nearest_resistance_zone.invalidated = True
        nearest_resistance_zone._cached_score = None  # invalidate cache
        nearest_resistance = _zone_to_level(
            pair_upper, nearest_resistance_zone, level_type="RESISTANCE"
        )

    # 8. Bias.
    htf_bias, htf_debug = detect_htf_bias(candles_h1)
    local_bias, local_debug = detect_local_bias(candles_m5, candles_m15)

    # 9. Mode.
    near_liquidity = _price_near_liquidity(
        current_price=current_price,
        atr_m5=atr_m5,
        liquidity_above_zone=liquidity_above_zone,
        liquidity_below_zone=liquidity_below_zone,
    )
    structure_mode, mode_reason = classify_mode(
        df_m5=candles_m5,
        df_h1=candles_h1,
        htf_bias=htf_bias,
        local_bias=local_bias,
        current_reaction=reaction,
        near_liquidity=near_liquidity,
    )

    # 10. Confidence (refinement B — explicit None guards).
    confidence = _compute_confidence(nearest_support, nearest_resistance)

    # 11. Debug payload.
    debug = {
        "candidate_levels": len(raw_zones),
        "merged_zones": len(merged),
        "swings_h1": len(swings_h1),
        "swings_m15": len(swings_m15),
        "swings_m5": len(swings_m5),
        "current_price": current_price,
        "atr_m5": atr_m5,
        "zone_half_width": half_width,
        "htf_bias_reason": htf_debug,
        "local_bias_reason": local_debug,
        "structure_mode_reason": mode_reason,
        "reaction_reason": reaction_reason,
        "regime_state": _summarise_regime(regime_state),
        "session_state_present": session_state is not None,
    }
    if nearest_support is not None:
        debug["nearest_support_price"] = nearest_support.price
        debug["support_score_components"] = nearest_support.debug.get(
            "score_components", {}
        )
    if nearest_resistance is not None:
        debug["nearest_resistance_price"] = nearest_resistance.price
        debug["resistance_score_components"] = nearest_resistance.debug.get(
            "score_components", {}
        )

    reason = _summarise_reason(
        htf_bias=htf_bias,
        local_bias=local_bias,
        structure_mode=structure_mode,
        reaction=reaction,
    )

    return StructureState(
        pair=pair_upper,
        timestamp=timestamp,
        is_valid=True,
        htf_bias=htf_bias,
        local_bias=local_bias,
        nearest_support=nearest_support,
        nearest_resistance=nearest_resistance,
        liquidity_above=liquidity_above,
        liquidity_below=liquidity_below,
        current_reaction=reaction,
        acceptance_state=acceptance,
        structure_mode=structure_mode,
        confidence=confidence,
        reason=reason,
        levels=levels,
        debug=debug,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invalid_state(pair: str, timestamp: str, *, reason: str) -> StructureState:
    return StructureState(
        pair=pair,
        timestamp=timestamp,
        is_valid=False,
        htf_bias="NEUTRAL",
        local_bias="NEUTRAL",
        nearest_support=None,
        nearest_resistance=None,
        liquidity_above=None,
        liquidity_below=None,
        current_reaction="NONE",
        acceptance_state="NONE",
        structure_mode="UNKNOWN",
        confidence=0.0,
        reason=reason,
        levels=[],
        debug={"reason": reason},
    )


def _has_min_candles(df: pd.DataFrame, minimum: int) -> bool:
    return df is not None and len(df) >= minimum


def _latest_timestamp(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return ""
    idx = df.index[-1]
    if isinstance(idx, pd.Timestamp):
        return idx.isoformat()
    return str(idx)


def _latest_atr(df: pd.DataFrame) -> float:
    for col in ("atr_14", "atr_m5", "atr"):
        if col in df.columns:
            v = df[col].iloc[-1]
            try:
                f = float(v)
                if math.isnan(f):
                    continue
                return f
            except (TypeError, ValueError):
                continue
    return 0.0


def _latest_close(df: pd.DataFrame) -> float:
    if "close" not in df.columns or df.empty:
        return float("nan")
    return float(df["close"].iloc[-1])


def _zones_from_swings(
    pair: str, swings: list[Swing], half_width: float
) -> list[CandidateZone]:
    zones: list[CandidateZone] = []
    for s in swings:
        side = "HIGH" if s.type == "HIGH" else "LOW"
        zones.append(
            make_zone(
                pair=pair,
                side=side,
                price=s.price,
                timeframe=s.timeframe,
                half_width=half_width,
                source=f"swing_{s.timeframe.lower()}",
                swing_strength=s.strength,
            )
        )
    return zones


def _zones_from_session(
    pair: str, session: Optional[SessionState], half_width: float
) -> list[CandidateZone]:
    if session is None:
        return []
    zones: list[CandidateZone] = []
    pairs = [
        ("HIGH", session.previous_day_high, "previous_day_high", "prev_day"),
        ("LOW", session.previous_day_low, "previous_day_low", "prev_day"),
        ("HIGH", session.london_high, "london_high", "london"),
        ("LOW", session.london_low, "london_low", "london"),
        ("HIGH", session.new_york_high, "new_york_high", "ny"),
        ("LOW", session.new_york_low, "new_york_low", "ny"),
        ("HIGH", session.asia_high, "asia_high", "asia"),
        ("LOW", session.asia_low, "asia_low", "asia"),
    ]
    for side, price, source, kind in pairs:
        if price is None:
            continue
        # Session levels use H1 timeframe weight — they're macro structure.
        z = make_zone(
            pair=pair,
            side=side,
            price=price,
            timeframe="H1",
            half_width=half_width,
            source=source,
        )
        z.is_session_level = True
        z.session_kind = kind
        zones.append(z)
    return zones


def _mark_equal_hl_clusters(zones: list[CandidateZone]) -> None:
    """Flag zones that absorbed ``EQUAL_HL_MIN_COUNT`` or more swings.

    A merged zone with N member swings on the same side is an
    equal-high (or equal-low) cluster — the canonical liquidity pool.

    Counts ``swing_strengths`` rather than ``sources``. ``sources`` is
    deduped via ``sorted(set(...))`` in ``merge_zones._merge_pair``,
    so three H1 swings clustering at one price collapse to a single
    ``"swing_h1"`` source string — but the underlying ``swing_strengths``
    list preserves one entry per member swing (per H-2 review fix
    2026-05-16).
    """
    for z in zones:
        if len(z.swing_strengths) >= EQUAL_HL_MIN_COUNT:
            z.is_equal_hl_cluster = True


def _accumulate_touches(
    zones: list[CandidateZone], df_m5: pd.DataFrame
) -> None:
    """Count how many M5 bars touch each zone (wick overlap = touch).

    Locked decision #8: a touch is any bar whose ``[low, high]`` range
    overlaps the zone band. Updates ``touch_count``, ``last_touched_ts``,
    ``bars_since_last_touch``, and the reaction strength estimate.
    """
    if df_m5 is None or df_m5.empty:
        return
    highs = df_m5["high"].to_numpy(dtype=float)
    lows = df_m5["low"].to_numpy(dtype=float)
    closes = df_m5["close"].to_numpy(dtype=float)
    atrs = (
        df_m5["atr_14"].to_numpy(dtype=float)
        if "atr_14" in df_m5.columns
        else None
    )
    timestamps = list(df_m5.index)
    total = len(df_m5)
    for zone in zones:
        touches = 0
        last_idx: Optional[int] = None
        max_reaction = 0.0
        for i in range(total):
            high_i = highs[i]
            low_i = lows[i]
            if math.isnan(high_i) or math.isnan(low_i):
                continue
            # Wick overlap with the zone.
            if high_i < zone.zone_low or low_i > zone.zone_high:
                continue
            touches += 1
            last_idx = i
            # Reaction strength: distance the next 3 closes travelled
            # away from the zone midpoint, in ATR units. Best-of-3.
            atr_i = atrs[i] if atrs is not None else float("nan")
            if atrs is not None and not math.isnan(atr_i) and atr_i > 0:
                midpoint = (zone.zone_low + zone.zone_high) / 2.0
                end = min(total, i + 4)
                for j in range(i + 1, end):
                    c = closes[j]
                    if math.isnan(c):
                        continue
                    if zone.side == "LOW":
                        travel = c - midpoint
                    else:
                        travel = midpoint - c
                    if travel <= 0:
                        continue
                    mult = travel / atr_i
                    if mult > max_reaction:
                        max_reaction = mult
        zone.touch_count = touches
        zone.reaction_atr_mult = max_reaction
        if last_idx is not None:
            ts = timestamps[last_idx]
            zone.last_touched_ts = (
                ts.isoformat() if isinstance(ts, pd.Timestamp) else str(ts)
            )
            zone.bars_since_last_touch = (total - 1) - last_idx


def _score_with_cache(zone: CandidateZone) -> tuple[float, dict]:
    """Memoised wrapper around :func:`score_zone`.

    L-5 review fix (2026-05-16). The orchestrator scores each merged
    zone via ``_wrap_levels`` and then re-scores it again per role
    (nearest_support / nearest_resistance / liquidity_above /
    liquidity_below) — up to 5× duplicate work per analysis cycle.
    Caching on the mutable ``CandidateZone`` is safe because the
    accumulation pipeline finishes (touches, sources, swing_strengths
    all populated) before scoring starts.
    """
    if zone._cached_score is None:
        zone._cached_score = score_zone(zone)
    return zone._cached_score


def _wrap_levels(zones: list[CandidateZone]) -> list[StructureLevel]:
    out: list[StructureLevel] = []
    for z in zones:
        score, components = _score_with_cache(z)
        # Primary level_type: liquidity flag takes precedence for HIGH-side
        # equal-high clusters; LOW-side equal-low clusters surface as
        # LIQUIDITY_LOW. Otherwise SUPPORT/RESISTANCE by side. A level
        # carrying both roles can still be picked up by liquidity selectors
        # (locked decision #6) via the zone reference, not the wrapped type.
        if z.is_equal_hl_cluster:
            level_type = "LIQUIDITY_HIGH" if z.side == "HIGH" else "LIQUIDITY_LOW"
        else:
            level_type = "RESISTANCE" if z.side == "HIGH" else "SUPPORT"
        out.append(
            StructureLevel(
                pair=z.pair,
                level_type=level_type,
                price=z.price,
                zone_low=z.zone_low,
                zone_high=z.zone_high,
                timeframe=z.timeframe,
                score=score,
                touch_count=z.touch_count,
                last_touched_ts=z.last_touched_ts,
                source=",".join(z.sources),
                debug={"score_components": components, "sources": list(z.sources)},
            )
        )
    return out


def _nearest_zone(
    zones: list[CandidateZone],
    *,
    side: str,
    current_price: float,
    prefer_below: bool,
) -> Optional[CandidateZone]:
    """Pick the nearest same-side zone, biased by direction."""
    if math.isnan(current_price):
        return None
    same_side = [z for z in zones if z.side == side]
    if not same_side:
        return None
    if prefer_below:
        below = [z for z in same_side if z.price <= current_price]
        if below:
            return max(below, key=lambda z: z.price)
        # No below candidate — fall back to closest overall.
        return min(same_side, key=lambda z: abs(z.price - current_price))
    above = [z for z in same_side if z.price >= current_price]
    if above:
        return min(above, key=lambda z: z.price)
    return min(same_side, key=lambda z: abs(z.price - current_price))


def _zone_to_level(
    pair: str, zone: CandidateZone, *, level_type: str
) -> StructureLevel:
    score, components = _score_with_cache(zone)
    return StructureLevel(
        pair=pair,
        level_type=level_type,  # type: ignore[arg-type]
        price=zone.price,
        zone_low=zone.zone_low,
        zone_high=zone.zone_high,
        timeframe=zone.timeframe,
        score=score,
        touch_count=zone.touch_count,
        last_touched_ts=zone.last_touched_ts,
        source=",".join(zone.sources),
        debug={
            "score_components": components,
            "sources": list(zone.sources),
            "is_equal_hl_cluster": zone.is_equal_hl_cluster,
            "is_session_level": zone.is_session_level,
            "session_kind": zone.session_kind,
        },
    )


def _compute_confidence(
    support: Optional[StructureLevel], resistance: Optional[StructureLevel]
) -> float:
    """Refinement B: explicit None guards.

    A one-sided structure (only support, only resistance) is not
    penalised for the missing side — confidence equals the single
    side's normalised score. ``None`` for both yields 0.0.
    """
    if support is not None and resistance is not None:
        return min(support.score, resistance.score) / 10.0
    if support is not None:
        return support.score / 10.0
    if resistance is not None:
        return resistance.score / 10.0
    return 0.0


def _price_near_liquidity(
    *,
    current_price: float,
    atr_m5: float,
    liquidity_above_zone: Optional[CandidateZone],
    liquidity_below_zone: Optional[CandidateZone],
) -> bool:
    """Return True when price sits within ``NEAR_LIQUIDITY_ATR_MULT`` ATR of
    either liquidity pool.

    M-3 review fix (2026-05-16). Previously checked existence only — once
    the engine identified any liquidity zone (almost always), the flag was
    True regardless of distance. The narrower definition aligns with the
    spec §11 VOLATILE_SWEEP_ZONE wording ("price near obvious equal
    highs/lows … liquidity pools close").
    """
    if math.isnan(current_price) or atr_m5 <= 0 or math.isnan(atr_m5):
        return False
    threshold = NEAR_LIQUIDITY_ATR_MULT * atr_m5
    for zone in (liquidity_above_zone, liquidity_below_zone):
        if zone is None:
            continue
        if abs(zone.price - current_price) <= threshold:
            return True
    return False


def _summarise_reason(
    *,
    htf_bias: Direction,
    local_bias: Direction,
    structure_mode: str,
    reaction: str,
) -> str:
    return (
        f"htf={htf_bias}, local={local_bias}, mode={structure_mode}, "
        f"reaction={reaction}"
    )


def _summarise_regime(regime_state: dict) -> dict:
    if not isinstance(regime_state, dict):
        return {}
    return {
        "current_regime": regime_state.get("current_regime"),
        "current_direction": regime_state.get("current_direction"),
    }


__all__ = ["analyze_structure"]
