"""Direct unit tests for :func:`structure_engine.structure_state._compute_confidence`.

M-8 review fix (2026-05-16). Refinement B's "one-sided structure
doesn't penalise" behaviour was previously only exercised end-to-end
through ``analyze_structure``, where the test asserted ``[0, 1]`` —
which is true for any sane implementation. Direct tests pin every
branch explicitly.
"""
from __future__ import annotations

import pytest

from structure_engine.structure_state import _compute_confidence
from structure_engine.types import StructureLevel


def _level(score: float, *, side: str = "LOW") -> StructureLevel:
    level_type = "SUPPORT" if side == "LOW" else "RESISTANCE"
    price = 1.30000
    return StructureLevel(
        pair="GBPUSD",
        level_type=level_type,  # type: ignore[arg-type]
        price=price,
        zone_low=price - 0.0004,
        zone_high=price + 0.0004,
        timeframe="H1",
        score=score,
        touch_count=1,
        last_touched_ts=None,
        source="test",
        debug={},
    )


def test_both_present_takes_min_normalised() -> None:
    """``min(support.score, resistance.score) / 10.0``."""
    sup = _level(8.0, side="LOW")
    res = _level(6.0, side="HIGH")
    assert _compute_confidence(sup, res) == pytest.approx(0.6)


def test_only_support_uses_support_score() -> None:
    sup = _level(7.5, side="LOW")
    assert _compute_confidence(sup, None) == pytest.approx(0.75)


def test_only_resistance_uses_resistance_score() -> None:
    res = _level(7.2, side="HIGH")
    assert _compute_confidence(None, res) == pytest.approx(0.72)


def test_both_none_returns_zero() -> None:
    assert _compute_confidence(None, None) == 0.0


def test_one_sided_is_not_penalised_for_missing_side() -> None:
    """Refinement B's whole point: a single-side score of 8.0 should
    yield confidence 0.8, NOT min(8.0, 0.0) / 10 = 0.0."""
    sup = _level(8.0, side="LOW")
    assert _compute_confidence(sup, None) == pytest.approx(0.8)
    res = _level(8.0, side="HIGH")
    assert _compute_confidence(None, res) == pytest.approx(0.8)
