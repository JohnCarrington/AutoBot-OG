"""Tests for execution.reconciliation.reconcile."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from execution.position_manager import PositionManager
from execution.reconciliation import reconcile
from execution.state.positions_state import PositionsState
from execution.types import (
    ExecutionPosition,
    ReconciliationKind,
    ReconciliationSeverity,
    SLAmendment,
)
from feed.ig_rest.types import BrokerPosition
from regime.labels import Direction, RegimeLabel


_NOW = datetime(2026, 5, 14, 17, 0, tzinfo=timezone.utc)


def _pos(
    deal_id: str = "D1",
    *,
    pair: str = "GBPUSD",
    current_sl_price: float = 1.29850,
    entry_time_utc: datetime = _NOW,
    sl_history: tuple = (),
) -> ExecutionPosition:
    return ExecutionPosition(
        deal_id=deal_id,
        deal_reference=f"REF_{deal_id}",
        pair=pair,
        direction=Direction.BULLISH,
        day_type_at_entry=RegimeLabel.TREND,
        strategy_name="ema_continuation",
        size_units=1.0,
        entry_price=1.30000,
        initial_sl_price=1.29850,
        current_sl_price=current_sl_price,
        suggested_tp_price=None,
        entry_time_utc=entry_time_utc,
        signal_source_candle_ts=entry_time_utc,
        be_moved=False,
        trail_active=False,
        sl_history=sl_history,
    )


def _broker(
    deal_id: str = "D1",
    *,
    epic: str = "CS.D.GBPUSD.TODAY.IP",
    stop_level: float | None = 1.29850,
    direction: str = "BUY",
    size: float = 1.0,
) -> BrokerPosition:
    return BrokerPosition(
        deal_id=deal_id,
        deal_reference=f"REF_{deal_id}",
        epic=epic,
        direction=direction,  # type: ignore[arg-type]
        size=size,
        open_level=1.30000,
        stop_level=stop_level,
        limit_level=None,
        created_date_utc=_NOW,
    )


def _mgr(tmp_path: Path, positions: list[ExecutionPosition] | None = None) -> PositionManager:
    state = PositionsState(path=tmp_path / "p.json")
    for p in positions or []:
        state.upsert(p)
    state.save_if_dirty()
    return PositionManager(state)


# --- No-op / clean state ---------------------------------------------------


def test_clean_state_emits_ok_no_op(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    outcome = reconcile(manager=mgr, broker_positions=[], now_utc=_NOW)
    assert len(outcome.report.events) == 1
    assert outcome.report.events[0].kind == ReconciliationKind.OK_NO_OP
    assert outcome.actions.apply_sl_updates == {}
    assert outcome.actions.remove_deal_ids == ()


def test_matched_positions_with_equal_sl_emit_no_events(tmp_path: Path) -> None:
    p = _pos()
    mgr = _mgr(tmp_path, [p])
    outcome = reconcile(
        manager=mgr,
        broker_positions=[_broker("D1", stop_level=1.29850)],
        now_utc=_NOW,
    )
    # Only the OK_NO_OP marker.
    assert len(outcome.report.events) == 1
    assert outcome.report.events[0].kind == ReconciliationKind.OK_NO_OP


# --- SL drift --------------------------------------------------------------


def test_small_sl_drift_emits_info_and_updates(tmp_path: Path) -> None:
    p = _pos(current_sl_price=1.29850)
    mgr = _mgr(tmp_path, [p])
    # 3 pip drift — below WARN threshold (5p).
    broker_sl = 1.29880
    outcome = reconcile(
        manager=mgr,
        broker_positions=[_broker("D1", stop_level=broker_sl)],
        now_utc=_NOW,
    )
    kinds = [e.kind for e in outcome.report.events]
    severities = [e.severity for e in outcome.report.events]
    assert ReconciliationKind.MANUAL_SL_MOVE in kinds or ReconciliationKind.SL_UPDATED_FROM_BROKER in kinds
    assert ReconciliationSeverity.WARNING in severities or ReconciliationSeverity.INFO in severities
    assert outcome.actions.apply_sl_updates == {"D1": broker_sl}


def test_large_sl_drift_emits_warning(tmp_path: Path) -> None:
    p = _pos(current_sl_price=1.29850)
    mgr = _mgr(tmp_path, [p])
    # 10 pip drift — above WARN threshold.
    broker_sl = 1.29950
    outcome = reconcile(
        manager=mgr,
        broker_positions=[_broker("D1", stop_level=broker_sl)],
        now_utc=_NOW,
    )
    drift_events = [
        e for e in outcome.report.events
        if e.kind == ReconciliationKind.SL_DRIFT_LARGE
    ]
    assert len(drift_events) == 1
    assert drift_events[0].severity == ReconciliationSeverity.WARNING
    assert outcome.actions.apply_sl_updates == {"D1": broker_sl}


def test_manual_sl_move_flagged_when_not_in_history(tmp_path: Path) -> None:
    """Broker SL doesn't match local history → operator probably amended via web."""
    amend = SLAmendment(
        at_utc=_NOW,
        from_price=1.29850,
        to_price=1.30010,
        reason="be_move_at_1r",
        deal_id_or_reference="D1",
    )
    p = _pos(current_sl_price=1.30010, sl_history=(amend,))
    mgr = _mgr(tmp_path, [p])
    # Broker says SL is at 1.30050 — neither local current nor any history entry.
    outcome = reconcile(
        manager=mgr,
        broker_positions=[_broker("D1", stop_level=1.30050)],
        now_utc=_NOW,
    )
    manual = [
        e for e in outcome.report.events
        if e.kind == ReconciliationKind.MANUAL_SL_MOVE
    ]
    assert len(manual) == 1
    assert manual[0].severity == ReconciliationSeverity.WARNING


def test_broker_sl_in_history_treated_as_known_amend(tmp_path: Path) -> None:
    amend = SLAmendment(
        at_utc=_NOW,
        from_price=1.29850,
        to_price=1.30010,
        reason="be_move_at_1r",
        deal_id_or_reference="D1",
    )
    # Local thinks SL is at 1.30020, but broker reports 1.30010 (from history).
    p = _pos(current_sl_price=1.30020, sl_history=(amend,))
    mgr = _mgr(tmp_path, [p])
    outcome = reconcile(
        manager=mgr,
        broker_positions=[_broker("D1", stop_level=1.30010)],
        now_utc=_NOW,
    )
    # 1 pip diff — small, but broker_sl matches history → SL_UPDATED_FROM_BROKER.
    kinds = [e.kind for e in outcome.report.events]
    assert ReconciliationKind.SL_UPDATED_FROM_BROKER in kinds


# --- Missing on broker -----------------------------------------------------


def test_missing_broker_without_confirmation_keeps_local_and_alerts(
    tmp_path: Path,
) -> None:
    p = _pos("D1")
    mgr = _mgr(tmp_path, [p])
    outcome = reconcile(
        manager=mgr,
        broker_positions=[],  # broker reports no positions
        deal_confirmations_log={},  # no close confirmation
        now_utc=_NOW,
    )
    missing = [
        e for e in outcome.report.events
        if e.kind == ReconciliationKind.MISSING_LOCAL_KEPT
    ]
    assert len(missing) == 1
    assert missing[0].severity == ReconciliationSeverity.ALERT
    # Local state NOT scheduled for removal.
    assert outcome.actions.remove_deal_ids == ()


def test_missing_broker_with_confirmation_schedules_removal(tmp_path: Path) -> None:
    p = _pos("D1")
    mgr = _mgr(tmp_path, [p])
    outcome = reconcile(
        manager=mgr,
        broker_positions=[],
        deal_confirmations_log={"D1": {"status": "ACCEPTED", "level": 1.30150}},
        now_utc=_NOW,
    )
    closed = [
        e for e in outcome.report.events
        if e.kind == ReconciliationKind.POSITION_CLOSED
    ]
    assert len(closed) == 1
    assert closed[0].severity == ReconciliationSeverity.INFO
    assert outcome.actions.remove_deal_ids == ("D1",)


# --- Broker orphan ---------------------------------------------------------


def test_broker_orphan_emits_alert_no_action(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)  # no local positions
    outcome = reconcile(
        manager=mgr,
        broker_positions=[_broker("ORPHAN_1")],
        now_utc=_NOW,
    )
    orphans = [
        e for e in outcome.report.events
        if e.kind == ReconciliationKind.BROKER_ORPHAN
    ]
    assert len(orphans) == 1
    assert orphans[0].severity == ReconciliationSeverity.ALERT
    # M1 regression (review 2026-05-14): pair must be the symbol
    # ("GBPUSD"), not the IG epic. The raw epic is preserved in debug.
    assert orphans[0].pair == "GBPUSD"
    assert orphans[0].debug["broker_epic"] == "CS.D.GBPUSD.TODAY.IP"
    # No auto-import.
    assert outcome.actions.apply_sl_updates == {}
    assert outcome.actions.remove_deal_ids == ()


# --- Stale position --------------------------------------------------------


def test_stale_position_emits_warning(tmp_path: Path) -> None:
    # Position 12h old (threshold = 8h).
    stale_entry = _NOW - timedelta(hours=12)
    p = _pos("D1", entry_time_utc=stale_entry)
    mgr = _mgr(tmp_path, [p])
    outcome = reconcile(
        manager=mgr,
        broker_positions=[_broker("D1")],
        now_utc=_NOW,
    )
    stale = [
        e for e in outcome.report.events
        if e.kind == ReconciliationKind.STALE_POSITION
    ]
    assert len(stale) == 1
    assert stale[0].severity == ReconciliationSeverity.WARNING


def test_recent_position_not_flagged_stale(tmp_path: Path) -> None:
    p = _pos("D1", entry_time_utc=_NOW - timedelta(hours=2))
    mgr = _mgr(tmp_path, [p])
    outcome = reconcile(
        manager=mgr,
        broker_positions=[_broker("D1")],
        now_utc=_NOW,
    )
    stale = [
        e for e in outcome.report.events
        if e.kind == ReconciliationKind.STALE_POSITION
    ]
    assert len(stale) == 0


# --- Multiple events in one pass -------------------------------------------


def test_mixed_local_and_broker_state(tmp_path: Path) -> None:
    """Tracked position with SL drift + an orphan broker position."""
    p = _pos("D1", current_sl_price=1.29850)
    mgr = _mgr(tmp_path, [p])
    outcome = reconcile(
        manager=mgr,
        broker_positions=[
            _broker("D1", stop_level=1.29870),  # 2p drift
            _broker("ORPHAN_2"),
        ],
        now_utc=_NOW,
    )
    kinds = {e.kind for e in outcome.report.events}
    # SL update + BROKER_ORPHAN at minimum.
    assert ReconciliationKind.BROKER_ORPHAN in kinds
    assert outcome.actions.apply_sl_updates == {"D1": 1.29870}
    # The orphan is NOT scheduled for any local mutation.
    assert outcome.actions.remove_deal_ids == ()
