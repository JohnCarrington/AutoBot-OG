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
from typing import Callable, Optional

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
        engine: Optional[RegimeEngine] = None,
        state: Optional[CircuitBreakerState] = None,
        *,
        state_path: Path | str | None = None,
        engine_for_pair: Optional[Callable[[str], RegimeEngine]] = None,
    ) -> None:
        """Construct the guard.

        Parameters
        ----------
        engine : RegimeEngine, optional
            Single regime engine for backward compatibility with
            single-pair (v1 GBPUSD) callers. Required when
            ``engine_for_pair`` is not supplied. Phase 4 tests use this
            form.
        state : CircuitBreakerState, optional
            Pre-loaded state object. Useful in tests. If omitted, state
            is loaded from ``state_path`` (or :data:`DEFAULT_STATE_PATH`).
        state_path : Path or str, optional
            Where to load/persist circuit-breaker state. Ignored if
            ``state`` is provided.
        engine_for_pair : Callable[[str], RegimeEngine], optional
            Multi-pair routing seam (C2 fix, adversarial review
            2026-05-15). When supplied, every method that consults
            regime state resolves the pair's engine via this callable;
            ``engine`` is then unused. Phase 8's BotLoop owns the
            per-pair engine map and hands ``lambda p: engines[p]`` here
            so RiskGuard reads the SAME object the BotLoop feeds with
            H1/M5 closes. The old code path (`engine` only) created a
            standalone engine that nothing fed, silently disabling the
            regime-instability circuit breaker.
        """
        if engine is None and engine_for_pair is None:
            raise ValueError(
                "RiskGuard requires either 'engine' (single-pair) or "
                "'engine_for_pair' (multi-pair routing)."
            )
        self._engine = engine
        self._engine_for_pair = engine_for_pair
        if state is not None:
            self.state = state
        else:
            path = (
                Path(state_path)
                if state_path is not None
                else DEFAULT_STATE_PATH
            )
            self.state = CircuitBreakerState.load(path)

    # --- Engine resolution ---------------------------------------------------

    def _resolve_engine(self, pair: str) -> RegimeEngine:
        """Return the regime engine for ``pair``.

        Prefers ``engine_for_pair`` (multi-pair routing); falls back to
        the single ``engine`` constructor argument when no callable was
        supplied. Raises if neither is reachable for the pair.
        """
        if self._engine_for_pair is not None:
            eng = self._engine_for_pair(pair)
            if eng is None:
                raise RuntimeError(
                    f"engine_for_pair returned None for {pair!r}"
                )
            return eng
        assert self._engine is not None, "RiskGuard has no engine wired"
        return self._engine

    @property
    def engine(self) -> RegimeEngine:
        """Backward-compatible accessor.

        Returns the constructor's ``engine`` argument when the legacy
        single-pair form was used. When the multi-pair callable form
        was used, raises — callers must use :py:meth:`_resolve_engine`
        with a pair (or read engine state per-pair via that path).
        """
        if self._engine is None:
            raise RuntimeError(
                "RiskGuard was constructed with engine_for_pair only; "
                "use _resolve_engine(pair) or call allow_entry() to "
                "route per pair."
            )
        return self._engine

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
            "candidate_day_type": candidate.intended_day_type.value,
            "candidate_direction": candidate.intended_direction.value,
        }

        # 1. circuit_breakers — route to the pair's engine (C2 fix).
        engine = self._resolve_engine(candidate.pair)
        recent_emissions = engine.get_recent_emissions(
            window_minutes=REGIME_INSTABILITY_WINDOW_MIN,
            now_utc=now_utc,
        )
        live_at_last = engine.regime_live_at_last_h1_close()
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

        Reads ``current_regime`` / ``current_direction`` and
        ``pending_regime`` / ``pending_direction`` directly from the
        engine. The pending state is forwarded so the EOD rule can
        force-close TREND positions when a transition to RANGE /
        VOLATILE / opposite-direction TREND is already in flight
        (H3 from the 2026-05-14 review).

        Multi-pair routing (C2 fix, 2026-05-15): each position is
        evaluated against ITS pair's regime engine — a BULLISH GBPUSD
        TREND and a BEARISH USDJPY TREND don't share regime state, so
        grouping by pair and calling ``apply_eod_force_close`` once
        per group is correct. When the legacy single-engine
        constructor form is used, every pair resolves to the same
        engine and the loop collapses to one call.
        """
        if not positions:
            return []
        # Group positions by pair to keep the call count down. The EOD
        # rule itself is per-position; only the regime snapshot inputs
        # differ per pair.
        by_pair: dict[str, list[OpenPosition]] = {}
        for p in positions:
            by_pair.setdefault(p.pair, []).append(p)
        out: list[ForceCloseOrder] = []
        for pair, pair_positions in by_pair.items():
            engine = self._resolve_engine(pair)
            out.extend(apply_eod_force_close(
                positions=pair_positions,
                now_utc=now_utc,
                current_regime=engine.current_regime,
                current_direction=engine.current_direction,
                pending_regime=engine.pending_regime,
                pending_direction=engine.pending_direction,
            ))
        return out

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
