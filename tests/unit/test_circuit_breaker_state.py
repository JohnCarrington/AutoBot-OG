"""Tests for risk.state.circuit_breaker_state."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from risk.state.circuit_breaker_state import (
    CircuitBreakerState,
    current_session_date_ny,
)


# --- Session-date helper ---------------------------------------------------


def test_session_date_before_ny_close_is_today() -> None:
    # 14:00 ET on 2025-05-14 (Wed) → still in Wed's session.
    # 14:00 EDT = 18:00 UTC.
    now_utc = datetime(2025, 5, 14, 18, 0, tzinfo=timezone.utc)
    assert current_session_date_ny(now_utc) == date(2025, 5, 14)


def test_session_date_at_ny_close_rolls_forward() -> None:
    # 17:00 ET on Wed → start of Thursday's session.
    # 17:00 EDT = 21:00 UTC.
    now_utc = datetime(2025, 5, 14, 21, 0, tzinfo=timezone.utc)
    assert current_session_date_ny(now_utc) == date(2025, 5, 15)


def test_session_date_after_ny_close_is_tomorrow() -> None:
    # 22:00 UTC = 18:00 EDT → past 17:00, so we're in tomorrow's session.
    now_utc = datetime(2025, 5, 14, 22, 0, tzinfo=timezone.utc)
    assert current_session_date_ny(now_utc) == date(2025, 5, 15)


def test_session_date_dst_winter_at_close() -> None:
    # 2025-01-14 winter (EST), 17:00 EST = 22:00 UTC.
    now_utc = datetime(2025, 1, 14, 22, 0, tzinfo=timezone.utc)
    assert current_session_date_ny(now_utc) == date(2025, 1, 15)


# --- Construction / load fresh ---------------------------------------------


def test_load_returns_fresh_state_if_file_missing(tmp_path: Path) -> None:
    path = tmp_path / "cb.json"
    s = CircuitBreakerState.load(path)
    assert s.loss_streak == 0
    assert s.consecutive_loss_cooldown_until_utc is None
    assert s.daily_dd_cooldown_until_utc is None
    assert s.regime_instability_cooldown_until_utc is None
    assert s.path == path
    assert s._dirty is False


def test_load_returns_fresh_state_if_file_malformed(tmp_path: Path) -> None:
    path = tmp_path / "cb.json"
    path.write_text("{not valid json")
    s = CircuitBreakerState.load(path)
    assert s.loss_streak == 0
    assert s.path == path


def test_load_recovers_from_unparseable_datetime(tmp_path: Path) -> None:
    path = tmp_path / "cb.json"
    path.write_text(
        json.dumps(
            {
                "loss_streak": 2,
                "consecutive_loss_cooldown_until_utc": "not-a-date",
                "daily_dd_cooldown_until_utc": "garbage",
                "daily_dd_session_date": "not-a-date",
                "regime_instability_cooldown_until_utc": None,
                "regime_instability_pair": None,
            }
        )
    )
    s = CircuitBreakerState.load(path)
    # Parseable fields survive; unparseable ones drop to None.
    assert s.loss_streak == 2
    assert s.consecutive_loss_cooldown_until_utc is None
    assert s.daily_dd_cooldown_until_utc is None
    assert s.daily_dd_session_date is None


# --- Round-trip save/load --------------------------------------------------


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "cb.json"
    cooldown_until = datetime(2025, 5, 14, 18, 0, tzinfo=timezone.utc)
    saved = CircuitBreakerState(
        path=path,
        loss_streak=3,
        consecutive_loss_cooldown_until_utc=cooldown_until,
        daily_dd_session_date=date(2025, 5, 14),
        daily_dd_cooldown_until_utc=cooldown_until + timedelta(hours=1),
        regime_instability_cooldown_until_utc=cooldown_until,
        regime_instability_pair="GBPUSD",
    )
    saved.save()
    loaded = CircuitBreakerState.load(path)
    assert loaded.loss_streak == 3
    assert loaded.consecutive_loss_cooldown_until_utc == cooldown_until
    assert loaded.daily_dd_session_date == date(2025, 5, 14)
    assert (
        loaded.daily_dd_cooldown_until_utc
        == cooldown_until + timedelta(hours=1)
    )
    assert loaded.regime_instability_pair == "GBPUSD"


def test_save_creates_parent_directories(tmp_path: Path) -> None:
    deep = tmp_path / "nested" / "deeper" / "cb.json"
    s = CircuitBreakerState(path=deep, loss_streak=1)
    s.save()
    assert deep.exists()


# --- Dirty tracking --------------------------------------------------------


def test_dirty_flag_initially_false(tmp_path: Path) -> None:
    s = CircuitBreakerState(path=tmp_path / "cb.json")
    assert s._dirty is False


def test_mark_dirty_sets_flag(tmp_path: Path) -> None:
    s = CircuitBreakerState(path=tmp_path / "cb.json")
    s.mark_dirty()
    assert s._dirty is True


def test_save_clears_dirty_flag(tmp_path: Path) -> None:
    s = CircuitBreakerState(path=tmp_path / "cb.json", loss_streak=2)
    s.mark_dirty()
    s.save()
    assert s._dirty is False


def test_save_if_dirty_does_nothing_when_clean(tmp_path: Path) -> None:
    path = tmp_path / "cb.json"
    s = CircuitBreakerState(path=path)
    s.save_if_dirty()
    assert not path.exists()


def test_save_if_dirty_persists_when_dirty(tmp_path: Path) -> None:
    path = tmp_path / "cb.json"
    s = CircuitBreakerState(path=path, loss_streak=5)
    s.mark_dirty()
    s.save_if_dirty()
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["loss_streak"] == 5


# --- reset_daily_dd_if_new_session ----------------------------------------


def test_reset_daily_dd_does_nothing_in_same_session(tmp_path: Path) -> None:
    # First, set state to the current session.
    now = datetime(2025, 5, 14, 14, 0, tzinfo=timezone.utc)  # before 17:00 ET
    session = current_session_date_ny(now)
    s = CircuitBreakerState(
        path=tmp_path / "cb.json",
        daily_dd_session_date=session,
        daily_dd_cooldown_until_utc=datetime(
            2025, 5, 14, 21, 0, tzinfo=timezone.utc
        ),
    )
    changed = s.reset_daily_dd_if_new_session(now)
    assert changed is False
    assert s.daily_dd_cooldown_until_utc is not None


def test_reset_daily_dd_clears_on_new_session(tmp_path: Path) -> None:
    # State belongs to yesterday's session.
    s = CircuitBreakerState(
        path=tmp_path / "cb.json",
        daily_dd_session_date=date(2025, 5, 13),
        daily_dd_cooldown_until_utc=datetime(
            2025, 5, 13, 20, 0, tzinfo=timezone.utc
        ),
    )
    # Check at 2025-05-14 14:00 UTC (10:00 EDT → still in 2025-05-14 session).
    now = datetime(2025, 5, 14, 14, 0, tzinfo=timezone.utc)
    changed = s.reset_daily_dd_if_new_session(now)
    assert changed is True
    assert s.daily_dd_cooldown_until_utc is None
    assert s.daily_dd_session_date == date(2025, 5, 14)
    assert s._dirty is True


def test_reset_daily_dd_first_call_initialises_session(tmp_path: Path) -> None:
    # Brand new state has session_date=None.
    s = CircuitBreakerState(path=tmp_path / "cb.json")
    now = datetime(2025, 5, 14, 14, 0, tzinfo=timezone.utc)
    changed = s.reset_daily_dd_if_new_session(now)
    assert changed is True
    assert s.daily_dd_session_date == date(2025, 5, 14)
