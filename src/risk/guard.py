"""RiskGuard — the public orchestrator for the risk layer.

A single instance per running bot. Holds:

- a :py:class:`regime.RegimeEngine` reference (read-only access for
  ``get_recent_emissions`` and ``regime_live_at_last_h1_close``),
- a :py:class:`risk.state.CircuitBreakerState` loaded from disk.

Three public methods:

- :py:meth:`allow_entry` runs the rule pipeline (circuit_breakers →
  position_caps → news_blackout → spread_filter → pre-EOD
  suppression). First rejection short-circuits.
- :py:meth:`positions_to_force_close` runs EOD enforcement and
  returns the list of close orders the caller should send.
- :py:meth:`record_trade_outcome` updates the consecutive-loss state
  after a trade closes.

State is persisted lazily — only when a rule marks state dirty.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from regime.engine import RegimeEngine

from .constants import REGIME_INSTABILITY_WINDOW_MIN
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
        engine: RegimeEngine,
        state: Optional[CircuitBreakerState] = None,
        *,
        state_path: Path | str | None = None,
    ) -> None:
        """Construct the guard.

        Parameters
        ----------
        engine : RegimeEngine
            The pair's regime engine. Used by circuit_breakers for
            emission queries and the live-at-last-H1 check.
        state : CircuitBreakerState, optional
            Pre-loaded state object. Useful in tests. If omitted, state
            is loaded from ``state_path`` (or :data:`DEFAULT_STATE_PATH`).
        state_path : Path or str, optional
            Where to load/persist circuit-breaker state. Ignored if
            ``state`` is provided.
        """
        self.engine = engine
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

        1. ``circuit_breakers`` — daily DD, loss streak, regime instability.
           May mutate state. Persisted on dirty.
        2. ``position_caps`` — global / per-pair / per-regime caps.
        3. ``news_blackout`` — per-currency calendar lookup.
        4. ``spread_filter`` — live spread vs cap.
        5. ``pre_eod_suppression`` — buffer before NY close.
        """
        debug: dict = {
            "pipeline": [],
            "now_utc": now_utc.isoformat(),
            "candidate_pair": candidate.pair,
            "candidate_regime": candidate.intended_regime.value,
            "candidate_direction": candidate.intended_direction.value,
        }

        # 1. circuit_breakers
        recent_emissions = self.engine.get_recent_emissions(
            window_minutes=REGIME_INSTABILITY_WINDOW_MIN,
            now_utc=now_utc,
        )
        live_at_last = self.engine.regime_live_at_last_h1_close()
        cb_result = check_circuit_breakers(
            candidate=candidate,
            positions=positions,
            account=account,
            state=self.state,
            recent_emissions=recent_emissions,
            live_at_last_h1_close=live_at_last,
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
    ) -> list[ForceCloseOrder]:
        """Return the list of positions that must be flat overnight.

        Reads ``current_regime`` / ``current_direction`` directly from
        the engine — these are the post-most-recent-H1-close values
        (the spec's "regime still TREND, same direction" check).
        """
        return apply_eod_force_close(
            positions=positions,
            now_utc=now_utc,
            current_regime=self.engine.current_regime,
            current_direction=self.engine.current_direction,
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
