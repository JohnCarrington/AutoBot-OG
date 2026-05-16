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


def test_log_payload_strips_dataframes(monkeypatch, tmp_path: Path) -> None:
    """Spec §15: no full DataFrames in jsonl."""
    monkeypatch.setattr(struct_log, "STRUCTURE_LOG_ENABLED", True)
    path = tmp_path / "structure.jsonl"
    monkeypatch.setattr(struct_log, "STRUCTURE_LOG_PATH", str(path))
    struct_log.log_structure_state(_state())
    payload = json.loads(path.read_text().strip())
    assert "levels" not in payload
    assert "debug" not in payload
