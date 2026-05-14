"""Risk-layer public surface (Phase 4).

The ``RiskGuard`` class is the single entry point for strategy code: it
gates every prospective trade through the rule pipeline documented in
``docs/v1_architecture.md`` §6.10 (circuit_breakers → position_caps →
news_blackout → spread_filter → eod_enforcement) and emits EOD
force-close orders.

Sub-packages
------------
``news_calendar/``
    Finnhub-backed economic calendar (Phase 4-A) — already exported.
``rules/``
    One module per rule. Each rule is a pure function returning a
    :py:class:`RuleResult`. The orchestrator imports from each module
    directly so import-time failures localise.
``state/``
    Persisted state — only :py:class:`CircuitBreakerState` in v1.

Public surface
--------------
Constructors / orchestrator:
    RiskGuard, CircuitBreakerState

Types:
    OpenPosition, AccountState, CandidateTrade, MarketSnapshot,
    RuleResult, RiskDecision, ForceCloseOrder

Rules (importable directly for advanced consumers / tests):
    check_circuit_breakers, check_position_caps, check_news_blackout,
    check_spread_filter, check_pre_eod_suppression, apply_eod_force_close

Trade-outcome bookkeeping:
    record_trade_outcome

See also: :py:mod:`risk.news_calendar` for the calendar surface.
"""
from .guard import RiskGuard
from .rules.circuit_breakers import (
    check_circuit_breakers,
    record_trade_outcome,
)
from .rules.eod_enforcement import (
    apply_eod_force_close,
    check_pre_eod_suppression,
)
from .rules.news_blackout import check_news_blackout
from .rules.position_caps import check_position_caps
from .rules.spread_filter import check_spread_filter
from .state.circuit_breaker_state import CircuitBreakerState
from .types import (
    AccountState,
    CandidateTrade,
    ForceCloseOrder,
    MarketSnapshot,
    OpenPosition,
    RiskDecision,
    RuleResult,
)

__all__ = [
    "AccountState",
    "CandidateTrade",
    "CircuitBreakerState",
    "ForceCloseOrder",
    "MarketSnapshot",
    "OpenPosition",
    "RiskDecision",
    "RiskGuard",
    "RuleResult",
    "apply_eod_force_close",
    "check_circuit_breakers",
    "check_news_blackout",
    "check_position_caps",
    "check_pre_eod_suppression",
    "check_spread_filter",
    "record_trade_outcome",
]
