"""Tests for structure_alerts.hydration.load_latest_structure_state_per_pair.

Covers cold-start (missing / empty file), per-pair last-write-wins,
corrupt-line skipping, malformed-level filtering, and the end-to-end
round-trip via the Phase 11 logger (writes via
:func:`structure_engine.logging.log_structure_state`, reads via
:func:`load_latest_structure_state_per_pair`).
"""
from __future__ import annotations

import json
from pathlib import Path

import structure_engine.logging as struct_log
from structure_alerts.hydration import load_latest_structure_state_per_pair
from structure_engine.types import StructureLevel, StructureState


# ---------------------------------------------------------------------------
# Cold-start cases
# ---------------------------------------------------------------------------


def test_missing_file_returns_empty_dict(tmp_path) -> None:
    """First run, jsonl doesn't exist yet — no crash, empty result."""
    result = load_latest_structure_state_per_pair(tmp_path / "nonexistent.jsonl")
    assert result == {}


def test_empty_file_returns_empty_dict(tmp_path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    assert load_latest_structure_state_per_pair(path) == {}


def test_whitespace_only_lines_skipped(tmp_path) -> None:
    """Blank lines between real records (e.g., editor artefact)
    must not crash hydration or produce phantom entries."""
    path = tmp_path / "structure.jsonl"
    rec = _good_rec("GBPUSD", htf_bias="BULLISH")
    path.write_text("\n   \n" + json.dumps(rec) + "\n\n")
    result = load_latest_structure_state_per_pair(path)
    assert set(result.keys()) == {"GBPUSD"}


# ---------------------------------------------------------------------------
# Basic hydration
# ---------------------------------------------------------------------------


def test_single_record_rehydrates_basics(tmp_path) -> None:
    path = tmp_path / "structure.jsonl"
    rec = {
        "timestamp": "2026-05-16T09:00:00+00:00",
        "pair": "GBPUSD",
        "is_valid": True,
        "htf_bias": "BULLISH",
        "local_bias": "NEUTRAL",
        "nearest_support": 1.30000,
        "nearest_support_score": 7.5,
        "nearest_resistance": 1.31000,
        "nearest_resistance_score": 8.0,
        "liquidity_above": None,
        "liquidity_below": None,
        "current_reaction": "NONE",
        "acceptance_state": "NONE",
        "structure_mode": "RANGE_BALANCE",
        "confidence": 0.7,
        "reason": "test",
        "levels": [
            {"p": 1.30000, "s": "SUPPORT", "sc": 7.5, "tf": "H1"},
            {"p": 1.31000, "s": "RESISTANCE", "sc": 8.0, "tf": "M15"},
        ],
    }
    path.write_text(json.dumps(rec) + "\n")
    result = load_latest_structure_state_per_pair(path)

    assert "GBPUSD" in result
    state = result["GBPUSD"]
    assert state.pair == "GBPUSD"
    assert state.htf_bias == "BULLISH"
    assert state.structure_mode == "RANGE_BALANCE"
    assert state.current_reaction == "NONE"
    assert len(state.levels) == 2


def test_refinement_a_threads_tf_into_nearest_support(tmp_path) -> None:
    """Refinement A — the nearest_support's timeframe is recovered
    from the matching compact-levels entry."""
    path = tmp_path / "structure.jsonl"
    rec = _good_rec(
        "GBPUSD",
        nearest_support=1.30000,
        nearest_support_score=7.5,
        nearest_resistance=1.31000,
        nearest_resistance_score=8.0,
        levels=[
            {"p": 1.30000, "s": "SUPPORT", "sc": 7.5, "tf": "M5"},
            {"p": 1.31000, "s": "RESISTANCE", "sc": 8.0, "tf": "M15"},
        ],
    )
    path.write_text(json.dumps(rec) + "\n")
    state = load_latest_structure_state_per_pair(path)["GBPUSD"]
    assert state.nearest_support.timeframe == "M5"
    assert state.nearest_resistance.timeframe == "M15"


def test_refinement_a_threads_tf_through_liquidity_zone_match(tmp_path) -> None:
    """Refinement A — a SUPPORT-nearest at the same quantised price as
    a LIQUIDITY_LOW compact entry should pick up the LIQUIDITY_LOW's
    timeframe (the engine wraps the underlying zone as LIQUIDITY_LOW
    when it's an equal-low cluster; nearest_support carries the
    canonical SUPPORT level_type)."""
    path = tmp_path / "structure.jsonl"
    rec = _good_rec(
        "GBPUSD",
        nearest_support=1.30000,
        nearest_support_score=7.5,
        levels=[
            {"p": 1.30000, "s": "LIQUIDITY_LOW", "sc": 7.5, "tf": "H1"},
        ],
    )
    path.write_text(json.dumps(rec) + "\n")
    state = load_latest_structure_state_per_pair(path)["GBPUSD"]
    assert state.nearest_support is not None
    assert state.nearest_support.level_type == "SUPPORT"  # canonical side
    assert state.nearest_support.timeframe == "H1"  # from LIQUIDITY_LOW match


def test_nearest_without_match_falls_back_to_h1(tmp_path) -> None:
    """Cap-evicted nearest level (or pre-refinement-A record): no
    matching compact entry → conservative H1 fallback."""
    path = tmp_path / "structure.jsonl"
    rec = _good_rec(
        "GBPUSD",
        nearest_support=1.30000,
        nearest_support_score=7.5,
        levels=[],  # no compact entries at all
    )
    path.write_text(json.dumps(rec) + "\n")
    state = load_latest_structure_state_per_pair(path)["GBPUSD"]
    assert state.nearest_support is not None
    assert state.nearest_support.price == 1.30000
    assert state.nearest_support.score == 7.5
    assert state.nearest_support.timeframe == "H1"
    assert state.nearest_support.debug.get("hydrated_fallback") is True


def test_nearest_score_missing_defaults_to_zero(tmp_path) -> None:
    """Pre-refinement-A record (no nearest_*_score field) doesn't
    crash; score defaults to 0.

    Note: the "no spurious NEW_MAJOR_LEVEL on first post-upgrade bar"
    invariant is enforced by ``compute_structure_diff`` including
    ``prev.nearest_support`` / ``prev.nearest_resistance`` in
    ``prev_keys`` (see H1 in the C-6 review and the
    ``test_pre_refinement_a_record_does_not_spuriously_fire_new_major_level``
    test in test_diff.py). The rehydrated score=0.0 default plays no
    role in that protection — the diff-layer threshold check reads
    curr's score, not prev's.
    """
    path = tmp_path / "structure.jsonl"
    rec = _good_rec(
        "GBPUSD",
        nearest_support=1.30000,
        levels=[],
    )
    # Strip the score field — simulating an old record.
    rec.pop("nearest_support_score", None)
    path.write_text(json.dumps(rec) + "\n")
    state = load_latest_structure_state_per_pair(path)["GBPUSD"]
    assert state.nearest_support is not None
    assert state.nearest_support.score == 0.0


def test_nearest_none_hydrates_as_none(tmp_path) -> None:
    path = tmp_path / "structure.jsonl"
    rec = _good_rec("GBPUSD", nearest_support=None, nearest_resistance=None)
    path.write_text(json.dumps(rec) + "\n")
    state = load_latest_structure_state_per_pair(path)["GBPUSD"]
    assert state.nearest_support is None
    assert state.nearest_resistance is None


# ---------------------------------------------------------------------------
# Multi-pair / last-write-wins
# ---------------------------------------------------------------------------


def test_mixed_pairs_returns_latest_per_pair(tmp_path) -> None:
    """3 GBPUSD lines + 2 EURUSD lines: dict has both pairs, each
    carrying its last record."""
    path = tmp_path / "structure.jsonl"
    lines = []
    for pair, bias in [
        ("GBPUSD", "NEUTRAL"),
        ("GBPUSD", "BULLISH"),
        ("EURUSD", "NEUTRAL"),
        ("GBPUSD", "BEARISH"),
        ("EURUSD", "BULLISH"),
    ]:
        lines.append(json.dumps(_good_rec(pair, htf_bias=bias)))
    path.write_text("\n".join(lines) + "\n")

    result = load_latest_structure_state_per_pair(path)
    assert set(result.keys()) == {"GBPUSD", "EURUSD"}
    assert result["GBPUSD"].htf_bias == "BEARISH"
    assert result["EURUSD"].htf_bias == "BULLISH"


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


def test_corrupt_line_skipped_and_warning_logged(tmp_path, caplog) -> None:
    path = tmp_path / "structure.jsonl"
    good = _good_rec("GBPUSD", htf_bias="BULLISH")
    path.write_text("not json at all\n" + json.dumps(good) + "\n")
    with caplog.at_level("WARNING", logger="structure_alerts.hydration"):
        result = load_latest_structure_state_per_pair(path)
    assert "GBPUSD" in result
    assert result["GBPUSD"].htf_bias == "BULLISH"
    matching = [r for r in caplog.records if "unparseable" in r.getMessage()]
    assert len(matching) == 1


def test_record_missing_pair_skipped(tmp_path) -> None:
    """A record without a 'pair' field is observability garbage and
    should be skipped, not crash hydration."""
    path = tmp_path / "structure.jsonl"
    bad = {"timestamp": "2026-05-16T09:00:00+00:00"}
    good = _good_rec("GBPUSD", htf_bias="BULLISH")
    path.write_text(json.dumps(bad) + "\n" + json.dumps(good) + "\n")
    result = load_latest_structure_state_per_pair(path)
    assert set(result.keys()) == {"GBPUSD"}


def test_levels_with_invalid_entries_filtered_out(tmp_path) -> None:
    """A mix of valid + malformed level entries: valid ones land in
    state.levels, malformed ones are silently skipped."""
    path = tmp_path / "structure.jsonl"
    rec = _good_rec(
        "GBPUSD",
        levels=[
            {"p": 1.30000, "s": "SUPPORT", "sc": 7.0, "tf": "H1"},  # valid
            {"p": 1.31000, "s": "WEIRD_TYPE", "sc": 7.0, "tf": "H1"},  # bad s
            {"p": 1.32000, "s": "SUPPORT", "sc": 7.0, "tf": "X"},  # bad tf
            {"p": "nope", "s": "SUPPORT", "sc": 7.0, "tf": "H1"},  # bad p
            {"s": "SUPPORT", "sc": 7.0, "tf": "H1"},  # missing p
            "not even a dict",
        ],
    )
    path.write_text(json.dumps(rec) + "\n")
    state = load_latest_structure_state_per_pair(path)["GBPUSD"]
    assert len(state.levels) == 1
    assert state.levels[0].price == 1.30000


# ---------------------------------------------------------------------------
# End-to-end round-trip via the Phase 11 logger
# ---------------------------------------------------------------------------


def test_round_trip_via_phase11_logger(tmp_path, monkeypatch) -> None:
    """Smoke test: write via :func:`log_structure_state`, read via
    :func:`load_latest_structure_state_per_pair`. Verifies the
    refinement-A fields land in the jsonl in the shape hydration
    consumes (no contract drift between the two layers).
    """
    monkeypatch.setattr(struct_log, "STRUCTURE_LOG_ENABLED", True)
    path = tmp_path / "structure.jsonl"
    monkeypatch.setattr(struct_log, "STRUCTURE_LOG_PATH", str(path))

    sup = StructureLevel(
        pair="GBPUSD",
        level_type="SUPPORT",
        price=1.30000,
        zone_low=1.29996,
        zone_high=1.30004,
        timeframe="H1",
        score=7.5,
        touch_count=2,
        last_touched_ts=None,
        source="swing_h1",
        debug={"engine_internal": "stripped"},
    )
    res = StructureLevel(
        pair="GBPUSD",
        level_type="RESISTANCE",
        price=1.31000,
        zone_low=1.30996,
        zone_high=1.31004,
        timeframe="M15",
        score=8.0,
        touch_count=1,
        last_touched_ts=None,
        source="swing_m15",
        debug={"engine_internal": "stripped"},
    )
    state = StructureState(
        pair="GBPUSD",
        timestamp="2026-05-16T09:00:00+00:00",
        is_valid=True,
        htf_bias="BEARISH",
        local_bias="NEUTRAL",
        nearest_support=sup,
        nearest_resistance=res,
        liquidity_above=None,
        liquidity_below=None,
        current_reaction="SUPPORT_REJECTION",
        acceptance_state="INSIDE_RANGE",
        structure_mode="VOLATILE_SWEEP_ZONE",
        confidence=0.65,
        reason="test",
        levels=[sup, res],
        debug={"engine_internal": "stripped"},
    )
    struct_log.log_structure_state(state)
    # Second snapshot one bar later — last-write-wins should keep this one.
    state2 = StructureState(
        pair="GBPUSD",
        timestamp="2026-05-16T09:05:00+00:00",
        is_valid=True,
        htf_bias="BEARISH",
        local_bias="BEARISH",
        nearest_support=sup,
        nearest_resistance=res,
        liquidity_above=None,
        liquidity_below=None,
        current_reaction="SUPPORT_ACCEPTANCE_BREAK",
        acceptance_state="ACCEPTED_BELOW_SUPPORT",
        structure_mode="VOLATILE_SWEEP_ZONE",
        confidence=0.7,
        reason="test2",
        levels=[sup, res],
        debug={},
    )
    struct_log.log_structure_state(state2)

    result = load_latest_structure_state_per_pair(path)
    assert "GBPUSD" in result
    rehydrated = result["GBPUSD"]
    # Last-write-wins: state2's values surface.
    assert rehydrated.timestamp == "2026-05-16T09:05:00+00:00"
    assert rehydrated.current_reaction == "SUPPORT_ACCEPTANCE_BREAK"
    assert rehydrated.acceptance_state == "ACCEPTED_BELOW_SUPPORT"
    # Diff-relevant fields preserved.
    assert rehydrated.htf_bias == "BEARISH"
    assert rehydrated.structure_mode == "VOLATILE_SWEEP_ZONE"
    # Refinement A: tf threaded through both nearest_* fields.
    assert rehydrated.nearest_support is not None
    assert rehydrated.nearest_support.price == 1.30000
    assert rehydrated.nearest_support.score == 7.5
    assert rehydrated.nearest_support.timeframe == "H1"
    assert rehydrated.nearest_resistance.timeframe == "M15"
    # Per-zone debug payloads stripped on the way down; only the
    # hydration marker survives.
    assert rehydrated.nearest_support.debug == {"hydrated": True}
    assert len(rehydrated.levels) == 2


# ---------------------------------------------------------------------------
# Local helper
# ---------------------------------------------------------------------------


def _good_rec(pair: str, **overrides) -> dict:
    """Build a Phase 11–compatible record with sensible defaults.

    Tests override only the fields they care about. Mirrors what
    :func:`structure_engine.logging._to_payload` produces post
    refinement A so the hydration side stays honest about the
    payload contract.
    """
    rec = {
        "timestamp": "2026-05-16T09:00:00+00:00",
        "pair": pair,
        "is_valid": True,
        "htf_bias": "NEUTRAL",
        "local_bias": "NEUTRAL",
        "nearest_support": None,
        "nearest_support_score": None,
        "nearest_resistance": None,
        "nearest_resistance_score": None,
        "liquidity_above": None,
        "liquidity_below": None,
        "current_reaction": "NONE",
        "acceptance_state": "NONE",
        "structure_mode": "RANGE_BALANCE",
        "confidence": 0.7,
        "reason": "test",
        "levels": [],
    }
    rec.update(overrides)
    return rec
