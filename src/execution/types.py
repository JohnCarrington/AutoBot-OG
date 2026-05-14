"""Shared types for the execution layer (Phase 6).

Frozen dataclasses + enums that the executor, position manager,
SL manager, and reconciliation engine all speak. Defined once here so
each component module can stay focused.

Time fields are timezone-aware ``datetime`` instances in UTC.
Price fields are in broker quote units; pip conversions go through
:py:func:`config.pair_config.pip_to_price` /
:py:func:`config.pair_config.price_to_pips`.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from regime.labels import Direction, RegimeLabel
from risk.types import OpenPosition as RiskOpenPosition
from strategies.signal import StrategyName


# ---------------------------------------------------------------------------
# SL audit trail
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SLAmendment:
    """One historical SL-amend event on an :py:class:`ExecutionPosition`.

    Stored as an immutable tuple of these on the position; the most
    recent entry is always the last element. Reasons are short
    machine-readable strings (``"be_move_at_1r"``, ``"trail_swing_primary"``,
    etc.) — the human-readable narrative belongs in the alerts layer.
    """

    at_utc: datetime
    from_price: float
    to_price: float
    reason: str
    deal_id_or_reference: str  # for cross-reference against the broker confirm


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionPosition:
    """Tracked broker position state owned by the execution layer.

    Distinct from :py:class:`risk.types.OpenPosition` (which is the
    *input* to risk-rule evaluation): an ``ExecutionPosition`` carries
    everything we need to manage the position through to close —
    deal-id, amend history, BE / trail flags, idempotency keys.

    The dataclass is frozen; every state transition produces a new
    instance via :py:meth:`with_changes` or :py:meth:`with_sl_amend`.
    Immutability keeps the position-manager state-transition logic
    auditable and makes restart safety obvious (saved JSON is the
    canonical state).

    Fields
    ------
    deal_id, deal_reference
        Broker primary keys. ``deal_id`` is set after the open
        confirmation lands; ``deal_reference`` is our submission
        nonce (kept as a fallback when the deal_id has not been
        observed yet).
    pair, direction, regime_at_entry, strategy_name
        Trade metadata captured at open time. ``regime_at_entry``
        drives EOD enforcement; ``strategy_name`` drives the
        SL-trail rule selection (BB Reclaim → EMA20 primary,
        TREND / VOLATILE → swing primary).
    size_units
        IG-side size (e.g. ``1.0`` spreadbet unit). Pips per unit
        depend on the pair via :py:func:`config.pair_config.get_ppp`.
    entry_price
        Confirmation-level fill price.
    initial_sl_price
        Stop price at open. Never mutated — used to compute R-multiples
        across the position's life so a restart cannot lose the
        reference.
    current_sl_price
        Stop price as currently held at the broker. Mutated by every
        successful amend.
    suggested_tp_price
        Fixed TP for BB Reclaim only; ``None`` for trend / sweep
        (trail managed by :py:mod:`execution.sl_management`).
    entry_time_utc
        Open timestamp (broker confirmation, not our submission).
    signal_source_candle_ts
        Idempotency key part — ``Signal.source_candle_ts`` at open
        time. Combined with ``(pair, strategy_name)`` to deduplicate
        repeated signals.
    be_moved, trail_active
        One-shot flag pair. ``be_moved`` flips True when the BE-move
        amend lands; ``trail_active`` follows in the same transition.
        Trail logic is **gated** on ``trail_active`` per the locked
        Phase 6 decision.
    sl_history
        Append-only tuple of :py:class:`SLAmendment` records.
    """

    deal_id: str
    deal_reference: str
    pair: str
    direction: Direction
    regime_at_entry: RegimeLabel
    strategy_name: StrategyName
    size_units: float
    entry_price: float
    initial_sl_price: float
    current_sl_price: float
    suggested_tp_price: Optional[float]
    entry_time_utc: datetime
    signal_source_candle_ts: datetime
    be_moved: bool = False
    trail_active: bool = False
    sl_history: tuple[SLAmendment, ...] = field(default_factory=tuple)

    # --- Derived helpers ----------------------------------------------------

    @property
    def initial_sl_distance(self) -> float:
        """Absolute price distance between entry and the *initial* SL."""
        return abs(self.entry_price - self.initial_sl_price)

    def current_pnl_r(self, current_price: float) -> float:
        """Unrealised PnL in R-multiples of the initial stop distance."""
        if self.initial_sl_distance <= 0:
            return 0.0
        if self.direction == Direction.BULLISH:
            move = current_price - self.entry_price
        else:
            move = self.entry_price - current_price
        return move / self.initial_sl_distance

    def to_risk_open_position(
        self, *, current_price: float
    ) -> RiskOpenPosition:
        """Adapter to the risk-layer view (used by EOD enforcement).

        The risk layer asks for ``current_pnl_r`` and a few other
        snapshot fields; we compute them on demand so a stale
        ``ExecutionPosition`` instance never feeds the risk layer.
        """
        return RiskOpenPosition(
            position_id=self.deal_id,
            pair=self.pair,
            direction=self.direction,
            regime_at_entry=self.regime_at_entry,
            entry_price=self.entry_price,
            current_price=current_price,
            entry_time_utc=self.entry_time_utc,
            current_pnl_r=self.current_pnl_r(current_price),
        )

    # --- Transitions --------------------------------------------------------

    def with_changes(self, **updates) -> "ExecutionPosition":
        """Return a new instance with ``updates`` applied."""
        return replace(self, **updates)

    def with_sl_amend(
        self,
        *,
        new_sl_price: float,
        at_utc: datetime,
        reason: str,
        deal_id_or_reference: str,
        be_moved: Optional[bool] = None,
        trail_active: Optional[bool] = None,
    ) -> "ExecutionPosition":
        """Return a new instance reflecting a successful SL amend.

        Appends an :py:class:`SLAmendment` record to ``sl_history``.
        ``be_moved`` / ``trail_active`` default to the current values
        unless explicitly overridden — the BE-move amend sets both to
        ``True``; subsequent trail amends leave them.
        """
        amend = SLAmendment(
            at_utc=at_utc,
            from_price=self.current_sl_price,
            to_price=new_sl_price,
            reason=reason,
            deal_id_or_reference=deal_id_or_reference,
        )
        return replace(
            self,
            current_sl_price=new_sl_price,
            sl_history=(*self.sl_history, amend),
            be_moved=self.be_moved if be_moved is None else be_moved,
            trail_active=self.trail_active if trail_active is None else trail_active,
        )


# ---------------------------------------------------------------------------
# Amend
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AmendOrder:
    """Instruction emitted by :py:class:`SLManager`.

    The execution layer translates this into a
    :py:class:`feed.ig_rest.AmendRequest` and forwards to the broker.
    """

    deal_id: str
    new_sl_price: float
    reason: Literal[
        "be_move_at_1r",
        "trail_swing_primary",
        "trail_ema20_primary",
        "trail_swing_secondary",
        "trail_ema20_secondary",
    ]


@dataclass(frozen=True)
class AmendResult:
    """Outcome of forwarding an :py:class:`AmendOrder` to the broker."""

    success: bool
    deal_id: str
    new_sl_price: float
    reason: str  # mirrors AmendOrder.reason on success; failure message otherwise
    broker_status: Optional[str] = None  # "ACCEPTED" | "REJECTED"


# ---------------------------------------------------------------------------
# Trade open
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeOrder:
    """Internal-facing open-order spec, built from a :py:class:`Signal`."""

    pair: str
    epic: str
    direction: Direction
    size_units: float
    entry_price: float  # for diagnostics only — actual fill comes from confirm
    sl_price: float
    tp_price: Optional[float]
    regime_at_entry: RegimeLabel
    strategy_name: StrategyName
    signal_source_candle_ts: datetime


@dataclass(frozen=True)
class TradeResult:
    """Outcome of :py:meth:`Executor.open_from_signal`.

    ``opened_position`` is populated on success; ``rejection_reason``
    on failure. The caller can branch on ``success`` without needing
    to inspect the underlying confirmation.
    """

    success: bool
    deal_id: Optional[str]
    deal_reference: str
    rejection_reason: Optional[str] = None
    opened_position: Optional[ExecutionPosition] = None


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


class ReconciliationSeverity(str, Enum):
    """Tiered severity for reconciliation events.

    INFO    — normal-path observation (manual close detected via deal log,
              manual SL move detected, etc.).
    WARNING — non-blocking divergence (broker SL drift > delta, stale local
              position).
    ALERT   — operator action required (orphaned broker position, missing
              local position with no close confirmation).
    """

    INFO = "INFO"
    WARNING = "WARNING"
    ALERT = "ALERT"


class ReconciliationKind(str, Enum):
    """Discriminator for what kind of divergence the event records."""

    OK_NO_OP = "OK_NO_OP"
    SL_UPDATED_FROM_BROKER = "SL_UPDATED_FROM_BROKER"
    POSITION_CLOSED = "POSITION_CLOSED"
    MISSING_LOCAL_KEPT = "MISSING_LOCAL_KEPT"
    BROKER_ORPHAN = "BROKER_ORPHAN"
    STALE_POSITION = "STALE_POSITION"
    SL_DRIFT_LARGE = "SL_DRIFT_LARGE"
    AMEND_FAILED = "AMEND_FAILED"
    MANUAL_SL_MOVE = "MANUAL_SL_MOVE"


@dataclass(frozen=True)
class ReconciliationEvent:
    """One observation from a reconciliation pass.

    Serialised line-by-line into
    ``data/execution/reconciliation_events.jsonl`` for the Phase 7
    alerts module to forward. ``deal_id`` is ``None`` only for
    ``BROKER_ORPHAN`` events that don't correspond to any local state.
    """

    at_utc: datetime
    severity: ReconciliationSeverity
    kind: ReconciliationKind
    deal_id: Optional[str]
    pair: Optional[str]
    message: str
    debug: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ReconciliationReport:
    """Bundle of events emitted by a single reconciliation pass."""

    at_utc: datetime
    events: tuple[ReconciliationEvent, ...]

    @property
    def alerts(self) -> tuple[ReconciliationEvent, ...]:
        return tuple(
            e for e in self.events if e.severity == ReconciliationSeverity.ALERT
        )


__all__ = [
    "AmendOrder",
    "AmendResult",
    "ExecutionPosition",
    "ReconciliationEvent",
    "ReconciliationKind",
    "ReconciliationReport",
    "ReconciliationSeverity",
    "SLAmendment",
    "TradeOrder",
    "TradeResult",
]
