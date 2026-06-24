"""Tests for the (strategy × day_type) management matrix (step 6).

Three concerns:

1. Pin each cell's exact field values so a future typo in
   ``MANAGEMENT_MATRIX`` breaks a test.
2. Confirm ``profile_for`` raises ``KeyError`` for an unreachable cell
   (the fail-loud contract).
3. The keyset-matches-dispatcher guard — flagged in Phase 1 — asserts
   the set of (strategy_name, day_type) the dispatcher can actually
   produce equals ``MANAGEMENT_MATRIX.keys()``. Prevents the matrix
   and dispatcher from drifting silently.
"""
from __future__ import annotations

import pytest

from day_type import DayType
from strategies import dispatcher as dispatcher_mod
from strategies.management import (
    MANAGEMENT_MATRIX,
    ManagementProfile,
    profile_for,
)


# --- Per-cell pins ---------------------------------------------------------


def test_bb_bounce_normal_profile() -> None:
    p = profile_for("bb_bounce", DayType.NORMAL)
    assert p == ManagementProfile(
        sl_atr_mult=0.8,
        sl_floor_pips_override=None,
        be_trigger_r=1.0,
        be_buffer_pips=1.0,
        trail_primary="ema20",
        trail_secondary="swing",
        trail_min_delta_pips=1.0,
        tp_mode="fixed_zone_midpoint",
        eager_structure_exit_enabled=True,
    )


def test_ema_pullback_big_news_day_profile() -> None:
    p = profile_for("ema_pullback", DayType.BIG_NEWS_DAY)
    assert p == ManagementProfile(
        sl_atr_mult=1.8,
        sl_floor_pips_override=None,
        be_trigger_r=1.5,
        be_buffer_pips=1.0,
        trail_primary="swing",
        trail_secondary="ema20",
        trail_min_delta_pips=1.0,
        tp_mode="none",
        eager_structure_exit_enabled=True,
    )


def test_ema_pullback_pre_big_news_profile() -> None:
    p = profile_for("ema_pullback", DayType.PRE_BIG_NEWS)
    assert p == ManagementProfile(
        sl_atr_mult=1.5,
        sl_floor_pips_override=None,
        be_trigger_r=1.5,
        be_buffer_pips=1.0,
        trail_primary="swing",
        trail_secondary="ema20",
        trail_min_delta_pips=1.0,
        tp_mode="none",
        eager_structure_exit_enabled=True,
    )


def test_structure_break_big_news_day_profile() -> None:
    p = profile_for("structure_break", DayType.BIG_NEWS_DAY)
    assert p == ManagementProfile(
        sl_atr_mult=1.8,
        sl_floor_pips_override=None,
        be_trigger_r=1.5,
        be_buffer_pips=1.0,
        trail_primary="swing",
        trail_secondary="ema20",
        trail_min_delta_pips=1.0,
        tp_mode="none",
        eager_structure_exit_enabled=True,
    )


def test_structure_break_pre_big_news_profile() -> None:
    p = profile_for("structure_break", DayType.PRE_BIG_NEWS)
    assert p == ManagementProfile(
        sl_atr_mult=1.5,
        sl_floor_pips_override=None,
        be_trigger_r=1.5,
        be_buffer_pips=1.0,
        trail_primary="swing",
        trail_secondary="ema20",
        trail_min_delta_pips=1.0,
        tp_mode="none",
        eager_structure_exit_enabled=True,
    )


def test_news_big_news_day_profile() -> None:
    """Step 5b: news matches the BIG_NEWS_DAY cell's wider 1.8 × ATR
    stop, no fixed TP — structure trail by execution.sl_management."""
    p = profile_for("news", DayType.BIG_NEWS_DAY)
    assert p == ManagementProfile(
        sl_atr_mult=1.8,
        sl_floor_pips_override=None,
        be_trigger_r=1.5,
        be_buffer_pips=1.0,
        trail_primary="swing",
        trail_secondary="ema20",
        trail_min_delta_pips=1.0,
        tp_mode="none",
        eager_structure_exit_enabled=True,
    )


# --- Fail-loud on unreachable cells ----------------------------------------


def test_profile_for_raises_on_unreachable_bb_bounce_news_day() -> None:
    """bb_bounce is only dispatched on NORMAL — news-day combinations
    are unreachable and must raise."""
    with pytest.raises(KeyError):
        profile_for("bb_bounce", DayType.BIG_NEWS_DAY)


def test_profile_for_raises_on_unreachable_ema_pullback_normal() -> None:
    """ema_pullback is only dispatched on news days — NORMAL is
    unreachable."""
    with pytest.raises(KeyError):
        profile_for("ema_pullback", DayType.NORMAL)


def test_profile_for_raises_on_unreachable_structure_break_normal() -> None:
    with pytest.raises(KeyError):
        profile_for("structure_break", DayType.NORMAL)


def test_profile_for_raises_on_unknown_strategy() -> None:
    """A strategy_name the dispatcher doesn't even produce raises."""
    with pytest.raises(KeyError):
        profile_for("experimental_pattern", DayType.NORMAL)


def test_keyerror_message_lists_reachable_cells() -> None:
    """The fail-loud error message names the reachable cells so a
    developer can fix the call site without consulting the matrix."""
    with pytest.raises(KeyError) as exc_info:
        profile_for("ema_pullback", DayType.NORMAL)
    msg = str(exc_info.value)
    assert "bb_bounce" in msg and "NORMAL" in msg
    assert "structure_break" in msg


# --- Keyset == dispatcher reachable set ------------------------------------


def _dispatcher_reachable_cells() -> set[tuple[str, DayType]]:
    """Derive the (strategy_name, day_type) set the dispatcher can
    actually produce. Step 5b: ``detect_news`` is now a real detector,
    so the ``(news, BIG_NEWS_DAY)`` cell is reachable.

    Mapping detector → strategy_name is by module convention:
    ``strategies.<name>.detect_<name>`` emits ``strategy_name=<name>``.
    """
    detector_to_strategy = {
        dispatcher_mod.detect_bb_bounce: "bb_bounce",
        dispatcher_mod.detect_ema_pullback: "ema_pullback",
        dispatcher_mod.detect_structure_break: "structure_break",
        dispatcher_mod.detect_news: "news",
    }
    reachable: set[tuple[str, DayType]] = set()
    for day_type, detectors in dispatcher_mod.DISPATCH.items():
        for det in detectors:
            strategy_name = detector_to_strategy.get(det)
            if strategy_name is None:
                continue  # stub
            reachable.add((strategy_name, day_type))
    return reachable


def test_management_keyset_matches_dispatcher() -> None:
    """The matrix and the dispatcher must stay in lockstep.

    If the dispatcher routes a real detector to a (strategy, day_type)
    the matrix doesn't cover, the position will raise ``KeyError`` at
    its first BE/trail evaluation — production breakage. Equivalently,
    a matrix entry that no dispatcher route ever produces is dead code.
    """
    dispatcher_set = _dispatcher_reachable_cells()
    matrix_set = set(MANAGEMENT_MATRIX.keys())
    assert dispatcher_set == matrix_set, (
        f"matrix ↔ dispatcher drift; "
        f"only-in-dispatcher={dispatcher_set - matrix_set}, "
        f"only-in-matrix={matrix_set - dispatcher_set}"
    )


def test_matrix_has_exactly_six_cells() -> None:
    """Belt-and-braces: pin the cell count so an accidental addition
    breaks visibly even before the keyset test diagnoses what.

    Step 5b: ``(news, BIG_NEWS_DAY)`` joined the reachable set when
    the real ``detect_news`` replaced the stub.
    """
    assert len(MANAGEMENT_MATRIX) == 6


def test_eager_structure_exit_enabled_is_true_for_all_cells() -> None:
    """Phase 1 confirmed no eager structure-exit mechanism exists today.
    All current cells flag the forward-guard as enabled — the field
    documents intent for the day a mid-trade structure-flip exit lands.
    If a future cell wants the flag off, this test will need updating
    deliberately."""
    for cell, profile in MANAGEMENT_MATRIX.items():
        assert profile.eager_structure_exit_enabled is True, (
            f"cell {cell!r} unexpectedly has eager_structure_exit_enabled=False"
        )
