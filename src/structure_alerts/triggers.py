"""Phase 12 trigger layer — :class:`StructureChange` → :class:`AlertEvent`.

Pure functions. Same ``(changes, curr, now)`` produces the same
events; no dedupe (C-3), no persistence (C-3), no Phase 9 translation
(C-5). Splitting the rendering / dedupe-key / side-derivation logic
from the diff layer means a copy edit on a Telegram body string
doesn't require re-baselining the diff suite.

Dedupe-key formats (spec §9 anchored in
:mod:`structure_alerts` MODULE.md)
----------------------------------

============================  ====================================================
Event kind                    Dedupe key
============================  ====================================================
HTF_BIAS_CHANGE               ``{pair}_HTF_BIAS_{curr_bias}``
STRUCTURE_MODE_CHANGE         ``{pair}_MODE_{curr_mode}``
SUPPORT_ACCEPTANCE_BREAK      ``{pair}_SUPPORT_ACCEPTANCE_{Q(price)}``
RESISTANCE_ACCEPTANCE_BREAK   ``{pair}_RESISTANCE_ACCEPTANCE_{Q(price)}``
SWEEP_RECLAIM                 ``{pair}_SWEEP_RECLAIM_{side}_{Q(price)}``
FAILED_RECLAIM                ``{pair}_FAILED_RECLAIM_{side}_{Q(price)}``
NEW_MAJOR_LEVEL               ``{pair}_NEW_LEVEL_{side}_{Q(price)}``
LEVEL_INVALIDATED             ``{pair}_LEVEL_INVALIDATED_{side}_{Q(price)}``
============================  ====================================================

``Q(price)`` is :func:`structure_alerts.constants.quantise_price` —
integer pip count via :func:`config.pair_config.pip_size_for`.

Side derivation: SUPPORT for low-side reactions / levels, RESISTANCE
for high-side. The acceptance-break events bake "support" /
"resistance" into the *kind* (not just the dedupe key) because the
operator's mental model treats a support break and a resistance
break as different events; sweep / failed-reclaim collapse to a
single kind with a side-bearing dedupe key because the action is the
same shape.
"""
from __future__ import annotations

from datetime import datetime
from typing import Final, Optional

from structure_engine.types import StructureState

from .constants import quantise_price
from .diff import ChangeKind, StructureChange, level_side
from .types import AlertEvent, AlertEventKind, severity_for


# Map the engine's six alerting ReactionType values onto the Phase 12
# event kind catalogue. Acceptance breaks split per-side (CRITICAL on
# both); sweep + failed-reclaim each fold both sides into a single
# kind with the side carried in the dedupe key.
_REACTION_TO_EVENT_KIND: Final[dict[str, AlertEventKind]] = {
    "SUPPORT_ACCEPTANCE_BREAK": AlertEventKind.SUPPORT_ACCEPTANCE_BREAK,
    "RESISTANCE_ACCEPTANCE_BREAK": AlertEventKind.RESISTANCE_ACCEPTANCE_BREAK,
    "SUPPORT_SWEEP_RECLAIM": AlertEventKind.SWEEP_RECLAIM,
    "RESISTANCE_SWEEP_RECLAIM": AlertEventKind.SWEEP_RECLAIM,
    "FAILED_RECLAIM_BELOW_SUPPORT": AlertEventKind.FAILED_RECLAIM,
    "FAILED_RECLAIM_ABOVE_RESISTANCE": AlertEventKind.FAILED_RECLAIM,
}


_REACTION_SIDE: Final[dict[str, str]] = {
    "SUPPORT_ACCEPTANCE_BREAK": "SUPPORT",
    "RESISTANCE_ACCEPTANCE_BREAK": "RESISTANCE",
    "SUPPORT_SWEEP_RECLAIM": "SUPPORT",
    "RESISTANCE_SWEEP_RECLAIM": "RESISTANCE",
    "FAILED_RECLAIM_BELOW_SUPPORT": "SUPPORT",
    "FAILED_RECLAIM_ABOVE_RESISTANCE": "RESISTANCE",
}


def _format_price(pair: str, price: float) -> str:
    """Pair-aware price rendering for operator-readable bodies.

    JPY pairs (two-decimal quote) render with three decimals; other
    majors (four-decimal quote) render with five. Matches what an
    operator reads on their broker UI and keeps the Telegram body
    grep-friendly across pair classes.
    """
    if pair.upper().endswith("JPY"):
        return f"{price:.3f}"
    return f"{price:.5f}"


def changes_to_events(
    changes: list[StructureChange],
    curr: StructureState,
    *,
    now: datetime,
) -> list[AlertEvent]:
    """Map :class:`StructureChange` records to :class:`AlertEvent` payloads.

    Order-preserving: events appear in the same order as the input
    changes, which is the diff layer's emit order (bias → mode →
    reaction → new level → invalidated). The operator's Telegram
    timeline thus matches the engine's cause-then-effect order.

    Each :class:`AlertEvent` carries the locked severity from
    :func:`structure_alerts.types.severity_for` — no callsite can
    drift from the spec-§7 catalogue.
    """
    events: list[AlertEvent] = []
    pair = curr.pair
    for change in changes:
        event = _build_event(change, pair, now)
        if event is not None:
            events.append(event)
    return events


def _build_event(
    change: StructureChange, pair: str, now: datetime,
) -> Optional[AlertEvent]:
    if change.kind is ChangeKind.HTF_BIAS_CHANGED:
        return _bias_change_event(change, pair, now)
    if change.kind is ChangeKind.MODE_CHANGED:
        return _mode_change_event(change, pair, now)
    if change.kind is ChangeKind.REACTION_OBSERVED:
        return _reaction_event(change, pair, now)
    if change.kind is ChangeKind.NEW_MAJOR_LEVEL:
        return _new_level_event(change, pair, now)
    if change.kind is ChangeKind.LEVEL_INVALIDATED:
        return _level_invalidated_event(change, pair, now)
    return None


def _bias_change_event(
    change: StructureChange, pair: str, now: datetime,
) -> AlertEvent:
    kind = AlertEventKind.HTF_BIAS_CHANGE
    prev_v = change.prev_value
    curr_v = change.curr_value
    return AlertEvent(
        kind=kind,
        pair=pair,
        severity=severity_for(kind),
        timestamp=now,
        dedupe_key=f"{pair}_HTF_BIAS_{curr_v}",
        full_text=f"HTF bias {prev_v} -> {curr_v}",
        short_text=f"HTF {prev_v} -> {curr_v}",
        debug={"prev_htf_bias": prev_v, "curr_htf_bias": curr_v},
    )


def _mode_change_event(
    change: StructureChange, pair: str, now: datetime,
) -> AlertEvent:
    kind = AlertEventKind.STRUCTURE_MODE_CHANGE
    prev_v = change.prev_value
    curr_v = change.curr_value
    return AlertEvent(
        kind=kind,
        pair=pair,
        severity=severity_for(kind),
        timestamp=now,
        dedupe_key=f"{pair}_MODE_{curr_v}",
        full_text=f"Structure mode {prev_v} -> {curr_v}",
        short_text=f"mode {prev_v} -> {curr_v}",
        debug={"prev_mode": prev_v, "curr_mode": curr_v},
    )


def _reaction_event(
    change: StructureChange, pair: str, now: datetime,
) -> Optional[AlertEvent]:
    reaction = change.reaction
    level = change.level
    if reaction is None or level is None:
        return None  # diff layer guarantees both, but fail-closed here too
    kind = _REACTION_TO_EVENT_KIND[reaction]
    side = _REACTION_SIDE[reaction]
    qp = quantise_price(pair, level.price)
    formatted = _format_price(pair, level.price)

    if kind is AlertEventKind.SUPPORT_ACCEPTANCE_BREAK:
        dedupe_key = f"{pair}_SUPPORT_ACCEPTANCE_{qp}"
        full_text = (
            f"Support broken at {formatted} "
            f"({level.timeframe}, score was {level.score:.1f})"
        )
        short_text = f"support broken @ {formatted}"
    elif kind is AlertEventKind.RESISTANCE_ACCEPTANCE_BREAK:
        dedupe_key = f"{pair}_RESISTANCE_ACCEPTANCE_{qp}"
        full_text = (
            f"Resistance broken at {formatted} "
            f"({level.timeframe}, score was {level.score:.1f})"
        )
        short_text = f"resistance broken @ {formatted}"
    elif kind is AlertEventKind.SWEEP_RECLAIM:
        dedupe_key = f"{pair}_SWEEP_RECLAIM_{side}_{qp}"
        full_text = (
            f"Sweep + reclaim at {side} {formatted} ({level.timeframe})"
        )
        short_text = f"sweep reclaim {side} @ {formatted}"
    else:  # AlertEventKind.FAILED_RECLAIM
        dedupe_key = f"{pair}_FAILED_RECLAIM_{side}_{qp}"
        full_text = (
            f"Failed reclaim at {side} {formatted} ({level.timeframe})"
        )
        short_text = f"failed reclaim {side} @ {formatted}"

    return AlertEvent(
        kind=kind,
        pair=pair,
        severity=severity_for(kind),
        timestamp=now,
        dedupe_key=dedupe_key,
        full_text=full_text,
        short_text=short_text,
        debug={
            "reaction": reaction,
            "side": side,
            "price": level.price,
            "score": level.score,
            "timeframe": level.timeframe,
            "level_type": level.level_type,
        },
    )


def _new_level_event(
    change: StructureChange, pair: str, now: datetime,
) -> Optional[AlertEvent]:
    level = change.level
    if level is None:
        return None
    kind = AlertEventKind.NEW_MAJOR_LEVEL
    side = level_side(level)
    qp = quantise_price(pair, level.price)
    formatted = _format_price(pair, level.price)
    return AlertEvent(
        kind=kind,
        pair=pair,
        severity=severity_for(kind),
        timestamp=now,
        dedupe_key=f"{pair}_NEW_LEVEL_{side}_{qp}",
        full_text=(
            f"New major {side} level at {formatted} "
            f"({level.timeframe}, score {level.score:.1f})"
        ),
        short_text=f"new {side} @ {formatted}",
        debug={
            "side": side,
            "price": level.price,
            "score": level.score,
            "timeframe": level.timeframe,
            "level_type": level.level_type,
        },
    )


def _level_invalidated_event(
    change: StructureChange, pair: str, now: datetime,
) -> Optional[AlertEvent]:
    level = change.level
    if level is None:
        return None
    kind = AlertEventKind.LEVEL_INVALIDATED
    side = level_side(level)
    qp = quantise_price(pair, level.price)
    formatted = _format_price(pair, level.price)
    return AlertEvent(
        kind=kind,
        pair=pair,
        severity=severity_for(kind),
        timestamp=now,
        dedupe_key=f"{pair}_LEVEL_INVALIDATED_{side}_{qp}",
        full_text=(
            f"{side.capitalize()} level at {formatted} invalidated "
            f"({level.timeframe})"
        ),
        short_text=f"{side} invalidated @ {formatted}",
        debug={
            "side": side,
            "price": level.price,
            "timeframe": level.timeframe,
            "level_type": level.level_type,
        },
    )


__all__ = ["changes_to_events"]
