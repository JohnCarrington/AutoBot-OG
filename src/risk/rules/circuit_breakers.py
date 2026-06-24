"""Circuit-breaker rule (§6.9).

Two independent breakers, each checked in order. The first that
trips short-circuits with ``allow=False``. Order:

1. **Daily drawdown** (§6.9.1): realised + unrealised PnL across all
   open positions for the current NY session ≤ ``DAILY_DD_LIMIT_R``.
2. **Consecutive loss cooldown** (§6.9.2): tripped when
   ``CONSECUTIVE_LOSS_THRESHOLD`` losing trades have closed back-to-
   back. State persists across restarts.

2c (B-2): the third "regime instability" breaker was deleted. The
day-type spine doesn't have a per-bar instability concept the way the
regime engine did, and the surviving daily-DD + consecutive-loss
breakers cover the two failure modes operators actually care about.

The rule mutates :py:class:`risk.state.CircuitBreakerState` in place
(marking it dirty when fields change); the :py:class:`risk.guard.
RiskGuard` orchestrator persists after the rule returns.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..constants import (
    CONSECUTIVE_LOSS_COOLDOWN_HOURS,
    DAILY_DD_LIMIT_R,
)
from ..state.circuit_breaker_state import (
    CircuitBreakerState,
    current_session_date_ny,
)
from ..types import AccountState, CandidateTrade, OpenPosition, RuleResult


_RULE_NAME = "circuit_breakers"


def check_circuit_breakers(
    *,
    candidate: CandidateTrade,  # noqa: ARG001 — accepted for symmetry with other rules
    positions: list[OpenPosition],
    account: AccountState,
    state: CircuitBreakerState,
    now_utc: datetime,
) -> RuleResult:
    """Run all breakers in order, short-circuiting on first trip.

    Has side effects: rolls daily DD state at NY-session boundary, sets
    cooldown timestamps when breakers trip, and clears them when they
    elapse. Persistence is the caller's responsibility — the rule only
    sets ``state._dirty`` via :py:meth:`CircuitBreakerState.mark_dirty`.
    """
    # --- Session rollover -----------------------------------------------------
    state.reset_daily_dd_if_new_session(now_utc)

    # --- Breaker 1: daily drawdown -------------------------------------------
    if state.daily_dd_cooldown_until_utc is not None:
        if now_utc < state.daily_dd_cooldown_until_utc:
            return RuleResult(
                allow=False,
                rule=_RULE_NAME,
                reason=(
                    f"daily_dd_cooldown active until "
                    f"{state.daily_dd_cooldown_until_utc.isoformat()}"
                ),
            )
        # Cooldown elapsed mid-session (unusual but possible) — clear.
        state.daily_dd_cooldown_until_utc = None
        state.mark_dirty()

    unrealised_r = sum(p.current_pnl_r for p in positions)
    total_r = account.realized_pnl_today_r + unrealised_r
    if total_r <= DAILY_DD_LIMIT_R:
        # Trigger DD: cooldown until end of current session (= start of
        # next session in our labelling scheme).
        from zoneinfo import ZoneInfo

        from ..constants import NY_CLOSE_HOUR_LOCAL, NY_TZ_NAME

        ny = ZoneInfo(NY_TZ_NAME)
        now_ny = now_utc.astimezone(ny)
        if now_ny.time().hour >= NY_CLOSE_HOUR_LOCAL:
            close_today_ny = (now_ny + timedelta(days=1)).replace(
                hour=NY_CLOSE_HOUR_LOCAL, minute=0, second=0, microsecond=0
            )
        else:
            close_today_ny = now_ny.replace(
                hour=NY_CLOSE_HOUR_LOCAL, minute=0, second=0, microsecond=0
            )
        state.daily_dd_cooldown_until_utc = close_today_ny.astimezone(
            ZoneInfo("UTC")
        )
        state.mark_dirty()
        return RuleResult(
            allow=False,
            rule=_RULE_NAME,
            reason=(
                f"daily_dd_triggered: total_R={total_r:.2f} "
                f"<= {DAILY_DD_LIMIT_R} "
                f"(realised={account.realized_pnl_today_r:.2f}, "
                f"unrealised={unrealised_r:.2f})"
            ),
        )

    # --- Breaker 2: consecutive-loss cooldown --------------------------------
    if state.consecutive_loss_cooldown_until_utc is not None:
        if now_utc < state.consecutive_loss_cooldown_until_utc:
            return RuleResult(
                allow=False,
                rule=_RULE_NAME,
                reason=(
                    f"consecutive_loss_cooldown active until "
                    f"{state.consecutive_loss_cooldown_until_utc.isoformat()} "
                    f"(streak={state.loss_streak})"
                ),
            )
        # Cooldown elapsed — clear.
        state.consecutive_loss_cooldown_until_utc = None
        state.mark_dirty()

    return RuleResult(allow=True, rule=_RULE_NAME, reason="ok")


def record_trade_outcome(
    state: CircuitBreakerState,
    pnl_r: float,
    closed_at_utc: datetime,
) -> None:
    """Update loss-streak state after a trade closes.

    Idempotent only at the rule's own contract — call once per closed
    trade. PnL ``> 0`` resets the streak; PnL ``<= 0`` increments and,
    on reaching :data:`risk.constants.CONSECUTIVE_LOSS_THRESHOLD`,
    arms the cooldown.
    """
    from ..constants import CONSECUTIVE_LOSS_THRESHOLD

    if pnl_r > 0:
        if state.loss_streak != 0:
            state.loss_streak = 0
            state.mark_dirty()
        return

    state.loss_streak += 1
    state.mark_dirty()
    if state.loss_streak >= CONSECUTIVE_LOSS_THRESHOLD:
        state.consecutive_loss_cooldown_until_utc = (
            closed_at_utc
            + timedelta(hours=CONSECUTIVE_LOSS_COOLDOWN_HOURS)
        )


__all__ = ["check_circuit_breakers", "record_trade_outcome"]
