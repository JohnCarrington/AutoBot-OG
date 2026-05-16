"""Phase 12 structure-state diff.

Compares the previous and current :class:`StructureState` for a pair
and emits zero or more :class:`StructureChange` records describing
what transitioned. The next stage (:mod:`structure_alerts.triggers`)
maps each :class:`StructureChange` to an :class:`AlertEvent`.

Splitting diff from triggers is deliberate: diff is "what changed"
(pure data; no rendering, no severity, no dedupe), triggers is "how
do we surface it" (text rendering, dedupe key, side derivation).
Tests cover each side independently, so a rendering tweak doesn't
require re-baselining the diff suite.

Spec §3 (general diff principle) + §7 A–H (per-event triggers):

============================  ====================  ====================
Spec section                  Diff produces         Event kind (C-5)
============================  ====================  ====================
§7 A — HTF bias               HTF_BIAS_CHANGED      HTF_BIAS_CHANGE
§7 B — Structure mode         MODE_CHANGED          STRUCTURE_MODE_CHANGE
§7 C — Support acceptance     REACTION_OBSERVED     SUPPORT_ACCEPTANCE_BREAK
§7 D — Resistance acceptance  REACTION_OBSERVED     RESISTANCE_ACCEPTANCE_BREAK
§7 E — Sweep + reclaim        REACTION_OBSERVED     SWEEP_RECLAIM
§7 F — Failed reclaim         REACTION_OBSERVED     FAILED_RECLAIM
§7 G — New major level        NEW_MAJOR_LEVEL       NEW_MAJOR_LEVEL
§7 H — Level invalidated      LEVEL_INVALIDATED     LEVEL_INVALIDATED
============================  ====================  ====================

The four reaction subtypes (§7 C/D/E/F) collapse to a single
``REACTION_OBSERVED`` change kind in the diff layer — the trigger
layer (C-2 :mod:`structure_alerts.triggers`) reads the engine's
finer-grained ``ReactionType`` value off the change record and picks
the matching :class:`AlertEventKind`. This keeps the diff layer free
of catalogue-shape concerns.

Cold-start contract
-------------------

When ``prev is None`` (BotLoop has not observed a structure cycle
for this pair yet) or either state is ``is_valid=False``, the
function returns an empty list. Rationale: on first observation
post-startup, there is no prior state to compare against, and an
"INITIAL_BIAS_OBSERVED" alert storm for every pair on every cold
start would erode signal-to-noise. The follow-up bar's diff has a
non-None ``prev`` and behaves normally — any genuine transition
fires on bar N+1, not on bar N+0.

This is paired with the C-4 hydration logic that rehydrates
``_previous_structure`` from the structure-engine jsonl so a restart
mid-session does NOT lose the prev for pairs that have history on
disk.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final, Optional

from structure_engine.constants import STRONG_LEVEL_THRESHOLD
from structure_engine.types import StructureLevel, StructureState

from .constants import quantise_price


# Reaction subtypes the diff layer alerts on. Plain rejections
# (SUPPORT_REJECTION, RESISTANCE_REJECTION) and RANGE_ROTATION are
# observability-only — they're recorded in the structure jsonl but
# do not page the operator. NONE is the no-reaction case.
_ALERTING_REACTIONS: Final[frozenset[str]] = frozenset({
    "SUPPORT_ACCEPTANCE_BREAK",
    "RESISTANCE_ACCEPTANCE_BREAK",
    "SUPPORT_SWEEP_RECLAIM",
    "RESISTANCE_SWEEP_RECLAIM",
    "FAILED_RECLAIM_BELOW_SUPPORT",
    "FAILED_RECLAIM_ABOVE_RESISTANCE",
})


class ChangeKind(Enum):
    """Internal closed-set discriminator for :class:`StructureChange`.

    Distinct from :class:`structure_alerts.types.AlertEventKind` —
    the diff layer collapses the four reaction subtypes into a single
    ``REACTION_OBSERVED`` kind; the trigger layer expands it back out
    by reading the engine's ``ReactionType``. Mapping is:

    - ``HTF_BIAS_CHANGED`` → ``HTF_BIAS_CHANGE``
    - ``MODE_CHANGED`` → ``STRUCTURE_MODE_CHANGE``
    - ``REACTION_OBSERVED`` → one of
      ``SUPPORT_ACCEPTANCE_BREAK`` / ``RESISTANCE_ACCEPTANCE_BREAK`` /
      ``SWEEP_RECLAIM`` / ``FAILED_RECLAIM`` depending on the
      underlying ``StructureChange.reaction``
    - ``NEW_MAJOR_LEVEL`` → ``NEW_MAJOR_LEVEL``
    - ``LEVEL_INVALIDATED`` → ``LEVEL_INVALIDATED``
    """

    HTF_BIAS_CHANGED = "HTF_BIAS_CHANGED"
    MODE_CHANGED = "MODE_CHANGED"
    REACTION_OBSERVED = "REACTION_OBSERVED"
    NEW_MAJOR_LEVEL = "NEW_MAJOR_LEVEL"
    LEVEL_INVALIDATED = "LEVEL_INVALIDATED"


@dataclass(frozen=True)
class StructureChange:
    """Internal-only record of one structural transition.

    Tagged union — exactly one payload-field set is meaningful per
    :attr:`kind`. The trigger layer reads only the fields relevant to
    the kind; consumers outside :mod:`structure_alerts` should depend
    on :class:`structure_alerts.types.AlertEvent` instead.

    Field usage by kind
    -------------------

    - ``HTF_BIAS_CHANGED`` / ``MODE_CHANGED``: ``prev_value``,
      ``curr_value`` (engine ``Direction`` / ``StructureMode`` string).
    - ``REACTION_OBSERVED``: ``reaction`` (engine ``ReactionType``
      string) + ``level`` (the side-relevant nearest level —
      :attr:`StructureState.nearest_support` for support-side
      reactions, ``nearest_resistance`` for resistance-side).
    - ``NEW_MAJOR_LEVEL`` / ``LEVEL_INVALIDATED``: ``level`` (the
      level being introduced / invalidated; engine ``StructureLevel``).
    """

    kind: ChangeKind
    prev_value: Optional[str] = None
    curr_value: Optional[str] = None
    reaction: Optional[str] = None
    level: Optional[StructureLevel] = None


def level_side(level: StructureLevel) -> str:
    """Return ``"SUPPORT"`` / ``"RESISTANCE"`` for a :class:`StructureLevel`.

    Collapses the engine's four-way :attr:`StructureLevel.level_type`
    (SUPPORT / RESISTANCE / LIQUIDITY_LOW / LIQUIDITY_HIGH) into the
    binary side relevant to dedupe keys and prev/curr lookups.
    LIQUIDITY_LOW maps to SUPPORT (low-side zone), LIQUIDITY_HIGH to
    RESISTANCE (high-side zone) — same convention the engine uses
    when picking nearest_support / nearest_resistance via the same
    underlying ``side`` field.

    Phase 12 dedupe keys embed this side so a SUPPORT at 1.30000 and
    a RESISTANCE at 1.30000 (rare but possible across separate zones)
    quantise to the same integer pip count but produce distinct
    dedupe keys.
    """
    if level.level_type in ("SUPPORT", "LIQUIDITY_LOW"):
        return "SUPPORT"
    return "RESISTANCE"


def _level_key_set(
    pair: str, levels: list[StructureLevel]
) -> set[tuple[str, int]]:
    """Return ``{(side, quantised_price)}`` for the level list.

    Used by the NEW_MAJOR_LEVEL ("present in curr, absent in prev")
    and LEVEL_INVALIDATED ("present in prev, absent in curr") checks.
    Indexing by ``(side, quantised_price)`` (rather than the integer
    alone) prevents a same-quantised support and resistance from
    spuriously masking each other's presence / absence.
    """
    return {(level_side(level), quantise_price(pair, level.price)) for level in levels}


def _reaction_target_level(state: StructureState) -> Optional[StructureLevel]:
    """Pick which nearest level the engine's current_reaction refers to.

    Support-side reactions (acceptance break, sweep reclaim, failed
    reclaim below) point at :attr:`StructureState.nearest_support`;
    resistance-side reactions point at ``nearest_resistance``.

    Returns ``None`` for non-alerting reactions (NONE,
    SUPPORT_REJECTION, RESISTANCE_REJECTION, RANGE_ROTATION) and
    when the relevant nearest level is unset on the state.
    """
    reaction = state.current_reaction
    if reaction in (
        "SUPPORT_ACCEPTANCE_BREAK",
        "SUPPORT_SWEEP_RECLAIM",
        "FAILED_RECLAIM_BELOW_SUPPORT",
    ):
        return state.nearest_support
    if reaction in (
        "RESISTANCE_ACCEPTANCE_BREAK",
        "RESISTANCE_SWEEP_RECLAIM",
        "FAILED_RECLAIM_ABOVE_RESISTANCE",
    ):
        return state.nearest_resistance
    return None


def compute_structure_diff(
    prev: Optional[StructureState],
    curr: StructureState,
) -> list[StructureChange]:
    """Compare ``prev`` and ``curr``; return the list of transitions.

    Order of detection (also the returned list order):

    1. HTF bias change (§7 A)
    2. Structure mode change, excluding curr=UNKNOWN warm-up (§7 B)
    3. Reaction observed, if alerting (§7 C–F)
    4. New major level (§7 G), one per side that qualifies
    5. Level invalidated (§7 H), one per prev nearest that vanished

    The trigger layer preserves this order, so the operator sees a
    multi-event bar in a sensible chronological-cause order (bias →
    mode → reaction → level catalogue).

    Returns an empty list when ``prev is None`` or either state is
    ``is_valid=False`` — see the cold-start contract in the module
    docstring.
    """
    if prev is None or not prev.is_valid:
        return []
    if not curr.is_valid:
        return []

    changes: list[StructureChange] = []

    # §7 A: HTF bias change.
    if prev.htf_bias != curr.htf_bias:
        changes.append(
            StructureChange(
                kind=ChangeKind.HTF_BIAS_CHANGED,
                prev_value=prev.htf_bias,
                curr_value=curr.htf_bias,
            )
        )

    # §7 B: Structure mode change. Suppress transitions INTO UNKNOWN
    # — that's the engine's warm-up degradation signal and should not
    # spam the operator. Transitions OUT OF UNKNOWN into a real mode
    # do fire (operator wants to know "warm-up finished, mode now X").
    if prev.structure_mode != curr.structure_mode and curr.structure_mode != "UNKNOWN":
        changes.append(
            StructureChange(
                kind=ChangeKind.MODE_CHANGED,
                prev_value=prev.structure_mode,
                curr_value=curr.structure_mode,
            )
        )

    # §7 C–F: Reactions. Fire on any alerting reaction in curr; the
    # dedupe layer (C-3) handles "same break, multiple bars". This is
    # simpler than tracking ``prev.current_reaction`` and produces
    # the same operator-visible behaviour once dedupe is in place.
    if curr.current_reaction in _ALERTING_REACTIONS:
        target = _reaction_target_level(curr)
        if target is not None:
            changes.append(
                StructureChange(
                    kind=ChangeKind.REACTION_OBSERVED,
                    reaction=curr.current_reaction,
                    level=target,
                )
            )

    # §7 G: New major level. Scoped to curr.nearest_support /
    # nearest_resistance — those are the actionable levels. A new
    # level that's not one of the two nearest is observability-only.
    prev_keys = _level_key_set(curr.pair, prev.levels)
    for nearest in (curr.nearest_support, curr.nearest_resistance):
        if nearest is None:
            continue
        if nearest.score < STRONG_LEVEL_THRESHOLD:
            continue
        if (level_side(nearest), quantise_price(curr.pair, nearest.price)) in prev_keys:
            continue
        changes.append(
            StructureChange(kind=ChangeKind.NEW_MAJOR_LEVEL, level=nearest)
        )

    # §7 H: Level invalidated. A prev nearest whose (side, quantised
    # price) no longer appears in curr.levels. We match against
    # curr.levels (not curr.nearest_*) because the broken level may
    # still exist in the wider catalogue as a non-nearest zone — in
    # that case it isn't "invalidated", just demoted.
    curr_keys = _level_key_set(curr.pair, curr.levels)
    for nearest in (prev.nearest_support, prev.nearest_resistance):
        if nearest is None:
            continue
        if (level_side(nearest), quantise_price(curr.pair, nearest.price)) in curr_keys:
            continue
        changes.append(
            StructureChange(kind=ChangeKind.LEVEL_INVALIDATED, level=nearest)
        )

    return changes


__all__ = [
    "ChangeKind",
    "StructureChange",
    "compute_structure_diff",
    "level_side",
]
