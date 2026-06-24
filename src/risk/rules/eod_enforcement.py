"""End-of-day enforcement (§6.5).

Two distinct responsibilities, both DST-aware via :mod:`zoneinfo`:

- :py:func:`check_pre_eod_suppression` — reject new entries within
  :data:`risk.constants.PRE_EOD_NO_ENTRY_MIN` minutes of NY close.
  2c (B-1): the prior TREND/Mon-Thu carve-out is gone — every
  candidate inside the buffer is rejected regardless of day-type, on
  the grounds that the trade can't reach +1R before close and we can't
  predict whether structure will agree at EOD.
- :py:func:`apply_eod_force_close` — at NY close, return
  :py:class:`ForceCloseOrder` records for every position that should
  be flat overnight. 2c/2d (B-1): the carve-out for overnight hold
  keys on the structure engine's ``htf_bias`` matching the position's
  direction (HTF thesis intact). Any position whose htf_bias has
  flipped — or for which structure isn't available — force-closes.
  Fridays close everything. 2d strips the +1R floor that was added in
  2c but not part of the locked B-1 design — pnl level no longer
  affects the hold decision; only htf_bias alignment + weekday do.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Mapping, Optional
from zoneinfo import ZoneInfo

from common import Direction

from ..constants import (
    NY_CLOSE_HOUR_LOCAL,
    NY_TZ_NAME,
    PRE_EOD_NO_ENTRY_MIN,
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

    2c (B-1): no day-type carve-out. Every candidate inside the
    ``PRE_EOD_NO_ENTRY_MIN`` window before NY close is rejected.
    Rationale: the trade has no realistic path to +1R inside the
    buffer, and the overnight-hold decision is no longer a property
    of the candidate's day-type — it depends on the structure engine's
    ``htf_bias`` at EOD time, which we cannot predict at entry.

    Outside the buffer the rule allows everything; other rules in the
    pipeline gate behaviour upstream.
    """
    next_close = _next_ny_close(now_utc)
    minutes_to_close = (next_close - now_utc).total_seconds() / 60.0
    if minutes_to_close > PRE_EOD_NO_ENTRY_MIN:
        return RuleResult(allow=True, rule=_RULE_NAME, reason="ok")

    ny_weekday = _ny_now(now_utc).weekday()
    return RuleResult(
        allow=False,
        rule=_RULE_NAME,
        reason=(
            f"pre_eod_suppression: {minutes_to_close:.1f}min to NY close "
            f"(buffer={PRE_EOD_NO_ENTRY_MIN}min); "
            f"day_type={candidate.intended_day_type.value}, "
            f"weekday={ny_weekday}"
        ),
    )


def apply_eod_force_close(
    positions: list[OpenPosition],
    now_utc: datetime,
    *,
    htf_bias_for_pair: Mapping[str, Optional[str]],
) -> list[ForceCloseOrder]:
    """Return force-close orders for positions that must be flat overnight.

    Caller invokes this at (or shortly after) NY close. The function is
    idempotent on a given second — it just describes what *should* be
    closed; placing the order is the caller's job.

    A position survives overnight (no order returned) iff BOTH of:

    - Today is Mon, Tue, Wed, or Thu (in NY local time).
    - Structure ``htf_bias`` for the position's pair still matches the
      position's direction (BULLISH-position needs ``htf_bias="BULLISH"``;
      BEARISH needs ``htf_bias="BEARISH"``).

    Everything else force-closes at NY close. Friday closes everything
    unconditionally. 2d note: the +1R PnL gate was removed — the
    hold decision is purely structural now, in line with the B-1
    design ("HTF thesis intact ⇒ hold").

    Parameters
    ----------
    positions
        Open positions to evaluate.
    now_utc
        Current UTC instant. Used to derive NY local time + weekday.
    htf_bias_for_pair
        Map ``pair → htf_bias`` from the latest structure analysis. A
        missing entry (or ``None``) force-closes — fail-closed when
        structure data is unavailable.
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

        # htf_bias gate — the position survives only if structure HTF
        # bias still agrees with the position's direction.
        htf_bias = htf_bias_for_pair.get(pos.pair)
        if htf_bias is None:
            orders.append(
                ForceCloseOrder(
                    position_id=pos.position_id,
                    pair=pos.pair,
                    reason=(
                        "eod_close: structure_unavailable "
                        f"(pair={pos.pair} has no current htf_bias; "
                        "fail-closed)"
                    ),
                )
            )
            continue

        expected_bias = (
            "BULLISH" if pos.direction == Direction.BULLISH else "BEARISH"
        )
        if htf_bias != expected_bias:
            orders.append(
                ForceCloseOrder(
                    position_id=pos.position_id,
                    pair=pos.pair,
                    reason=(
                        f"eod_close: htf_bias_misaligned "
                        f"(entry_dir={pos.direction.value}, "
                        f"current_htf_bias={htf_bias})"
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
