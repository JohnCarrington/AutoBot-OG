"""Tests for execution.types — ExecutionPosition transitions + helpers."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from execution.types import (
    ExecutionPosition,
    ReconciliationEvent,
    ReconciliationKind,
    ReconciliationReport,
    ReconciliationSeverity,
    SLAmendment,
)
from regime.labels import Direction, RegimeLabel


_TS = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)


def _pos(**overrides) -> ExecutionPosition:
    defaults = dict(
        deal_id="DEAL_1",
        deal_reference="REF_1",
        pair="GBPUSD",
        direction=Direction.BULLISH,
        day_type_at_entry=RegimeLabel.TREND,
        strategy_name="ema_continuation",
        size_units=1.0,
        entry_price=1.30000,
        initial_sl_price=1.29850,
        current_sl_price=1.29850,
        suggested_tp_price=None,
        entry_time_utc=_TS,
        signal_source_candle_ts=_TS,
        be_moved=False,
        trail_active=False,
    )
    defaults.update(overrides)
    return ExecutionPosition(**defaults)


def test_execution_position_is_frozen() -> None:
    p = _pos()
    with pytest.raises(FrozenInstanceError):
        p.entry_price = 1.31  # type: ignore[misc]


def test_initial_sl_distance_is_absolute() -> None:
    # long
    p_long = _pos(entry_price=1.30000, initial_sl_price=1.29850)
    assert p_long.initial_sl_distance == pytest.approx(0.00150)
    # short — mirror
    p_short = _pos(direction=Direction.BEARISH, initial_sl_price=1.30150)
    assert p_short.initial_sl_distance == pytest.approx(0.00150)


def test_current_pnl_r_long() -> None:
    p = _pos(entry_price=1.30000, initial_sl_price=1.29850)  # 15p risk
    # Price up 15p → +1R.
    assert p.current_pnl_r(1.30150) == pytest.approx(1.0)
    # Price at entry → 0R.
    assert p.current_pnl_r(1.30000) == pytest.approx(0.0)
    # Price hit SL → -1R.
    assert p.current_pnl_r(1.29850) == pytest.approx(-1.0)


def test_current_pnl_r_short_mirror() -> None:
    p = _pos(
        direction=Direction.BEARISH,
        entry_price=1.30000,
        initial_sl_price=1.30150,
    )
    assert p.current_pnl_r(1.29850) == pytest.approx(1.0)
    assert p.current_pnl_r(1.30150) == pytest.approx(-1.0)


def test_current_pnl_r_zero_distance_returns_zero() -> None:
    p = _pos(entry_price=1.30000, initial_sl_price=1.30000)
    assert p.current_pnl_r(1.30150) == 0.0


def test_to_risk_open_position_adapter() -> None:
    p = _pos(entry_price=1.30000, initial_sl_price=1.29850)
    risk_view = p.to_risk_open_position(current_price=1.30150)
    assert risk_view.position_id == "DEAL_1"
    assert risk_view.pair == "GBPUSD"
    assert risk_view.direction is Direction.BULLISH
    assert risk_view.current_pnl_r == pytest.approx(1.0)


def test_with_sl_amend_appends_history_and_records_transition() -> None:
    p = _pos(entry_price=1.30000, initial_sl_price=1.29850, current_sl_price=1.29850)
    moved_at = datetime(2026, 5, 14, 12, 30, tzinfo=timezone.utc)
    updated = p.with_sl_amend(
        new_sl_price=1.30010,
        at_utc=moved_at,
        reason="be_move_at_1r",
        deal_id_or_reference="DEAL_1",
        be_moved=True,
        trail_active=True,
    )
    assert updated.current_sl_price == 1.30010
    assert updated.initial_sl_price == 1.29850   # never mutated
    assert updated.be_moved is True
    assert updated.trail_active is True
    assert len(updated.sl_history) == 1
    rec = updated.sl_history[0]
    assert isinstance(rec, SLAmendment)
    assert rec.from_price == 1.29850
    assert rec.to_price == 1.30010
    assert rec.reason == "be_move_at_1r"
    # Original is untouched (frozen).
    assert p.current_sl_price == 1.29850
    assert p.be_moved is False
    assert p.sl_history == ()


def test_with_sl_amend_preserves_flags_when_not_overridden() -> None:
    p = _pos(be_moved=True, trail_active=True, current_sl_price=1.30010)
    upd = p.with_sl_amend(
        new_sl_price=1.30050,
        at_utc=_TS,
        reason="trail_swing_primary",
        deal_id_or_reference="DEAL_1",
    )
    assert upd.be_moved is True
    assert upd.trail_active is True


def test_reconciliation_report_alerts_filter() -> None:
    info = ReconciliationEvent(
        at_utc=_TS,
        severity=ReconciliationSeverity.INFO,
        kind=ReconciliationKind.OK_NO_OP,
        deal_id=None,
        pair=None,
        message="ok",
    )
    alert = ReconciliationEvent(
        at_utc=_TS,
        severity=ReconciliationSeverity.ALERT,
        kind=ReconciliationKind.MISSING_LOCAL_KEPT,
        deal_id="DEAL_1",
        pair="GBPUSD",
        message="missing",
    )
    report = ReconciliationReport(at_utc=_TS, events=(info, alert))
    alerts = report.alerts
    assert len(alerts) == 1
    assert alerts[0].kind == ReconciliationKind.MISSING_LOCAL_KEPT
