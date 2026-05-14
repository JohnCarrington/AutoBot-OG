"""Phase 6 execution layer.

Owns: broker-side trade open, SL amendments (BE at +1R + structure /
EMA20 trail), in-memory position tracking with JSON persistence,
broker-vs-local reconciliation.

Does NOT own: regime / strategy / risk decisions (`regime/`,
`strategies/`, `risk/`); raw IG REST protocol (`feed/ig_rest/`);
alert forwarding (`alerts/`, Phase 7).

Public surface
--------------
- :py:class:`Executor` — Signal → broker open; amend amends.
- :py:class:`PositionManager` — in-memory + persisted position store.
- :py:func:`evaluate_sl_amend` — pure SL-management decision.
- :py:func:`reconcile` — broker-vs-local divergence pass.
- Types: :py:class:`ExecutionPosition`, :py:class:`AmendOrder`,
  :py:class:`AmendResult`, :py:class:`TradeOrder`,
  :py:class:`TradeResult`, :py:class:`SLAmendment`,
  :py:class:`ReconciliationEvent`, :py:class:`ReconciliationReport`,
  :py:class:`ReconciliationActions`, :py:class:`ReconciliationOutcome`.

See ``docs/v1_architecture.md`` §6.3.1 (locked SL decisions) and
§6.11 (Phase 6 module structure).
"""
from .executor import Executor
from .position_manager import PositionManager
from .reconciliation import (
    ReconciliationActions,
    ReconciliationOutcome,
    reconcile,
)
from .sl_management import evaluate_sl_amend
from .state.positions_state import PositionsState
from .types import (
    AmendOrder,
    AmendResult,
    ExecutionPosition,
    ReconciliationEvent,
    ReconciliationKind,
    ReconciliationReport,
    ReconciliationSeverity,
    SLAmendment,
    TradeOrder,
    TradeResult,
)

__all__ = [
    "AmendOrder",
    "AmendResult",
    "ExecutionPosition",
    "Executor",
    "PositionManager",
    "PositionsState",
    "ReconciliationActions",
    "ReconciliationEvent",
    "ReconciliationKind",
    "ReconciliationOutcome",
    "ReconciliationReport",
    "ReconciliationSeverity",
    "SLAmendment",
    "TradeOrder",
    "TradeResult",
    "evaluate_sl_amend",
    "reconcile",
]
