"""Risk-layer entry-gate rules.

Each rule is a pure function returning a :py:class:`risk.types.RuleResult`.
The :py:class:`risk.guard.RiskGuard` orchestrator composes them in the
documented short-circuit order (see ``docs/v1_architecture.md`` §6.10):

    circuit_breakers → position_caps → news_blackout → spread_filter
    → pre_eod_suppression

Rules that need persistent state (only ``circuit_breakers`` in v1) take a
mutable :py:class:`risk.state.circuit_breaker_state.CircuitBreakerState`
parameter and update it in-place; the orchestrator persists the state
after the rule call.
"""
from .circuit_breakers import check_circuit_breakers, record_trade_outcome
from .eod_enforcement import (
    apply_eod_force_close,
    check_pre_eod_suppression,
)
from .news_blackout import check_news_blackout
from .position_caps import check_position_caps
from .spread_filter import check_spread_filter

__all__ = [
    "apply_eod_force_close",
    "check_circuit_breakers",
    "check_news_blackout",
    "check_position_caps",
    "check_pre_eod_suppression",
    "check_spread_filter",
    "record_trade_outcome",
]
