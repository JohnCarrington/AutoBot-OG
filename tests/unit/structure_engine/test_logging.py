"""Unit tests for :mod:`structure_engine.logging`."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import structure_engine.logging as struct_log
from structure_engine.types import StructureLevel, StructureState


def _state(**overrides) -> StructureState:
    base = dict(
        pair="GBPUSD",
        timestamp="2026-05-15T10:00:00+00:00",
        is_valid=True,
        htf_bias="BEARISH",
        local_bias="NEUTRAL",
        nearest_support=None,
        nearest_resistance=None,
        liquidity_above=None,
        liquidity_below=None,
        current_reaction="NONE",
        acceptance_state="NONE",
        structure_mode="UNKNOWN",
        confidence=0.5,
        reason="test",
        levels=[],
        debug={},
    )
    base.update(overrides)
    return StructureState(**base)


def test_log_disabled_is_noop(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(struct_log, "STRUCTURE_LOG_ENABLED", False)
    monkeypatch.setattr(
        struct_log, "STRUCTURE_LOG_PATH", str(tmp_path / "structure.jsonl")
    )
    struct_log.log_structure_state(_state())
    assert not (tmp_path / "structure.jsonl").exists()


def test_log_enabled_writes_one_line_per_call(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(struct_log, "STRUCTURE_LOG_ENABLED", True)
    path = tmp_path / "structure.jsonl"
    monkeypatch.setattr(struct_log, "STRUCTURE_LOG_PATH", str(path))
    struct_log.log_structure_state(_state(htf_bias="BULLISH"))
    struct_log.log_structure_state(_state(htf_bias="BEARISH"))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["htf_bias"] == "BULLISH"
    assert json.loads(lines[1])["htf_bias"] == "BEARISH"


def test_log_payload_strips_full_dataframes_and_debug(
    monkeypatch, tmp_path: Path,
) -> None:
    """Spec §15 + Phase 12 refinement A.

    The full-DataFrame and whole-state debug-dict stripping is the
    spec §15 contract. Phase 12 refinement A added a COMPACT
    ``levels`` list ({p, s, sc, tf} per entry, capped at 30) so
    structure-alerts hydration can rebuild prev-state on restart —
    that's additive and does NOT reintroduce DataFrames. The
    no-DataFrames test now asserts both: (1) whole-state ``debug``
    is absent, (2) ``levels`` is present as a list of compact dicts,
    not full StructureLevels.
    """
    monkeypatch.setattr(struct_log, "STRUCTURE_LOG_ENABLED", True)
    path = tmp_path / "structure.jsonl"
    monkeypatch.setattr(struct_log, "STRUCTURE_LOG_PATH", str(path))
    struct_log.log_structure_state(_state())
    payload = json.loads(path.read_text().strip())
    assert "debug" not in payload  # whole-state debug stripped (§15)
    assert payload["levels"] == []  # compact form retained, empty here


def _level(
    *,
    price: float = 1.30000,
    level_type: str = "SUPPORT",
    score: float = 7.0,
    timeframe: str = "H1",
) -> StructureLevel:
    """Local helper for Phase 12 refinement A tests."""
    return StructureLevel(
        pair="GBPUSD",
        level_type=level_type,  # type: ignore[arg-type]
        price=price,
        zone_low=price - 0.0004,
        zone_high=price + 0.0004,
        timeframe=timeframe,  # type: ignore[arg-type]
        score=score,
        touch_count=2,
        last_touched_ts=None,
        source="swing_h1",
        debug={"engine_internal": "stripped"},
    )


def test_payload_compact_levels_entries_shape(
    monkeypatch, tmp_path: Path,
) -> None:
    """Refinement A: each compact entry carries {p, s, sc, tf}."""
    monkeypatch.setattr(struct_log, "STRUCTURE_LOG_ENABLED", True)
    path = tmp_path / "structure.jsonl"
    monkeypatch.setattr(struct_log, "STRUCTURE_LOG_PATH", str(path))
    levels = [
        _level(price=1.30000, level_type="SUPPORT", score=7.0, timeframe="H1"),
        _level(
            price=1.31000, level_type="RESISTANCE", score=8.0, timeframe="M15",
        ),
    ]
    struct_log.log_structure_state(_state(levels=levels))
    payload = json.loads(path.read_text().strip())
    # Sorted by score descending, exact keys {p, s, sc, tf} only.
    assert payload["levels"] == [
        {"p": 1.31000, "s": "RESISTANCE", "sc": 8.0, "tf": "M15"},
        {"p": 1.30000, "s": "SUPPORT", "sc": 7.0, "tf": "H1"},
    ]


def test_payload_compact_levels_capped_at_max_per_record(
    monkeypatch, tmp_path: Path,
) -> None:
    """50 levels in → 30 levels out, highest scores retained."""
    monkeypatch.setattr(struct_log, "STRUCTURE_LOG_ENABLED", True)
    path = tmp_path / "structure.jsonl"
    monkeypatch.setattr(struct_log, "STRUCTURE_LOG_PATH", str(path))
    # Scores 50.0 down to 1.0; top 30 are 50..21.
    levels = [
        _level(price=1.30000 + i * 0.0001, score=50.0 - i)
        for i in range(50)
    ]
    struct_log.log_structure_state(_state(levels=levels))
    payload = json.loads(path.read_text().strip())
    assert len(payload["levels"]) == 30
    assert payload["levels"][0]["sc"] == 50.0
    assert payload["levels"][-1]["sc"] == 21.0


def test_payload_nearest_scores_present_when_nearest_set(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(struct_log, "STRUCTURE_LOG_ENABLED", True)
    path = tmp_path / "structure.jsonl"
    monkeypatch.setattr(struct_log, "STRUCTURE_LOG_PATH", str(path))
    sup = _level(price=1.30000, level_type="SUPPORT", score=7.5)
    res = _level(price=1.31000, level_type="RESISTANCE", score=8.2)
    struct_log.log_structure_state(
        _state(nearest_support=sup, nearest_resistance=res, levels=[sup, res]),
    )
    payload = json.loads(path.read_text().strip())
    assert payload["nearest_support"] == 1.30000
    assert payload["nearest_support_score"] == 7.5
    assert payload["nearest_resistance"] == 1.31000
    assert payload["nearest_resistance_score"] == 8.2


def test_payload_nearest_scores_none_when_nearest_missing(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(struct_log, "STRUCTURE_LOG_ENABLED", True)
    path = tmp_path / "structure.jsonl"
    monkeypatch.setattr(struct_log, "STRUCTURE_LOG_PATH", str(path))
    struct_log.log_structure_state(_state())  # both nearest are None
    payload = json.loads(path.read_text().strip())
    assert payload["nearest_support"] is None
    assert payload["nearest_support_score"] is None
    assert payload["nearest_resistance"] is None
    assert payload["nearest_resistance_score"] is None
