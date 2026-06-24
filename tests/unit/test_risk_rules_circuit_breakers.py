"""Tests for risk.rules.circuit_breakers."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from regime.engine import RegimeEmission
from regime.labels import Direction, RegimeLabel

from risk.constants import (
    CONSECUTIVE_LOSS_COOLDOWN_HOURS,
    CONSECUTIVE_LOSS_THRESHOLD,
    REGIME_INSTABILITY_PAUSE_HOURS,
)
from risk.rules.circuit_breakers import (
    check_circuit_breakers,
    record_trade_outcome,
)
from risk.state.circuit_breaker_state import (
    CircuitBreakerState,
    current_session_date_ny,
)
from risk.types import AccountState, CandidateTrade, OpenPosition


def _now() -> datetime:
    # 2025-05-14 14:00 UTC = 10:00 EDT (well inside the session).
    return datetime(2025, 5, 14, 14, 0, tzinfo=timezone.utc)


def _state(tmp_path: Path) -> CircuitBreakerState:
    return CircuitBreakerState(path=tmp_path / "cb.json")


def _candidate(pair: str = "GBPUSD") -> CandidateTrade:
    return CandidateTrade(
        pair=pair,
        intended_direction=Direction.BULLISH,
        intended_day_type=RegimeLabel.TREND,
        planned_entry_price=1.30,
    )


def _account(realized: float = 0.0) -> AccountState:
    return AccountState(
        balance=10_000.0, currency="GBP", realized_pnl_today_r=realized
    )


def _pos(pnl_r: float, pair: str = "GBPUSD") -> OpenPosition:
    return OpenPosition(
        position_id="p1",
        pair=pair,
        direction=Direction.BULLISH,
        day_type_at_entry=RegimeLabel.TREND,
        entry_price=1.30,
        current_price=1.31,
        entry_time_utc=_now(),
        current_pnl_r=pnl_r,
    )


def _emission(*, kind="H1", committed=False, was_m5_reset=False, ts=None):
    return RegimeEmission(
        timestamp=ts or _now(),
        kind=kind,
        regime=RegimeLabel.TREND,
        direction=Direction.BULLISH,
        is_live=True,
        reason="classified",
        committed=committed,
        was_m5_reset=was_m5_reset,
    )


# --- Allow on a quiet state ------------------------------------------------


def test_allows_on_fresh_state(tmp_path: Path) -> None:
    r = check_circuit_breakers(
        candidate=_candidate(),
        positions=[],
        account=_account(),
        state=_state(tmp_path),
        recent_emissions=[],
        live_at_last_h1_close=True,
        now_utc=_now(),
    )
    assert r.allow is True


# --- Daily drawdown --------------------------------------------------------


def test_daily_dd_triggers_when_total_R_at_or_below_limit(tmp_path: Path) -> None:
    state = _state(tmp_path)
    r = check_circuit_breakers(
        candidate=_candidate(),
        positions=[_pos(-1.5)],
        account=_account(realized=-1.5),  # total = -3.0 = limit
        state=state,
        recent_emissions=[],
        live_at_last_h1_close=True,
        now_utc=_now(),
    )
    assert r.allow is False
    assert "daily_dd_triggered" in r.reason
    assert state.daily_dd_cooldown_until_utc is not None


def test_daily_dd_just_above_limit_allows(tmp_path: Path) -> None:
    # Realised -1.4, unrealised -1.0 → total -2.4, above the -3 limit.
    r = check_circuit_breakers(
        candidate=_candidate(),
        positions=[_pos(-1.0)],
        account=_account(realized=-1.4),
        state=_state(tmp_path),
        recent_emissions=[],
        live_at_last_h1_close=True,
        now_utc=_now(),
    )
    assert r.allow is True


def test_existing_daily_dd_cooldown_blocks(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.daily_dd_session_date = current_session_date_ny(_now())
    state.daily_dd_cooldown_until_utc = _now() + timedelta(hours=2)
    r = check_circuit_breakers(
        candidate=_candidate(),
        positions=[],
        account=_account(),
        state=state,
        recent_emissions=[],
        live_at_last_h1_close=True,
        now_utc=_now(),
    )
    assert r.allow is False
    assert "daily_dd_cooldown" in r.reason


def test_daily_dd_clears_on_new_session(tmp_path: Path) -> None:
    state = _state(tmp_path)
    # State belongs to YESTERDAY's session.
    state.daily_dd_session_date = date(2025, 5, 12)
    state.daily_dd_cooldown_until_utc = datetime(
        2025, 5, 13, 18, 0, tzinfo=timezone.utc
    )
    # Now is 2025-05-14 14:00 UTC: in 2025-05-14 session.
    r = check_circuit_breakers(
        candidate=_candidate(),
        positions=[],
        account=_account(),
        state=state,
        recent_emissions=[],
        live_at_last_h1_close=True,
        now_utc=_now(),
    )
    assert r.allow is True
    assert state.daily_dd_cooldown_until_utc is None
    assert state.daily_dd_session_date == date(2025, 5, 14)


# --- Consecutive-loss cooldown ---------------------------------------------


def test_consecutive_loss_cooldown_blocks_while_active(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.consecutive_loss_cooldown_until_utc = _now() + timedelta(hours=2)
    state.loss_streak = CONSECUTIVE_LOSS_THRESHOLD
    r = check_circuit_breakers(
        candidate=_candidate(),
        positions=[],
        account=_account(),
        state=state,
        recent_emissions=[],
        live_at_last_h1_close=True,
        now_utc=_now(),
    )
    assert r.allow is False
    assert "consecutive_loss_cooldown" in r.reason


def test_consecutive_loss_cooldown_clears_when_elapsed(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.consecutive_loss_cooldown_until_utc = _now() - timedelta(hours=1)
    state.loss_streak = CONSECUTIVE_LOSS_THRESHOLD
    r = check_circuit_breakers(
        candidate=_candidate(),
        positions=[],
        account=_account(),
        state=state,
        recent_emissions=[],
        live_at_last_h1_close=True,
        now_utc=_now(),
    )
    assert r.allow is True
    assert state.consecutive_loss_cooldown_until_utc is None


def test_record_trade_outcome_increments_streak_on_loss(tmp_path: Path) -> None:
    state = _state(tmp_path)
    record_trade_outcome(state, pnl_r=-1.0, closed_at_utc=_now())
    assert state.loss_streak == 1
    assert state.consecutive_loss_cooldown_until_utc is None


def test_record_trade_outcome_arms_cooldown_at_threshold(tmp_path: Path) -> None:
    state = _state(tmp_path)
    for _ in range(CONSECUTIVE_LOSS_THRESHOLD):
        record_trade_outcome(state, pnl_r=-1.0, closed_at_utc=_now())
    assert state.loss_streak == CONSECUTIVE_LOSS_THRESHOLD
    assert state.consecutive_loss_cooldown_until_utc is not None
    expected = _now() + timedelta(hours=CONSECUTIVE_LOSS_COOLDOWN_HOURS)
    assert state.consecutive_loss_cooldown_until_utc == expected


def test_record_trade_outcome_resets_streak_on_win(tmp_path: Path) -> None:
    state = _state(tmp_path)
    record_trade_outcome(state, pnl_r=-1.0, closed_at_utc=_now())
    record_trade_outcome(state, pnl_r=-1.0, closed_at_utc=_now())
    record_trade_outcome(state, pnl_r=2.0, closed_at_utc=_now())
    assert state.loss_streak == 0


def test_record_trade_outcome_treats_zero_as_loss(tmp_path: Path) -> None:
    # PnL_R = 0 (broker scratch trade) counts as a non-positive outcome.
    state = _state(tmp_path)
    record_trade_outcome(state, pnl_r=0.0, closed_at_utc=_now())
    assert state.loss_streak == 1


# --- Regime instability ----------------------------------------------------


def test_regime_instability_triggers_on_too_many_commits(tmp_path: Path) -> None:
    state = _state(tmp_path)
    emissions = [_emission(committed=True) for _ in range(4)]  # > 3 commits
    r = check_circuit_breakers(
        candidate=_candidate(),
        positions=[],
        account=_account(),
        state=state,
        recent_emissions=emissions,
        live_at_last_h1_close=True,
        now_utc=_now(),
    )
    assert r.allow is False
    assert "regime_instability_triggered" in r.reason
    assert state.regime_instability_cooldown_until_utc is not None
    expected = _now() + timedelta(hours=REGIME_INSTABILITY_PAUSE_HOURS)
    assert state.regime_instability_cooldown_until_utc == expected


def test_regime_instability_triggers_on_too_many_m5_resets(tmp_path: Path) -> None:
    state = _state(tmp_path)
    emissions = [
        _emission(kind="M5", was_m5_reset=True) for _ in range(6)
    ]  # > 5 resets
    r = check_circuit_breakers(
        candidate=_candidate(),
        positions=[],
        account=_account(),
        state=state,
        recent_emissions=emissions,
        live_at_last_h1_close=True,
        now_utc=_now(),
    )
    assert r.allow is False
    assert "regime_instability_triggered" in r.reason


def test_regime_instability_at_thresholds_does_not_trigger(tmp_path: Path) -> None:
    """Counts at the threshold (3 commits, 5 m5 resets) do NOT trigger."""
    state = _state(tmp_path)
    emissions = (
        [_emission(committed=True) for _ in range(3)]
        + [_emission(kind="M5", was_m5_reset=True) for _ in range(5)]
    )
    r = check_circuit_breakers(
        candidate=_candidate(),
        positions=[],
        account=_account(),
        state=state,
        recent_emissions=emissions,
        live_at_last_h1_close=True,
        now_utc=_now(),
    )
    assert r.allow is True


def test_regime_instability_cooldown_blocks_while_active(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.regime_instability_cooldown_until_utc = _now() + timedelta(hours=2)
    state.regime_instability_pair = "GBPUSD"
    r = check_circuit_breakers(
        candidate=_candidate(),
        positions=[],
        account=_account(),
        state=state,
        recent_emissions=[],
        live_at_last_h1_close=True,
        now_utc=_now(),
    )
    assert r.allow is False
    assert "regime_instability_cooldown active" in r.reason


def test_regime_instability_cooldown_extended_when_not_live(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.regime_instability_cooldown_until_utc = _now() - timedelta(hours=1)
    state.regime_instability_pair = "GBPUSD"
    r = check_circuit_breakers(
        candidate=_candidate(),
        positions=[],
        account=_account(),
        state=state,
        recent_emissions=[],
        live_at_last_h1_close=False,
        now_utc=_now(),
    )
    assert r.allow is False
    assert "regime_instability_cooldown_extended" in r.reason
    # Cooldown timestamp is NOT cleared because we're still in extension.
    assert state.regime_instability_cooldown_until_utc is not None


def test_regime_instability_cooldown_clears_when_elapsed_and_live(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.regime_instability_cooldown_until_utc = _now() - timedelta(hours=1)
    state.regime_instability_pair = "GBPUSD"
    r = check_circuit_breakers(
        candidate=_candidate(),
        positions=[],
        account=_account(),
        state=state,
        recent_emissions=[],
        live_at_last_h1_close=True,
        now_utc=_now(),
    )
    assert r.allow is True
    assert state.regime_instability_cooldown_until_utc is None
    assert state.regime_instability_pair is None


# --- Breaker ordering ------------------------------------------------------


def test_daily_dd_takes_priority_over_other_breakers(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.consecutive_loss_cooldown_until_utc = _now() + timedelta(hours=2)
    state.regime_instability_cooldown_until_utc = _now() + timedelta(hours=2)
    state.daily_dd_session_date = current_session_date_ny(_now())
    state.daily_dd_cooldown_until_utc = _now() + timedelta(hours=2)
    r = check_circuit_breakers(
        candidate=_candidate(),
        positions=[],
        account=_account(),
        state=state,
        recent_emissions=[],
        live_at_last_h1_close=True,
        now_utc=_now(),
    )
    assert r.allow is False
    assert "daily_dd" in r.reason
