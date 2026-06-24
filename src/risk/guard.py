"""RiskGuard — the public orchestrator for the risk layer.

A single instance per running bot. Holds:

- a :py:class:`risk.state.CircuitBreakerState` loaded from disk.

Three public methods:

- :py:meth:`allow_entry` runs the rule pipeline (circuit_breakers →
  position_caps → news_blackout → spread_filter → pre-EOD
  suppression). First rejection short-circuits.
- :py:meth:`positions_to_force_close` runs EOD enforcement and
  returns the list of close orders the caller should send. The caller
  passes a ``structure_state_for_pair`` lookup so the EOD rule can
  consult the current ``htf_bias`` per pair.
- :py:meth:`record_trade_outcome` updates the consecutive-loss state
  after a trade closes.

State is persisted lazily — only when a rule marks state dirty.

2c (B-2/B-3): the regime-instability breaker is gone, so the guard
no longer needs a ``RegimeEngine`` reference. The overnight-hold
carve-out is driven by structure ``htf_bias``, plumbed at call-time
into :py:meth:`positions_to_force_close`.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from structure_engine import StructureState

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
from .state.circuit_breaker_state import (
    DEFAULT_STATE_PATH,
    CircuitBreakerState,
)
from .types import (
    AccountState,
    CandidateTrade,
    ForceCloseOrder,
    MarketSnapshot,
    OpenPosition,
    RiskDecision,
    RuleResult,
)


class RiskGuard:
    """Composes risk rules into entry / EOD decisions."""

    def __init__(
        self,
        state: Optional[CircuitBreakerState] = None,
        *,
        state_path: Path | str | None = None,
    ) -> None:
        """Construct the guard.

        Parameters
        ----------
        state : CircuitBreakerState, optional
            Pre-loaded state object. Useful in tests. If omitted, state
            is loaded from ``state_path`` (or :data:`DEFAULT_STATE_PATH`).
        state_path : Path or str, optional
            Where to load/persist circuit-breaker state. Ignored if
            ``state`` is provided.
        """
        if state is not None:
            self.state = state
        else:
            path = (
                Path(state_path)
                if state_path is not None
                else DEFAULT_STATE_PATH
            )
            self.state = CircuitBreakerState.load(path)

    # --- Entry gate ----------------------------------------------------------

    def allow_entry(
        self,
        *,
        candidate: CandidateTrade,
        positions: list[OpenPosition],
        account: AccountState,
        market: MarketSnapshot,
        now_utc: datetime,
    ) -> RiskDecision:
        """Run the rule pipeline; return the first rejection or final allow.

        Pipeline order (cheapest-state-only first; live-market last):

        1. ``circuit_breakers`` — daily DD, consecutive-loss cooldown.
           May mutate state. Persisted on dirty.
        2. ``position_caps`` — global / per-pair / per-strategy caps.
        3. ``news_blackout`` — per-currency calendar lookup.
        4. ``spread_filter`` — live spread vs cap.
        5. ``pre_eod_suppression`` — buffer before NY close.
        """
        debug: dict = {
            "pipeline": [],
            "now_utc": now_utc.isoformat(),
            "candidate_pair": candidate.pair,
            "candidate_day_type": candidate.intended_day_type.value,
            "candidate_direction": candidate.intended_direction.value,
            "candidate_strategy": candidate.strategy_name,
        }

        # 1. circuit_breakers
        cb_result = check_circuit_breakers(
            candidate=candidate,
            positions=positions,
            account=account,
            state=self.state,
            now_utc=now_utc,
        )
        debug["pipeline"].append(_pipeline_entry(cb_result))
        self.state.save_if_dirty()
        if not cb_result.allow:
            return _to_decision(cb_result, debug)

        # 2. position_caps
        caps_result = check_position_caps(
            candidate=candidate, positions=positions
        )
        debug["pipeline"].append(_pipeline_entry(caps_result))
        if not caps_result.allow:
            return _to_decision(caps_result, debug)

        # 3. news_blackout
        news_result = check_news_blackout(
            candidate=candidate, now_utc=now_utc
        )
        debug["pipeline"].append(_pipeline_entry(news_result))
        if not news_result.allow:
            return _to_decision(news_result, debug)

        # 4. spread_filter
        spread_result = check_spread_filter(market=market)
        debug["pipeline"].append(_pipeline_entry(spread_result))
        if not spread_result.allow:
            return _to_decision(spread_result, debug)

        # 5. pre_eod_suppression
        eod_result = check_pre_eod_suppression(
            candidate=candidate, now_utc=now_utc
        )
        debug["pipeline"].append(_pipeline_entry(eod_result))
        if not eod_result.allow:
            return _to_decision(eod_result, debug)

        return RiskDecision(
            allow=True,
            rule="risk_guard",
            reason="all gates passed",
            debug=debug,
        )

    # --- EOD enforcement -----------------------------------------------------

    def positions_to_force_close(
        self,
        positions: list[OpenPosition],
        now_utc: datetime,
        *,
        structure_state_for_pair: Callable[[str], Optional[StructureState]],
    ) -> list[ForceCloseOrder]:
        """Return the list of positions that must be flat overnight.

        2c (B-1): keys on structure ``htf_bias`` per pair. The
        ``structure_state_for_pair`` callable is required so a forgotten
        call site fails loudly at import / type-check time rather than
        silently force-closing everything (which would be the
        fail-closed behaviour if structure data is missing per pair).
        """
        if not positions:
            return []

        pairs = {p.pair for p in positions}
        htf_bias_for_pair: dict[str, Optional[str]] = {}
        for pair in pairs:
            try:
                snapshot = structure_state_for_pair(pair)
            except Exception:
                snapshot = None
            htf_bias_for_pair[pair] = (
                snapshot.htf_bias if snapshot is not None else None
            )
        return apply_eod_force_close(
            positions=positions,
            now_utc=now_utc,
            htf_bias_for_pair=htf_bias_for_pair,
        )

    # --- Trade-outcome bookkeeping ------------------------------------------

    def record_trade_outcome(
        self,
        pnl_r: float,
        closed_at_utc: datetime,
    ) -> None:
        """Update consecutive-loss state. Persists if dirty."""
        record_trade_outcome(
            state=self.state, pnl_r=pnl_r, closed_at_utc=closed_at_utc
        )
        self.state.save_if_dirty()


# --- Helpers ----------------------------------------------------------------


def _pipeline_entry(result: RuleResult) -> dict:
    return {
        "rule": result.rule,
        "allow": result.allow,
        "reason": result.reason,
    }


def _to_decision(result: RuleResult, debug: dict) -> RiskDecision:
    return RiskDecision(
        allow=result.allow,
        rule=result.rule,
        reason=result.reason,
        debug=debug,
    )


__all__ = ["RiskGuard"]
