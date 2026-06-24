"""(strategy × day_type) management matrix — step 6.

The dispatcher routes each ``DayType`` to a tuple of detectors; each
detector emits a Signal with a fixed ``strategy_name``. Once a position
is opened from that Signal, the (strategy, day_type) pair drives:

- entry SL sizing (``sl_atr_mult``, ``sl_floor_pips_override``);
- entry TP mode (``tp_mode``);
- post-entry BE move (``be_trigger_r``, ``be_buffer_pips``);
- post-entry trail (``trail_primary``, ``trail_secondary``,
  ``trail_min_delta_pips``);
- a forward-guard ``eager_structure_exit_enabled`` — Phase 1 confirmed
  no eager structure-exit mechanism exists today; the field documents
  intent so the day a mid-trade structure-flip exit is added it picks
  up the (strategy, day_type) routing for free.

The matrix lives here because both consumer layers (``strategies/*``
for entry, ``execution/sl_management`` for post-entry) already import
strategy-shaped types — putting it under ``strategies`` introduces no
new cross-layer dependency. Lookup via :py:func:`profile_for` is
fail-loud: a missing cell raises ``KeyError``. That's deliberate — a
dispatcher cell with no matrix entry is a bug we want to surface
instead of silently defaulting.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from day_type import DayType

from .constants import BB_RECLAIM_ATR_MULT
from .signal import StrategyName


TrailKind = Literal["ema20", "swing", "none"]
TPMode = Literal["fixed_zone_midpoint", "none"]


@dataclass(frozen=True)
class ManagementProfile:
    """Per-cell management knobs.

    All numeric fields are spec-time decisions, not env-tunable. Ops
    overrides are deliberately constrained — the matrix is the single
    source of truth so a divergent cell can't slip in via env var.
    """

    sl_atr_mult: float
    sl_floor_pips_override: float | None
    be_trigger_r: float
    be_buffer_pips: float
    trail_primary: TrailKind
    trail_secondary: TrailKind
    trail_min_delta_pips: float
    tp_mode: TPMode
    eager_structure_exit_enabled: bool


# --- The matrix (5 reachable cells per the dispatcher table) ---------------
#
# Reachability checked by ``test_management_keyset_matches_dispatcher`` —
# adding a cell here without a corresponding dispatcher route (or vice
# versa) breaks that test.
#
# bb_bounce keeps the historical BB_RECLAIM_ATR_MULT (0.8) verbatim — no
# divergence. ema_pullback / structure_break news-day cells DIVERGE from
# their old single-value ATR mults (EMA_CONT 1.2, STRUCT_BREAK 1.0) up
# to the wider 1.5 / 1.8 the spec calls for on news days, so the old
# constants are no longer the single source of truth for those cells —
# the matrix wins.

MANAGEMENT_MATRIX: dict[tuple[StrategyName, DayType], ManagementProfile] = {
    (
        "bb_bounce",
        DayType.NORMAL,
    ): ManagementProfile(
        # bb_bounce cell does not diverge from the legacy ATR mult — keep
        # the constant as the source so an ops env override on
        # ``STRATEGY_BB_RECLAIM_ATR_MULT`` still flows through.
        sl_atr_mult=BB_RECLAIM_ATR_MULT,
        sl_floor_pips_override=None,
        be_trigger_r=1.0,
        be_buffer_pips=1.0,
        trail_primary="ema20",
        trail_secondary="swing",
        trail_min_delta_pips=1.0,
        tp_mode="fixed_zone_midpoint",
        eager_structure_exit_enabled=True,
    ),
    (
        "ema_pullback",
        DayType.BIG_NEWS_DAY,
    ): ManagementProfile(
        sl_atr_mult=1.8,
        sl_floor_pips_override=None,
        be_trigger_r=1.5,
        be_buffer_pips=1.0,
        trail_primary="swing",
        trail_secondary="ema20",
        trail_min_delta_pips=1.0,
        tp_mode="none",
        eager_structure_exit_enabled=True,
    ),
    (
        "ema_pullback",
        DayType.PRE_BIG_NEWS,
    ): ManagementProfile(
        sl_atr_mult=1.5,
        sl_floor_pips_override=None,
        be_trigger_r=1.5,
        be_buffer_pips=1.0,
        trail_primary="swing",
        trail_secondary="ema20",
        trail_min_delta_pips=1.0,
        tp_mode="none",
        eager_structure_exit_enabled=True,
    ),
    (
        "structure_break",
        DayType.BIG_NEWS_DAY,
    ): ManagementProfile(
        sl_atr_mult=1.8,
        sl_floor_pips_override=None,
        be_trigger_r=1.5,
        be_buffer_pips=1.0,
        trail_primary="swing",
        trail_secondary="ema20",
        trail_min_delta_pips=1.0,
        tp_mode="none",
        eager_structure_exit_enabled=True,
    ),
    (
        "structure_break",
        DayType.PRE_BIG_NEWS,
    ): ManagementProfile(
        sl_atr_mult=1.5,
        sl_floor_pips_override=None,
        be_trigger_r=1.5,
        be_buffer_pips=1.0,
        trail_primary="swing",
        trail_secondary="ema20",
        trail_min_delta_pips=1.0,
        tp_mode="none",
        eager_structure_exit_enabled=True,
    ),
}


def profile_for(
    strategy_name: str, day_type: DayType
) -> ManagementProfile:
    """Return the ManagementProfile for ``(strategy_name, day_type)``.

    Raises ``KeyError`` for an unreachable cell — by design. A
    dispatcher route that produces a (strategy, day_type) the matrix
    doesn't cover is a bug; silently defaulting would hide it.
    """
    key = (strategy_name, day_type)
    try:
        return MANAGEMENT_MATRIX[key]  # type: ignore[index]
    except KeyError as exc:
        raise KeyError(
            f"No ManagementProfile for (strategy_name={strategy_name!r}, "
            f"day_type={day_type!r}). Reachable cells: "
            f"{sorted((s, d.value) for s, d in MANAGEMENT_MATRIX)}"
        ) from exc


__all__ = [
    "MANAGEMENT_MATRIX",
    "ManagementProfile",
    "TPMode",
    "TrailKind",
    "profile_for",
]
