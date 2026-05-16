"""Tests for structure_alerts.diff.compute_structure_diff.

One-fact-per-test: each case constructs a (prev, curr) pair that
flips exactly one engine field and asserts the resulting change list
shape. Where multiple changes coexist on one bar, the corresponding
test (`test_multiple_changes_in_one_bar`) covers the interaction.
"""
from __future__ import annotations

import pytest

from structure_alerts.diff import (
    ChangeKind,
    StructureChange,
    compute_structure_diff,
    level_side,
)

from .conftest import make_level, make_state


# ---------------------------------------------------------------------------
# Cold-start contract
# ---------------------------------------------------------------------------


def test_returns_empty_when_prev_is_none() -> None:
    curr = make_state(htf_bias="BULLISH", structure_mode="TREND_CONTINUATION")
    assert compute_structure_diff(prev=None, curr=curr) == []


def test_returns_empty_when_prev_invalid() -> None:
    prev = make_state(is_valid=False)
    curr = make_state(htf_bias="BULLISH")
    assert compute_structure_diff(prev=prev, curr=curr) == []


def test_returns_empty_when_curr_invalid() -> None:
    prev = make_state(htf_bias="NEUTRAL")
    curr = make_state(htf_bias="BULLISH", is_valid=False)
    assert compute_structure_diff(prev=prev, curr=curr) == []


# ---------------------------------------------------------------------------
# §7 A — HTF bias
# ---------------------------------------------------------------------------


def test_htf_bias_change_emits_single_change() -> None:
    prev = make_state(htf_bias="BULLISH")
    curr = make_state(htf_bias="BEARISH")
    changes = compute_structure_diff(prev=prev, curr=curr)
    assert len(changes) == 1
    assert changes[0] == StructureChange(
        kind=ChangeKind.HTF_BIAS_CHANGED,
        prev_value="BULLISH",
        curr_value="BEARISH",
    )


def test_htf_bias_unchanged_emits_nothing() -> None:
    prev = make_state(htf_bias="BULLISH")
    curr = make_state(htf_bias="BULLISH")
    assert compute_structure_diff(prev=prev, curr=curr) == []


def test_htf_bias_change_neutral_to_directional_emits() -> None:
    prev = make_state(htf_bias="NEUTRAL")
    curr = make_state(htf_bias="BULLISH")
    changes = compute_structure_diff(prev=prev, curr=curr)
    assert len(changes) == 1
    assert changes[0].kind is ChangeKind.HTF_BIAS_CHANGED


# ---------------------------------------------------------------------------
# §7 B — Structure mode
# ---------------------------------------------------------------------------


def test_mode_change_emits_change() -> None:
    prev = make_state(structure_mode="RANGE_BALANCE")
    curr = make_state(structure_mode="TREND_CONTINUATION")
    changes = compute_structure_diff(prev=prev, curr=curr)
    assert len(changes) == 1
    assert changes[0] == StructureChange(
        kind=ChangeKind.MODE_CHANGED,
        prev_value="RANGE_BALANCE",
        curr_value="TREND_CONTINUATION",
    )


def test_mode_change_to_unknown_suppressed() -> None:
    # Engine warm-up degradation can flip a mode back to UNKNOWN
    # mid-session (e.g., EMA200 dropping out after a feed gap). The
    # operator should not be paged for warm-up state.
    prev = make_state(structure_mode="TREND_CONTINUATION")
    curr = make_state(structure_mode="UNKNOWN")
    assert compute_structure_diff(prev=prev, curr=curr) == []


def test_mode_change_from_unknown_to_known_emits() -> None:
    # Inverse case: warm-up finishes, engine settles on a real mode.
    # Operator does want this notification.
    prev = make_state(structure_mode="UNKNOWN")
    curr = make_state(structure_mode="RANGE_BALANCE")
    changes = compute_structure_diff(prev=prev, curr=curr)
    assert len(changes) == 1
    assert changes[0].kind is ChangeKind.MODE_CHANGED


# ---------------------------------------------------------------------------
# §7 C–F — Reactions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reaction,nearest_field",
    [
        ("SUPPORT_ACCEPTANCE_BREAK", "nearest_support"),
        ("SUPPORT_SWEEP_RECLAIM", "nearest_support"),
        ("FAILED_RECLAIM_BELOW_SUPPORT", "nearest_support"),
        ("RESISTANCE_ACCEPTANCE_BREAK", "nearest_resistance"),
        ("RESISTANCE_SWEEP_RECLAIM", "nearest_resistance"),
        ("FAILED_RECLAIM_ABOVE_RESISTANCE", "nearest_resistance"),
    ],
)
def test_alerting_reaction_emits_change_with_target_level(
    reaction, nearest_field,
) -> None:
    level_type = "SUPPORT" if "SUPPORT" in nearest_field or "support" in nearest_field else "RESISTANCE"
    level = make_level(level_type=level_type, price=1.30200)
    prev = make_state()
    curr_kwargs = {nearest_field: level, "current_reaction": reaction}
    curr = make_state(**curr_kwargs)
    changes = compute_structure_diff(prev=prev, curr=curr)
    reaction_changes = [c for c in changes if c.kind is ChangeKind.REACTION_OBSERVED]
    assert len(reaction_changes) == 1
    assert reaction_changes[0].reaction == reaction
    assert reaction_changes[0].level is level


@pytest.mark.parametrize(
    "reaction",
    ["NONE", "SUPPORT_REJECTION", "RESISTANCE_REJECTION", "RANGE_ROTATION"],
)
def test_non_alerting_reaction_emits_nothing(reaction) -> None:
    prev = make_state()
    curr = make_state(current_reaction=reaction)
    assert compute_structure_diff(prev=prev, curr=curr) == []


def test_reaction_without_target_level_is_skipped() -> None:
    # Curr reports an alerting reaction but the side's nearest level
    # is None (engine couldn't resolve it). Defensive: diff layer
    # should not emit a REACTION_OBSERVED with a null level.
    prev = make_state()
    curr = make_state(
        current_reaction="SUPPORT_ACCEPTANCE_BREAK",
        nearest_support=None,
    )
    assert compute_structure_diff(prev=prev, curr=curr) == []


# ---------------------------------------------------------------------------
# §7 G — New major level
# ---------------------------------------------------------------------------


def test_new_major_level_fires_when_score_above_threshold() -> None:
    # STRONG_LEVEL_THRESHOLD defaults to 6.0; score 7.5 qualifies.
    new_support = make_level(level_type="SUPPORT", price=1.30200, score=7.5)
    prev = make_state(levels=[])  # nothing in prev
    curr = make_state(
        nearest_support=new_support,
        levels=[new_support],
    )
    changes = compute_structure_diff(prev=prev, curr=curr)
    nml = [c for c in changes if c.kind is ChangeKind.NEW_MAJOR_LEVEL]
    assert len(nml) == 1
    assert nml[0].level is new_support


def test_new_level_below_threshold_does_not_fire() -> None:
    weak_support = make_level(level_type="SUPPORT", price=1.30200, score=4.0)
    prev = make_state(levels=[])
    curr = make_state(nearest_support=weak_support, levels=[weak_support])
    changes = compute_structure_diff(prev=prev, curr=curr)
    assert [c for c in changes if c.kind is ChangeKind.NEW_MAJOR_LEVEL] == []


def test_new_level_already_present_in_prev_does_not_fire() -> None:
    # Same quantised price already in prev.levels — not a "new" level
    # from the operator's perspective.
    existing = make_level(level_type="SUPPORT", price=1.30200, score=7.5)
    # Prev has a level at the same quantised price (1.30203 -> same int as 1.30200 since they differ by 3 sub-pips, but actually 3 sub-pips = 0.00003 < 1 pip = 0.0001 so same Q).
    prev_existing = make_level(level_type="SUPPORT", price=1.30203, score=7.0)
    prev = make_state(levels=[prev_existing])
    curr = make_state(nearest_support=existing, levels=[existing])
    changes = compute_structure_diff(prev=prev, curr=curr)
    assert [c for c in changes if c.kind is ChangeKind.NEW_MAJOR_LEVEL] == []


def test_new_level_with_different_side_at_same_quantised_price_does_fire() -> None:
    # A SUPPORT at 1.30200 in prev should NOT suppress a NEW
    # RESISTANCE at 1.30200 in curr — different sides, different
    # operator-relevant events.
    prev_support = make_level(level_type="SUPPORT", price=1.30200, score=7.0)
    new_resistance = make_level(level_type="RESISTANCE", price=1.30200, score=7.5)
    prev = make_state(levels=[prev_support])
    curr = make_state(nearest_resistance=new_resistance, levels=[new_resistance])
    changes = compute_structure_diff(prev=prev, curr=curr)
    nml = [c for c in changes if c.kind is ChangeKind.NEW_MAJOR_LEVEL]
    assert len(nml) == 1
    assert nml[0].level.level_type == "RESISTANCE"


def test_new_level_scoped_to_nearest_only_not_all_levels() -> None:
    # A new strong level that's in curr.levels but NOT the nearest
    # support / resistance is observability-only — the diff layer
    # does not emit NEW_MAJOR_LEVEL for it.
    far_strong = make_level(level_type="SUPPORT", price=1.20000, score=9.0)
    nearest_support = make_level(level_type="SUPPORT", price=1.30000, score=7.0)
    prev = make_state(levels=[nearest_support])
    # Curr keeps the nearest support, plus introduces a far strong
    # support that's NOT nearest. Operator gets nothing for the far
    # one (it's catalogue clutter at the alert layer).
    curr = make_state(
        nearest_support=nearest_support, levels=[nearest_support, far_strong],
    )
    changes = compute_structure_diff(prev=prev, curr=curr)
    assert [c for c in changes if c.kind is ChangeKind.NEW_MAJOR_LEVEL] == []


@pytest.mark.parametrize(
    "nearest_field,level_type",
    [
        ("nearest_support", "SUPPORT"),
        ("nearest_resistance", "RESISTANCE"),
    ],
)
def test_pre_refinement_a_record_does_not_spuriously_fire_new_major_level(
    nearest_field, level_type,
) -> None:
    # Pre-refinement-A jsonl records have empty levels list but the
    # nearest_* fields are preserved through hydration. The diff layer
    # must treat the rehydrated nearest_* as "present in prev" — without
    # this, the first post-upgrade restart bar bursts spurious INFO
    # NEW_MAJOR_LEVEL alerts for levels that were always there.
    #
    # H1 regression test: prev has empty levels but populated nearest_*,
    # curr's nearest matches at the same quantised price. Expect no
    # NEW_MAJOR_LEVEL (the level is not new from the operator's view).
    prev_nearest = make_level(level_type=level_type, price=1.30050, score=0.0)
    prev_kwargs = {nearest_field: prev_nearest, "levels": []}
    prev = make_state(**prev_kwargs)
    curr_nearest = make_level(level_type=level_type, price=1.30050, score=8.0)
    curr_kwargs = {nearest_field: curr_nearest, "levels": [curr_nearest]}
    curr = make_state(**curr_kwargs)
    changes = compute_structure_diff(prev=prev, curr=curr)
    nml = [c for c in changes if c.kind is ChangeKind.NEW_MAJOR_LEVEL]
    assert nml == [], f"Spurious NEW_MAJOR_LEVEL fired: {nml}"


# ---------------------------------------------------------------------------
# §7 H — Level invalidated
# ---------------------------------------------------------------------------


def test_level_invalidated_when_prev_nearest_disappeared() -> None:
    prev_support = make_level(level_type="SUPPORT", price=1.30000, score=7.0)
    prev = make_state(nearest_support=prev_support, levels=[prev_support])
    # Curr has no support at the old price, and no support at all.
    curr = make_state(nearest_support=None, levels=[])
    changes = compute_structure_diff(prev=prev, curr=curr)
    inv = [c for c in changes if c.kind is ChangeKind.LEVEL_INVALIDATED]
    assert len(inv) == 1
    assert inv[0].level is prev_support


def test_level_invalidated_no_event_when_level_still_in_curr_levels() -> None:
    # Prev's nearest support is now demoted (not the nearest) but
    # still exists in curr.levels — not "invalidated", just demoted.
    old_support = make_level(level_type="SUPPORT", price=1.30000, score=7.0)
    new_support = make_level(level_type="SUPPORT", price=1.30200, score=8.0)
    prev = make_state(nearest_support=old_support, levels=[old_support])
    curr = make_state(
        nearest_support=new_support, levels=[old_support, new_support],
    )
    changes = compute_structure_diff(prev=prev, curr=curr)
    assert [c for c in changes if c.kind is ChangeKind.LEVEL_INVALIDATED] == []


def test_level_invalidated_when_quantised_match_disappears() -> None:
    # Prev support at 1.30000 (Q=13000). Curr has a level at 1.30100
    # (Q=13010) — different quantised pip count, so the old level
    # counts as invalidated.
    prev_support = make_level(level_type="SUPPORT", price=1.30000, score=7.0)
    new_support = make_level(level_type="SUPPORT", price=1.30100, score=7.0)
    prev = make_state(nearest_support=prev_support, levels=[prev_support])
    curr = make_state(nearest_support=new_support, levels=[new_support])
    changes = compute_structure_diff(prev=prev, curr=curr)
    inv = [c for c in changes if c.kind is ChangeKind.LEVEL_INVALIDATED]
    assert len(inv) == 1


def test_level_invalidated_handled_independently_per_side() -> None:
    # Prev has both nearest support and resistance; both vanish.
    # Should produce two invalidation events.
    prev_support = make_level(level_type="SUPPORT", price=1.30000, score=7.0)
    prev_resistance = make_level(level_type="RESISTANCE", price=1.31000, score=7.0)
    prev = make_state(
        nearest_support=prev_support,
        nearest_resistance=prev_resistance,
        levels=[prev_support, prev_resistance],
    )
    curr = make_state(levels=[])
    changes = compute_structure_diff(prev=prev, curr=curr)
    inv = [c for c in changes if c.kind is ChangeKind.LEVEL_INVALIDATED]
    assert len(inv) == 2
    invalidated_prices = {c.level.price for c in inv}
    assert invalidated_prices == {1.30000, 1.31000}


# ---------------------------------------------------------------------------
# Multi-change interaction
# ---------------------------------------------------------------------------


def test_multiple_changes_in_one_bar_preserve_emit_order() -> None:
    # Construct a (prev, curr) where bias, mode, reaction, new-level,
    # and invalidated all fire on the same bar. Operator's Telegram
    # timeline should read in cause-then-effect order.
    prev_support = make_level(level_type="SUPPORT", price=1.30000, score=7.0)
    new_resistance = make_level(level_type="RESISTANCE", price=1.31500, score=8.0)
    prev = make_state(
        htf_bias="BULLISH",
        structure_mode="TREND_CONTINUATION",
        nearest_support=prev_support,
        levels=[prev_support],
    )
    curr = make_state(
        htf_bias="BEARISH",
        structure_mode="VOLATILE_SWEEP_ZONE",
        nearest_support=None,
        nearest_resistance=new_resistance,
        current_reaction="RESISTANCE_ACCEPTANCE_BREAK",
        levels=[new_resistance],
    )
    changes = compute_structure_diff(prev=prev, curr=curr)
    kinds_in_order = [c.kind for c in changes]
    assert kinds_in_order == [
        ChangeKind.HTF_BIAS_CHANGED,
        ChangeKind.MODE_CHANGED,
        ChangeKind.REACTION_OBSERVED,
        ChangeKind.NEW_MAJOR_LEVEL,
        ChangeKind.LEVEL_INVALIDATED,
    ]


# ---------------------------------------------------------------------------
# level_side helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "level_type,expected_side",
    [
        ("SUPPORT", "SUPPORT"),
        ("LIQUIDITY_LOW", "SUPPORT"),
        ("RESISTANCE", "RESISTANCE"),
        ("LIQUIDITY_HIGH", "RESISTANCE"),
    ],
)
def test_level_side_collapses_four_way_to_binary(level_type, expected_side) -> None:
    level = make_level(level_type=level_type, price=1.30000)
    assert level_side(level) == expected_side
