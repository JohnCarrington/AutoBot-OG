"""Unit tests for :mod:`structure_engine.scoring`."""
from __future__ import annotations

import pytest

from structure_engine.scoring import score_zone
from structure_engine.zone_builder import CandidateZone


def _zone(**overrides) -> CandidateZone:
    base = dict(
        pair="GBPUSD",
        side="LOW",
        price=1.30000,
        zone_low=1.29960,
        zone_high=1.30040,
        timeframe="H1",
        sources=["swing_h1"],
        touch_count=1,
        last_touched_ts=None,
        reaction_atr_mult=0.0,
        bars_since_last_touch=5,
        invalidated=False,
        is_equal_hl_cluster=False,
        is_session_level=False,
        session_kind=None,
    )
    base.update(overrides)
    return CandidateZone(**base)


def test_h1_outscores_m5_at_equal_touches() -> None:
    """Spec §16 test 3: H1 with 3 touches > M5 with 1 touch."""
    h1 = _zone(timeframe="H1", touch_count=3, bars_since_last_touch=5)
    m5 = _zone(timeframe="M5", touch_count=1, bars_since_last_touch=5)
    h1_score, _ = score_zone(h1)
    m5_score, _ = score_zone(m5)
    assert h1_score > m5_score


def test_score_capped_at_score_cap() -> None:
    z = _zone(
        timeframe="H1",
        touch_count=5,
        reaction_atr_mult=2.0,
        bars_since_last_touch=1,
        is_equal_hl_cluster=True,
        is_session_level=True,
        session_kind="prev_day",
    )
    score, _ = score_zone(z)
    assert score <= 10.0


def test_invalidation_penalty_applied() -> None:
    pristine = _zone(timeframe="H1", touch_count=2)
    broken = _zone(timeframe="H1", touch_count=2, invalidated=True)
    p_score, _ = score_zone(pristine)
    b_score, _ = score_zone(broken)
    assert p_score - b_score == pytest.approx(2.0)


def test_components_breakdown_sums_to_score() -> None:
    z = _zone(
        timeframe="H1",
        touch_count=2,
        reaction_atr_mult=1.6,
        bars_since_last_touch=8,
    )
    score, components = score_zone(z)
    summed = sum(v for k, v in components.items())
    assert score == pytest.approx(max(0.0, summed))


def test_session_score_only_when_session_level() -> None:
    z_session = _zone(is_session_level=True, session_kind="prev_day")
    z_plain = _zone(is_session_level=False, session_kind="prev_day")
    s_score, _ = score_zone(z_session)
    p_score, _ = score_zone(z_plain)
    assert s_score > p_score
