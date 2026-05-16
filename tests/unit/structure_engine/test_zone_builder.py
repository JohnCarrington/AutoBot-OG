"""Unit tests for :mod:`structure_engine.zone_builder`."""
from __future__ import annotations

import pytest

from structure_engine.zone_builder import (
    CandidateZone,
    half_width_for,
    make_zone,
    merge_zones,
)


_PAIR = "GBPUSD"


def test_half_width_falls_back_to_pip_floor_when_atr_zero() -> None:
    # GBPUSD pip floor = 4 pips = 0.0004.
    assert half_width_for(_PAIR, 0.0) == pytest.approx(0.0004)


def test_half_width_uses_atr_when_larger() -> None:
    # 0.0040 * 0.25 = 0.0010 > 0.0004 floor.
    assert half_width_for(_PAIR, 0.0040) == pytest.approx(0.0010)


def test_half_width_handles_nan_atr() -> None:
    assert half_width_for(_PAIR, float("nan")) == pytest.approx(0.0004)


def test_make_zone_centres_band_on_price() -> None:
    zone = make_zone(
        pair=_PAIR,
        side="LOW",
        price=1.30000,
        timeframe="H1",
        half_width=0.0004,
        source="swing_h1",
    )
    assert zone.price == pytest.approx(1.30000)
    assert zone.zone_low == pytest.approx(1.29960)
    assert zone.zone_high == pytest.approx(1.30040)
    assert zone.sources == ["swing_h1"]


def test_merge_overlapping_zones() -> None:
    a = make_zone(pair=_PAIR, side="LOW", price=1.30000, timeframe="H1",
                  half_width=0.0004, source="swing_h1")
    b = make_zone(pair=_PAIR, side="LOW", price=1.30002, timeframe="M5",
                  half_width=0.0004, source="swing_m5")
    merged = merge_zones([a, b])
    assert len(merged) == 1
    # Weighted toward H1 (weight 3.0) vs M5 (weight 1.0). New price closer
    # to 1.30000 than to 1.30002.
    expected = (1.30000 * 3.0 + 1.30002 * 1.0) / 4.0
    assert merged[0].price == pytest.approx(expected, abs=1e-7)
    # Sources merged.
    assert set(merged[0].sources) == {"swing_h1", "swing_m5"}


def test_merge_keeps_zones_on_different_sides() -> None:
    """A swing high and a swing low at the same price stay separate."""
    high = make_zone(pair=_PAIR, side="HIGH", price=1.30000, timeframe="H1",
                     half_width=0.0004, source="swing_h1")
    low = make_zone(pair=_PAIR, side="LOW", price=1.30000, timeframe="H1",
                    half_width=0.0004, source="swing_h1")
    merged = merge_zones([high, low])
    assert len(merged) == 2


def test_merge_three_levels_into_one() -> None:
    """Spec §16 test 2: 13340, 13342, 13344 should merge into one zone."""
    half = 0.0004
    a = make_zone(pair=_PAIR, side="LOW", price=1.33400, timeframe="H1",
                  half_width=half, source="swing_h1_a")
    b = make_zone(pair=_PAIR, side="LOW", price=1.33420, timeframe="H1",
                  half_width=half, source="swing_h1_b")
    c = make_zone(pair=_PAIR, side="LOW", price=1.33440, timeframe="H1",
                  half_width=half, source="swing_h1_c")
    merged = merge_zones([a, b, c])
    assert len(merged) == 1
    assert merged[0].zone_low <= 1.33396
    assert merged[0].zone_high >= 1.33444


def test_merge_skipped_when_distance_exceeds_combined_widths() -> None:
    a = make_zone(pair=_PAIR, side="LOW", price=1.30000, timeframe="H1",
                  half_width=0.0004, source="swing_h1")
    b = make_zone(pair=_PAIR, side="LOW", price=1.30100, timeframe="H1",
                  half_width=0.0004, source="swing_h1")  # 10 pips apart
    merged = merge_zones([a, b])
    assert len(merged) == 2


def test_merged_zone_preserves_swing_strength_count() -> None:
    """H-2 regression: cluster detection counts member swings, not sources.

    Three H1 swings clustering at one price merge into one zone whose
    ``sources`` field deduplicates to a single ``"swing_h1"`` string —
    but the ``swing_strengths`` list preserves one entry per member,
    which is what :func:`_mark_equal_hl_clusters` reads.
    """
    half = 0.0004
    rows = [
        make_zone(pair=_PAIR, side="HIGH", price=p, timeframe="H1",
                  half_width=half, source="swing_h1", swing_strength=0.8)
        for p in (1.33600, 1.33605, 1.33610)
    ]
    merged = merge_zones(rows)
    assert len(merged) == 1
    # Source string set deduplicates by string identity → 1 entry.
    assert merged[0].sources == ["swing_h1"]
    # Strength list preserves all 3 members.
    assert len(merged[0].swing_strengths) == 3
