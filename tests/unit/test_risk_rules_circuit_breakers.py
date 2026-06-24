"""Tests for risk.rules.circuit_breakers.

2c (B-2): the regime-instability breaker was deleted. Only the
daily-drawdown and consecutive-loss breakers remain.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from day_type import DayType
from common import Direction

from risk.constants import (
    CONSECUTIVE_LOSS_COOLDOWN_HOURS,
    CONSECUTIVE_LOSS_THRESHOLD,
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
        intended_day_type=DayType.NORMAL,
        planned_entry_price=1.30,
        strategy_name="bb_bounce",
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
        day_type_at_entry=DayType.NORMAL,
        strategy_name="ema_pullback",
        entry_price=1.30,
        current_price=1.31,
        entry_time_utc=_now(),
        current_pnl_r=pnl_r,
    )


# --- Allow on a quiet state ------------------------------------------------


def test_allows_on_fresh_state(tmp_path: Path) -> None:
    r = check_circuit_breakers(
        candidate=_candidate(),
        positions=[],
        account=_account(),
        state=_state(tmp_path),
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


# --- Breaker ordering ------------------------------------------------------


def test_daily_dd_takes_priority_over_consecutive_loss(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.consecutive_loss_cooldown_until_utc = _now() + timedelta(hours=2)
    state.daily_dd_session_date = current_session_date_ny(_now())
    state.daily_dd_cooldown_until_utc = _now() + timedelta(hours=2)
    r = check_circuit_breakers(
        candidate=_candidate(),
        positions=[],
        account=_account(),
        state=state,
        now_utc=_now(),
    )
    assert r.allow is False
    assert "daily_dd" in r.reason
