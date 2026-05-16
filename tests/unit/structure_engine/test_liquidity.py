"""Unit tests for :mod:`structure_engine.liquidity`."""
from __future__ import annotations

import pytest

from structure_engine.liquidity import pick_liquidity_above, pick_liquidity_below
from structure_engine.zone_builder import CandidateZone, make_zone


def _z(side: str, price: float, **kw) -> CandidateZone:
    z = make_zone(
        pair="GBPUSD", side=side, price=price, timeframe="H1",
        half_width=0.0004, source="swing_h1",
    )
    for k, v in kw.items():
        setattr(z, k, v)
    return z


def test_liquidity_above_returns_none_when_no_high_zones() -> None:
    assert pick_liquidity_above(current_price=1.30000, zones=[]) is None


def test_liquidity_above_prefers_equal_hl_cluster() -> None:
    cluster = _z("HIGH", 1.30100, is_equal_hl_cluster=True)
    plain = _z("HIGH", 1.30050)  # closer but not a cluster
    pick = pick_liquidity_above(
        current_price=1.30000, zones=[cluster, plain]
    )
    assert pick is cluster


def test_liquidity_above_picks_nearest_within_tier() -> None:
    near = _z("HIGH", 1.30050)
    far = _z("HIGH", 1.30200)
    pick = pick_liquidity_above(current_price=1.30000, zones=[near, far])
    assert pick is near


def test_liquidity_below_only_picks_lower_zones() -> None:
    below = _z("LOW", 1.29900)
    above = _z("LOW", 1.30100)  # same side but above current
    pick = pick_liquidity_below(
        current_price=1.30000, zones=[below, above]
    )
    assert pick is below


def test_liquidity_below_prefers_session_when_no_cluster() -> None:
    session = _z("LOW", 1.29800, is_session_level=True, session_kind="prev_day")
    plain = _z("LOW", 1.29900)
    pick = pick_liquidity_below(
        current_price=1.30000, zones=[session, plain]
    )
    assert pick is session
