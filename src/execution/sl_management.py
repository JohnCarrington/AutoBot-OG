"""Stop-loss management: BE move + structure / EMA20 trailing.

Pure logic — given an :py:class:`ExecutionPosition`, the latest M5
DataFrame, and the current price, decide whether to emit an
:py:class:`AmendOrder`. No broker calls; the executor forwards the
order to :py:class:`feed.ig_rest.IGClient`.

Step 6: the BE trigger, BE buffer, trail priority, and min-delta all
flow from the :py:func:`strategies.management.profile_for` lookup
keyed on ``(position.strategy_name, position.day_type_at_entry)`` —
both fields are confirmed present + persisted (Phase 1 mapping). A
single ``profile = profile_for(...)`` call near the top of
:py:func:`evaluate_sl_amend` resolves all knobs for the call.

The lookup is fail-loud: a position whose (strategy, day_type) is not
in the matrix raises ``KeyError``. That's deliberate — the dispatcher
and matrix must stay in lockstep.

Spec sources
------------
- ``docs/v1_architecture.md`` §6.2 (BE move).
- ``docs/v1_architecture.md`` §6.3 (trail per strategy).
- §6.11 (Phase 6 locked decisions): trail gates on ``be_moved``;
  conservative = closer-to-price; never-widen rule; EMA20 wrong-side
  rejection.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from config.pair_config import MIN_SL_PIPS, pip_size_for, pip_to_price, price_to_pips
from common import Direction
from strategies.management import ManagementProfile, profile_for
from structure import get_structure_state

from .types import AmendOrder, ExecutionPosition


def evaluate_sl_amend(
    position: ExecutionPosition,
    df_m5: pd.DataFrame,
    current_price: float,
) -> Optional[AmendOrder]:
    """Return an :py:class:`AmendOrder` if an SL change is warranted.

    Branch order:

    1. **BE move**: if ``not position.be_moved`` and
       ``current_pnl_r >= profile.be_trigger_r``, return the BE amend.
       The new SL is ``entry ± profile.be_buffer_pips`` (in the trade's
       favour). The buffer protects against spread-oscillation
       triggering at the BE bar.
    2. **Trail**: if ``position.trail_active`` (set by the BE move),
       compute both candidate trail levels per
       ``(profile.trail_primary, profile.trail_secondary)``, pick the
       conservative one (closer to current price), and return an
       amend only if the move improves on ``current_sl_price`` by
       more than ``profile.trail_min_delta_pips``.

    Profile lookup is fail-loud — an unreachable
    ``(strategy_name, day_type_at_entry)`` raises ``KeyError``.
    """
    profile = profile_for(position.strategy_name, position.day_type_at_entry)
    if not position.be_moved:
        return _try_be_move(position, current_price, profile)
    if not position.trail_active:
        return None
    return _try_trail(position, df_m5, current_price, profile)


# ---------------------------------------------------------------------------
# BE move
# ---------------------------------------------------------------------------


def _try_be_move(
    position: ExecutionPosition,
    current_price: float,
    profile: ManagementProfile,
) -> Optional[AmendOrder]:
    pnl_r = position.current_pnl_r(current_price)
    if pnl_r < profile.be_trigger_r:
        return None
    buffer_price = pip_to_price(position.pair, profile.be_buffer_pips)
    if position.direction == Direction.BULLISH:
        new_sl = position.entry_price + buffer_price
    else:
        new_sl = position.entry_price - buffer_price

    # Defensive: never widen on the BE move either. A BE amend that
    # ends up worse than the current SL means the trade somehow ran
    # past the BE trigger while the existing SL is already tighter —
    # keep the tighter SL.
    if not _improves(position.direction, new_sl, position.current_sl_price):
        return None
    return AmendOrder(
        deal_id=position.deal_id,
        new_sl_price=new_sl,
        reason="be_move_at_1r",
    )


# ---------------------------------------------------------------------------
# Trail
# ---------------------------------------------------------------------------


def _try_trail(
    position: ExecutionPosition,
    df_m5: pd.DataFrame,
    current_price: float,
    profile: ManagementProfile,
) -> Optional[AmendOrder]:
    if len(df_m5) == 0:
        return None
    primary_kind = profile.trail_primary
    secondary_kind = profile.trail_secondary
    if primary_kind == "none" and secondary_kind == "none":
        return None

    primary = (
        _candidate(primary_kind, position.direction, df_m5, current_price)
        if primary_kind != "none"
        else None
    )
    secondary = (
        _candidate(secondary_kind, position.direction, df_m5, current_price)
        if secondary_kind != "none"
        else None
    )

    chosen, chosen_reason = _pick_conservative(
        position=position,
        primary=primary,
        primary_kind=primary_kind,
        secondary=secondary,
        secondary_kind=secondary_kind,
    )
    if chosen is None or chosen_reason is None:
        return None

    # Compare in pip units rounded to a sub-pip tolerance so FP noise
    # in subtractions of 4-decimal quote prices (e.g.
    # ``1.30020 - 1.30010 == 9.999...e-05``) doesn't spuriously fail
    # the "≥ 1 pip" threshold. 1e-3 pip = a tenth of a thousandth of
    # a pip, smaller than any quoted increment.
    delta_pips = price_to_pips(
        position.pair, abs(chosen - position.current_sl_price)
    )
    if delta_pips + 1e-3 < profile.trail_min_delta_pips:
        return None
    if not _improves(position.direction, chosen, position.current_sl_price):
        return None

    return AmendOrder(
        deal_id=position.deal_id,
        new_sl_price=chosen,
        reason=chosen_reason,  # type: ignore[arg-type]
    )


def _candidate(
    kind: str,
    direction: Direction,
    df_m5: pd.DataFrame,
    current_price: float,
) -> Optional[float]:
    """Compute a single trail candidate price (``None`` if unavailable)."""
    if kind == "ema20":
        ema20 = _safe_last(df_m5, "ema_20")
        if math.isnan(ema20):
            return None
        # Wrong-side rejection: never propose an SL above current price
        # on a long (or below on a short).
        if direction == Direction.BULLISH and ema20 >= current_price:
            return None
        if direction == Direction.BEARISH and ema20 <= current_price:
            return None
        return float(ema20)
    if kind == "swing":
        structure = get_structure_state(df_m5)
        if direction == Direction.BULLISH:
            swing = structure.get("last_swing_low")
            if swing is None or swing >= current_price:
                return None
            return float(swing)
        swing = structure.get("last_swing_high")
        if swing is None or swing <= current_price:
            return None
        return float(swing)
    return None  # unknown kind


def _pick_conservative(
    *,
    position: ExecutionPosition,
    primary: Optional[float],
    primary_kind: str,
    secondary: Optional[float],
    secondary_kind: str,
) -> tuple[Optional[float], Optional[str]]:
    """Pick the more conservative of primary / secondary, with reason.

    Conservative = closer to current price (higher for LONG, lower
    for SHORT). The reason string encodes which candidate was chosen
    so the audit log shows whether the primary or secondary applied.
    """
    candidates: list[tuple[float, str]] = []
    if primary is not None:
        candidates.append(
            (primary, f"trail_{primary_kind}_primary")  # e.g. trail_swing_primary
        )
    if secondary is not None:
        candidates.append(
            (secondary, f"trail_{secondary_kind}_secondary")
        )
    if not candidates:
        return None, None

    if position.direction == Direction.BULLISH:
        best = max(candidates, key=lambda c: c[0])
    else:
        best = min(candidates, key=lambda c: c[0])
    # Guard: don't return a candidate that's already worse than current SL.
    if not _improves(position.direction, best[0], position.current_sl_price):
        return None, None
    return best[0], best[1]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _improves(
    direction: Direction, candidate: float, current_sl: float
) -> bool:
    """``True`` iff moving SL from ``current_sl`` to ``candidate`` tightens it.

    LONG: tighter = higher. SHORT: tighter = lower. Equal returns False
    (no amend needed).
    """
    if direction == Direction.BULLISH:
        return candidate > current_sl
    return candidate < current_sl


def _safe_last(df: pd.DataFrame, column: str) -> float:
    if column not in df.columns or len(df) == 0:
        return float("nan")
    value = df.iloc[-1][column]
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


# ---------------------------------------------------------------------------
# Suppress unused-import nags
# ---------------------------------------------------------------------------

_ = (MIN_SL_PIPS, pip_size_for, datetime, Any)


__all__ = ["evaluate_sl_amend"]
