"""Broker ↔ local-state reconciliation.

Compares the broker's view of open positions against
:py:class:`PositionManager`'s persisted state and emits a
:py:class:`ReconciliationReport` describing every divergence.

Per ``docs/v1_architecture.md`` §6.11 the rules are deliberately
**conservative**:

- We **never auto-import** orphan broker positions. An open position
  on IG that has no local record requires operator confirmation.
- We **never auto-delete** local positions that aren't in the broker
  payload **unless** a close confirmation exists in the deal log.
- We **do** silently update local SL when the broker reports a
  different value — broker is authoritative for SL state — but
  if the drift is large (> :data:`EXECUTION_SL_DRIFT_WARN_PIPS`)
  we escalate to WARNING.

Manual broker actions (web-UI close, manual SL move) are
**normal-path** observations. The reconciliation log
(``data/execution/reconciliation_events.jsonl``) records them as
INFO / WARNING by materiality so the Phase 7 alerts module can
forward them.

The engine is a pure function of inputs — it does **not** mutate the
position manager. The caller (Phase 7 loop) applies the report's
actions explicitly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from config.pair_config import pair_from_epic, pip_size_for, price_to_pips
from feed.ig_rest import BrokerPosition

from .constants import (
    EXECUTION_BROKER_ORPHAN_ALERT,
    EXECUTION_SL_DRIFT_WARN_PIPS,
    EXECUTION_STALE_POSITION_HOURS,
)
from .position_manager import PositionManager
from .types import (
    ExecutionPosition,
    ReconciliationEvent,
    ReconciliationKind,
    ReconciliationReport,
    ReconciliationSeverity,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Actions returned alongside events (caller applies them)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconciliationActions:
    """Mutations the caller should apply after acknowledging events.

    - ``apply_sl_updates`` — ``deal_id → broker_sl_price`` pairs the
      caller should write through to the manager.
    - ``remove_deal_ids`` — positions confirmed closed; remove from
      the manager (events log captures the trade outcome).
    """

    apply_sl_updates: dict[str, float] = field(default_factory=dict)
    remove_deal_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconciliationOutcome:
    """Bundle of report + caller-action plan."""

    report: ReconciliationReport
    actions: ReconciliationActions


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def reconcile(
    *,
    manager: PositionManager,
    broker_positions: Iterable[BrokerPosition],
    deal_confirmations_log: Optional[dict[str, dict]] = None,
    now_utc: Optional[datetime] = None,
) -> ReconciliationOutcome:
    """Compare broker state against local state, return events + actions.

    Parameters
    ----------
    manager
        Local :py:class:`PositionManager`. Read-only — the function
        does **not** mutate it; the caller applies
        :py:class:`ReconciliationActions`.
    broker_positions
        Iterable of :py:class:`BrokerPosition` (typically from
        :py:meth:`IGClient.fetch_open_positions`).
    deal_confirmations_log
        Optional ``deal_id -> raw_confirmation_dict`` lookup of
        recently-closed deals. When a local position is missing from
        the broker payload AND has a confirmation in this map, the
        engine treats the position as manually closed (INFO event,
        scheduled removal). Without the log, missing positions stay
        in local state with an ALERT event.
    now_utc
        Override the wall-clock (tests).
    """
    log = deal_confirmations_log or {}
    now = now_utc or datetime.now(tz=timezone.utc)

    broker_list = list(broker_positions)
    broker_by_id = {p.deal_id: p for p in broker_list if p.deal_id}
    local_positions = manager.all()
    local_by_id = {p.deal_id: p for p in local_positions if p.deal_id}

    events: list[ReconciliationEvent] = []
    sl_updates: dict[str, float] = {}
    removals: list[str] = []

    # --- For each local position, classify against broker payload. ---------
    for deal_id, local in local_by_id.items():
        broker = broker_by_id.get(deal_id)

        if broker is None:
            outcome = _handle_missing_broker(local, log, now)
            events.append(outcome)
            if outcome.kind == ReconciliationKind.POSITION_CLOSED:
                removals.append(deal_id)
            continue

        # Stale check (informational; not blocking).
        age = now - local.entry_time_utc
        if age > timedelta(hours=EXECUTION_STALE_POSITION_HOURS):
            events.append(
                ReconciliationEvent(
                    at_utc=now,
                    severity=ReconciliationSeverity.WARNING,
                    kind=ReconciliationKind.STALE_POSITION,
                    deal_id=deal_id,
                    pair=local.pair,
                    message=(
                        f"local position open for "
                        f"{age.total_seconds() / 3600:.1f}h "
                        f"(threshold={EXECUTION_STALE_POSITION_HOURS}h); "
                        f"investigate."
                    ),
                    debug={"age_hours": age.total_seconds() / 3600},
                )
            )

        sl_event = _handle_sl(local, broker, now)
        if sl_event is not None:
            events.append(sl_event)
            if (
                sl_event.kind
                in (
                    ReconciliationKind.SL_UPDATED_FROM_BROKER,
                    ReconciliationKind.MANUAL_SL_MOVE,
                    ReconciliationKind.SL_DRIFT_LARGE,
                )
                and broker.stop_level is not None
            ):
                sl_updates[deal_id] = broker.stop_level

    # --- For each broker position, flag orphans. ---------------------------
    for deal_id, broker in broker_by_id.items():
        if deal_id in local_by_id:
            continue
        severity = (
            ReconciliationSeverity.ALERT
            if EXECUTION_BROKER_ORPHAN_ALERT
            else ReconciliationSeverity.WARNING
        )
        # M1 (review 2026-05-14): every other branch sets ``pair`` to
        # the pair *symbol* (``"GBPUSD"``) — only the orphan branch
        # previously used the raw IG ``epic`` (``"CS.D.GBPUSD.TODAY.IP"``).
        # Phase 7's alerts module groups events by ``pair``; mixed
        # symbol/epic values would silently miss orphan rows in any
        # per-pair summary. Normalise to the symbol here and keep the
        # raw epic in ``debug``.
        resolved_pair = pair_from_epic(broker.epic)
        events.append(
            ReconciliationEvent(
                at_utc=now,
                severity=severity,
                kind=ReconciliationKind.BROKER_ORPHAN,
                deal_id=deal_id,
                pair=resolved_pair,
                message=(
                    f"broker reports open position {deal_id} ({broker.epic}, "
                    f"{broker.direction}, size={broker.size}) that has no "
                    f"local state; manual confirmation required before "
                    f"the bot will manage it."
                ),
                debug={
                    "broker_epic": broker.epic,
                    "broker_open_level": broker.open_level,
                    "broker_stop_level": broker.stop_level,
                    "broker_direction": broker.direction,
                },
            )
        )

    if not events:
        events.append(
            ReconciliationEvent(
                at_utc=now,
                severity=ReconciliationSeverity.INFO,
                kind=ReconciliationKind.OK_NO_OP,
                deal_id=None,
                pair=None,
                message="no divergences",
            )
        )

    return ReconciliationOutcome(
        report=ReconciliationReport(at_utc=now, events=tuple(events)),
        actions=ReconciliationActions(
            apply_sl_updates=sl_updates,
            remove_deal_ids=tuple(removals),
        ),
    )


# ---------------------------------------------------------------------------
# Local-vs-broker handlers
# ---------------------------------------------------------------------------


def _handle_missing_broker(
    local: ExecutionPosition,
    deal_log: dict[str, dict],
    now: datetime,
) -> ReconciliationEvent:
    """Local position not in broker payload — close-confirmed or orphan."""
    confirmation = deal_log.get(local.deal_id)
    if confirmation:
        return ReconciliationEvent(
            at_utc=now,
            severity=ReconciliationSeverity.INFO,
            kind=ReconciliationKind.POSITION_CLOSED,
            deal_id=local.deal_id,
            pair=local.pair,
            message=(
                f"local position {local.deal_id} confirmed closed via deal "
                f"log; scheduling removal from local state."
            ),
            debug={"confirmation": confirmation},
        )
    return ReconciliationEvent(
        at_utc=now,
        severity=ReconciliationSeverity.ALERT,
        kind=ReconciliationKind.MISSING_LOCAL_KEPT,
        deal_id=local.deal_id,
        pair=local.pair,
        message=(
            f"local position {local.deal_id} on {local.pair} not reported "
            f"by broker and no close-confirmation found; KEEPING local "
            f"state, manual review required."
        ),
        debug={
            "local_entry_price": local.entry_price,
            "local_current_sl": local.current_sl_price,
        },
    )


def _handle_sl(
    local: ExecutionPosition,
    broker: BrokerPosition,
    now: datetime,
) -> Optional[ReconciliationEvent]:
    """Classify the broker-vs-local SL relationship."""
    if broker.stop_level is None:
        # IG sometimes returns no stop_level on partially-confirmed
        # records — treat as no-op until the next pass.
        return None

    local_sl = local.current_sl_price
    broker_sl = broker.stop_level
    if abs(broker_sl - local_sl) < pip_size_for(local.pair) * 0.5:
        return None  # within rounding noise; treat as equal

    drift_pips = abs(price_to_pips(local.pair, broker_sl - local_sl))
    is_manual = _is_manual_move(local, broker)
    if drift_pips > EXECUTION_SL_DRIFT_WARN_PIPS:
        severity = ReconciliationSeverity.WARNING
        kind = ReconciliationKind.SL_DRIFT_LARGE
    elif is_manual:
        severity = ReconciliationSeverity.WARNING
        kind = ReconciliationKind.MANUAL_SL_MOVE
    else:
        severity = ReconciliationSeverity.INFO
        kind = ReconciliationKind.SL_UPDATED_FROM_BROKER

    return ReconciliationEvent(
        at_utc=now,
        severity=severity,
        kind=kind,
        deal_id=local.deal_id,
        pair=local.pair,
        message=(
            f"SL drift on {local.deal_id}: local={local_sl}, "
            f"broker={broker_sl} ({drift_pips:.1f}p); adopting broker value."
        ),
        debug={
            "local_sl": local_sl,
            "broker_sl": broker_sl,
            "drift_pips": drift_pips,
        },
    )


def _is_manual_move(
    local: ExecutionPosition, broker: BrokerPosition
) -> bool:
    """Heuristic: SL move not recorded in local ``sl_history`` looks manual.

    If the broker's SL doesn't match any historical ``to_price`` in the
    position's amend log (or the live ``current_sl_price``), the
    position was probably modified outside the bot. Reconciliation
    flags this as a manual move so the Phase 7 alerts layer can
    forward it; the bot still adopts the broker's value to stay
    consistent.
    """
    if broker.stop_level is None:
        return False
    history_levels = {
        round(a.to_price, 5) for a in local.sl_history
    } | {round(local.current_sl_price, 5)}
    return round(broker.stop_level, 5) not in history_levels


__all__ = [
    "ReconciliationActions",
    "ReconciliationOutcome",
    "reconcile",
]
