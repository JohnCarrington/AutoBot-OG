"""End-of-day enforcement (§6.5).

Two distinct responsibilities, both DST-aware via :mod:`zoneinfo`:

- :py:func:`check_pre_eod_suppression` — reject new entries within
  :data:`risk.constants.PRE_EOD_NO_ENTRY_MIN` minutes of NY close.
  Applies to RANGE / VOLATILE candidates every day; applies to ALL
  candidates on Friday (because every position closes Friday).
- :py:func:`apply_eod_force_close` — at NY close, return
  :py:class:`ForceCloseOrder` records for every position that should
  be flat overnight. The asymmetry:
    * RANGE / VOLATILE: always close at NY close.
    * TREND: close UNLESS (Mon-Thu) AND (current_pnl_r >= +1R) AND
      (regime still TREND, same direction as entry).
    * All regimes close on Fridays at NY close.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from regime.labels import Direction, RegimeLabel

from ..constants import (
    NY_CLOSE_HOUR_LOCAL,
    NY_TZ_NAME,
    PRE_EOD_NO_ENTRY_MIN,
    TREND_OVERNIGHT_HOLD_MIN_R,
)
from ..types import (
    CandidateTrade,
    ForceCloseOrder,
    OpenPosition,
    RuleResult,
)


_RULE_NAME = "eod_enforcement"
# ``weekday()`` returns 0 for Monday, 4 for Friday.
_FRIDAY = 4


def _ny_now(now_utc: datetime) -> datetime:
    return now_utc.astimezone(ZoneInfo(NY_TZ_NAME))


def _next_ny_close(now_utc: datetime) -> datetime:
    """Return the next ``NY_CLOSE_HOUR_LOCAL``:00 NY-time as a UTC datetime."""
    ny = _ny_now(now_utc)
    close_today_ny = ny.replace(
        hour=NY_CLOSE_HOUR_LOCAL, minute=0, second=0, microsecond=0
    )
    if ny >= close_today_ny:
        close_today_ny = close_today_ny + timedelta(days=1)
    return close_today_ny.astimezone(ZoneInfo("UTC"))


def check_pre_eod_suppression(
    candidate: CandidateTrade,
    now_utc: datetime,
) -> RuleResult:
    """Reject if a new entry would have less than the EOD buffer to live.

    For non-TREND regimes (RANGE / VOLATILE / TRANSITION): reject within
    ``PRE_EOD_NO_ENTRY_MIN`` minutes of any NY close.
    For TREND: only reject within the buffer on Fridays (TREND can
    legitimately hold overnight Mon-Thu, but Friday closes everything).

    The rule does not gate behaviour outside the buffer — that is the
    pre-existing strategy + risk pipeline's job.
    """
    next_close = _next_ny_close(now_utc)
    minutes_to_close = (next_close - now_utc).total_seconds() / 60.0
    if minutes_to_close > PRE_EOD_NO_ENTRY_MIN:
        return RuleResult(allow=True, rule=_RULE_NAME, reason="ok")

    # Inside the buffer. TREND can survive overnight on Mon-Thu, so only
    # reject TREND inside the Friday buffer.
    ny_weekday = _ny_now(now_utc).weekday()
    if candidate.intended_regime == RegimeLabel.TREND and ny_weekday != _FRIDAY:
        return RuleResult(allow=True, rule=_RULE_NAME, reason="ok")

    return RuleResult(
        allow=False,
        rule=_RULE_NAME,
        reason=(
            f"pre_eod_suppression: {minutes_to_close:.1f}min to NY close "
            f"(buffer={PRE_EOD_NO_ENTRY_MIN}min); "
            f"regime={candidate.intended_regime.value}, "
            f"weekday={ny_weekday}"
        ),
    )


def apply_eod_force_close(
    positions: list[OpenPosition],
    now_utc: datetime,
    *,
    current_regime: RegimeLabel,
    current_direction: Optional[Direction],
    pending_regime: Optional[RegimeLabel],
    pending_direction: Optional[Direction],
) -> list[ForceCloseOrder]:
    """Return force-close orders for positions that must be flat overnight.

    Caller invokes this at (or shortly after) NY close. The function is
    idempotent on a given second — it just describes what *should* be
    closed; placing the order is the caller's job.

    A position survives overnight (no order returned) iff ALL of:
    - It is a TREND-regime position.
    - Today is Mon, Tue, Wed, or Thu (in NY local time).
    - ``current_pnl_r >= TREND_OVERNIGHT_HOLD_MIN_R`` (default +1R).
    - The engine's current committed regime is still TREND AND its
      direction matches the position's entry direction.
    - **H3 fix (review 2026-05-14):** no contradicting pending
      transition is staged. ``pending_regime`` must be either
      ``None`` (engine settled on the committed TREND), or
      ``TREND`` with ``pending_direction`` matching the position's
      entry direction (an in-flight reconfirmation of the same TREND
      is benign). Any other pending — RANGE, VOLATILE, or
      opposite-direction TREND — force-closes the position.

    ``pending_regime`` and ``pending_direction`` are **required**
    keyword-only arguments (N1 follow-up from the 2026-05-14 Session
    3 review). Defaults are deliberately omitted so a caller that
    forgets to thread the engine's pending state through fails loudly
    at the call site rather than silently disabling the H3 gate.

    Everything else is force-closed at NY close.
    """
    if not positions:
        return []

    ny = _ny_now(now_utc)
    is_friday = ny.weekday() == _FRIDAY
    is_at_or_after_close = ny.time() >= time(NY_CLOSE_HOUR_LOCAL, 0)
    if not is_at_or_after_close:
        return []

    orders: list[ForceCloseOrder] = []
    for pos in positions:
        # Non-trend regimes always close at NY close.
        if pos.regime_at_entry != RegimeLabel.TREND:
            orders.append(
                ForceCloseOrder(
                    position_id=pos.position_id,
                    pair=pos.pair,
                    reason=f"eod_close: regime={pos.regime_at_entry.value}",
                )
            )
            continue

        # Friday closes everything.
        if is_friday:
            orders.append(
                ForceCloseOrder(
                    position_id=pos.position_id,
                    pair=pos.pair,
                    reason="eod_close: friday_close",
                )
            )
            continue

        # TREND overnight-hold gates: profit AND regime-still-aligned.
        if pos.current_pnl_r < TREND_OVERNIGHT_HOLD_MIN_R:
            # H4 (review 2026-05-14): when the entry was inside the
            # pre-EOD buffer, the trade had no realistic path to
            # reach +1R before close — surface that in the reason so
            # post-mortems can identify the wasted-entry scenario.
            held_seconds = (now_utc - pos.entry_time_utc).total_seconds()
            held_min = held_seconds / 60.0
            inside_buffer = 0 <= held_min < PRE_EOD_NO_ENTRY_MIN
            buffer_note = (
                f" (entry was {held_min:.1f}min before close, "
                f"inside {PRE_EOD_NO_ENTRY_MIN}min buffer — "
                f"no path to {TREND_OVERNIGHT_HOLD_MIN_R}R)"
                if inside_buffer
                else ""
            )
            orders.append(
                ForceCloseOrder(
                    position_id=pos.position_id,
                    pair=pos.pair,
                    reason=(
                        f"eod_close: trend_below_overnight_R "
                        f"(pnl_r={pos.current_pnl_r:.2f} "
                        f"< {TREND_OVERNIGHT_HOLD_MIN_R})"
                        f"{buffer_note}"
                    ),
                )
            )
            continue

        if current_regime != RegimeLabel.TREND:
            orders.append(
                ForceCloseOrder(
                    position_id=pos.position_id,
                    pair=pos.pair,
                    reason=(
                        f"eod_close: trend_regime_lost "
                        f"(current={current_regime.value})"
                    ),
                )
            )
            continue

        if current_direction != pos.direction:
            orders.append(
                ForceCloseOrder(
                    position_id=pos.position_id,
                    pair=pos.pair,
                    reason=(
                        f"eod_close: trend_direction_changed "
                        f"(entry={pos.direction.value}, "
                        f"current={current_direction.value if current_direction else 'None'})"
                    ),
                )
            )
            continue

        # H3 (review 2026-05-14): if the engine has an in-flight
        # pending transition staged, only let TREND survive overnight
        # when the pending is another TREND in the same direction —
        # i.e. a benign reconfirmation. RANGE / VOLATILE / opposite-
        # direction pendings indicate the regime is actively
        # transitioning away from the committed TREND, so force-close
        # rather than ride the position through the change overnight.
        if pending_regime is not None and not (
            pending_regime == RegimeLabel.TREND
            and pending_direction == pos.direction
        ):
            pending_dir_str = (
                pending_direction.value
                if pending_direction is not None
                else "None"
            )
            orders.append(
                ForceCloseOrder(
                    position_id=pos.position_id,
                    pair=pos.pair,
                    reason=(
                        f"eod_close: trend_pending_transition "
                        f"(pending={pending_regime.value}, "
                        f"pending_dir={pending_dir_str}, "
                        f"entry_dir={pos.direction.value})"
                    ),
                )
            )
            continue

        # Survives overnight.

    return orders


__all__ = [
    "apply_eod_force_close",
    "check_pre_eod_suppression",
]
