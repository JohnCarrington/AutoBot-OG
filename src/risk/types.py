"""Shared types for the risk layer.

Frozen dataclasses + TypedDicts that the rules, the ``RiskGuard``
orchestrator, and downstream consumers (Phase 5 execution layer) all
speak. Defined once here so each rule module can stay focused on its
gating logic.

All time fields are timezone-aware ``datetime`` instances in UTC. Naive
datetimes are not accepted — the calendar and EOD-enforcement rules rely
on tz-aware arithmetic via ``zoneinfo``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from regime.labels import Direction, RegimeLabel


# ---------------------------------------------------------------------------
# Inputs the caller supplies to RiskGuard.allow_entry / positions_to_force_close
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenPosition:
    """Caller-supplied snapshot of a single open position.

    The risk layer never talks to the broker; the caller is responsible
    for keeping this list in sync with whatever the broker reports.

    Attributes
    ----------
    position_id : str
        Stable identifier; used in ``ForceCloseOrder.position_id``.
    pair : str
        Instrument symbol (e.g. ``"GBPUSD"``).
    direction : Direction
        ``BULLISH`` for a long position, ``BEARISH`` for short. Aligned
        with the regime ``Direction`` enum so the EOD "still TREND same
        direction" check is a direct equality test.
    regime_at_entry : RegimeLabel
        Which regime emitted the trade (drives EOD policy + duplicate-
        stacking checks).
    entry_price : float
        For diagnostics; not used by any rule in Phase 4.
    current_price : float
        For diagnostics; not used by any rule in Phase 4.
    entry_time_utc : datetime
        Used by potential time-in-trade caps; not used in Phase 4 v1.
    current_pnl_r : float
        Current unrealised PnL expressed in R-multiples of the initial
        stop distance. The caller computes this from
        ``(current_price − entry_price) / initial_stop_distance``. Used
        by the daily DD check and the TREND-overnight-hold gate.
    """

    position_id: str
    pair: str
    direction: Direction
    regime_at_entry: RegimeLabel
    entry_price: float
    current_price: float
    entry_time_utc: datetime
    current_pnl_r: float


@dataclass(frozen=True)
class AccountState:
    """Account-level snapshot at decision time.

    v1 risk rules consume only ``realized_pnl_today_r`` (combined with
    unrealised PnL from ``OpenPosition`` to drive the daily DD circuit
    breaker). ``balance`` and ``currency`` are carried for future hooks
    and diagnostic logs.
    """

    balance: float
    currency: str
    realized_pnl_today_r: float


@dataclass(frozen=True)
class CandidateTrade:
    """Trade the strategy layer is proposing to open."""

    pair: str
    intended_direction: Direction
    intended_regime: RegimeLabel
    planned_entry_price: float


@dataclass(frozen=True)
class MarketSnapshot:
    """Live-market inputs needed by the spread filter."""

    current_spread_pips: float
    atr_m5_pips: float


# ---------------------------------------------------------------------------
# Rule + decision outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleResult:
    """Uniform output shape from every rule.

    ``allow=True`` short-circuits onward; ``allow=False`` returns the
    decision to the caller with ``rule`` and ``reason`` populated for
    diagnostic logs.
    """

    allow: bool
    rule: str
    reason: str


@dataclass(frozen=True)
class RiskDecision:
    """Final decision returned by ``RiskGuard.allow_entry``.

    Carries the same shape as ``RuleResult`` plus a one-shot ``debug``
    dict for callers wanting to surface "why" in logs / Telegram
    alerts.
    """

    allow: bool
    rule: str
    reason: str
    debug: dict


@dataclass(frozen=True)
class ForceCloseOrder:
    """Instruction returned by ``RiskGuard.positions_to_force_close``.

    The caller is responsible for actually sending the close to the
    broker; this object just records the decision.
    """

    position_id: str
    pair: str
    reason: str


# ---------------------------------------------------------------------------
# Re-export Direction / RegimeLabel for callers that import from here
# ---------------------------------------------------------------------------

__all__ = [
    "AccountState",
    "CandidateTrade",
    "Direction",
    "ForceCloseOrder",
    "MarketSnapshot",
    "OpenPosition",
    "RegimeLabel",
    "RiskDecision",
    "RuleResult",
]


# Direction and RegimeLabel are imported above; keep references visible so
# linters do not flag the re-exports as unused.
_ = (Direction, Optional, RegimeLabel)
