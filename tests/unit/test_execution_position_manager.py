"""Tests for execution.position_manager.PositionManager."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from execution.position_manager import PositionManager
from execution.state.positions_state import PositionsState
from execution.types import ExecutionPosition
from day_type import DayType
from regime.labels import Direction


_TS = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)


def _pos(deal_id: str = "D1", **overrides) -> ExecutionPosition:
    # Persistence parses ``day_type_at_entry`` strictly via ``DayType(value)``
    # — use a DayType value here, not RegimeLabel (which the 2c-zone tests
    # still use elsewhere under Python's runtime duck typing).
    defaults = dict(
        deal_id=deal_id,
        deal_reference=f"REF_{deal_id}",
        pair="GBPUSD",
        direction=Direction.BULLISH,
        day_type_at_entry=DayType.NORMAL,
        strategy_name="ema_pullback",
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


def _mgr(tmp_path: Path) -> PositionManager:
    return PositionManager(PositionsState(path=tmp_path / "p.json"))


# --- Upsert / get / count ---------------------------------------------------


def test_upsert_and_get(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    p = _pos()
    mgr.upsert(p)
    assert mgr.get("D1") == p
    assert len(mgr) == 1


def test_count_for_pair(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    mgr.upsert(_pos("D1", pair="GBPUSD"))
    mgr.upsert(_pos("D2", pair="EURUSD"))
    assert mgr.count_for_pair("GBPUSD") == 1
    assert mgr.count_for_pair("EURUSD") == 1
    assert mgr.count_for_pair("USDJPY") == 0


def test_for_pair_returns_matching_positions(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    mgr.upsert(_pos("D1", pair="GBPUSD"))
    mgr.upsert(_pos("D2", pair="GBPUSD"))
    mgr.upsert(_pos("D3", pair="EURUSD"))
    gbp = mgr.for_pair("GBPUSD")
    assert len(gbp) == 2
    assert {p.deal_id for p in gbp} == {"D1", "D2"}


# --- Idempotency lookup ----------------------------------------------------


def test_by_signal_source_lookup_hits(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    src = _TS + timedelta(minutes=5)
    p = _pos("D1", signal_source_candle_ts=src, strategy_name="bb_bounce")
    mgr.upsert(p)
    got = mgr.by_signal_source("GBPUSD", "bb_bounce", src)
    assert got is not None and got.deal_id == "D1"


def test_by_signal_source_lookup_misses_on_different_strategy(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    src = _TS + timedelta(minutes=5)
    p = _pos("D1", signal_source_candle_ts=src, strategy_name="bb_bounce")
    mgr.upsert(p)
    got = mgr.by_signal_source("GBPUSD", "ema_pullback", src)
    assert got is None


def test_by_signal_source_lookup_misses_on_different_ts(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    p = _pos("D1", signal_source_candle_ts=_TS)
    mgr.upsert(p)
    other_ts = _TS + timedelta(minutes=5)
    got = mgr.by_signal_source("GBPUSD", "ema_pullback", other_ts)
    assert got is None


def test_pair_lookup_case_insensitive(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    mgr.upsert(_pos("D1", pair="GBPUSD"))
    assert mgr.count_for_pair("gbpusd") == 1
    assert len(mgr.for_pair("gbpusd")) == 1


# --- Remove ---------------------------------------------------------------


def test_remove_clears_position_and_indices(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    p = _pos("D1")
    mgr.upsert(p)
    removed = mgr.remove("D1")
    assert removed == p
    assert mgr.get("D1") is None
    assert mgr.count_for_pair("GBPUSD") == 0
    assert mgr.by_signal_source(
        "GBPUSD", p.strategy_name, p.signal_source_candle_ts
    ) is None


def test_remove_missing_returns_none(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    assert mgr.remove("X") is None


# --- Replace via upsert ----------------------------------------------------


def test_upsert_replacement_refreshes_indices(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    p = _pos("D1", pair="GBPUSD", strategy_name="bb_bounce")
    mgr.upsert(p)
    # Same deal_id but updated SL.
    updated = p.with_changes(current_sl_price=1.30010, be_moved=True)
    mgr.upsert(updated)
    got = mgr.get("D1")
    assert got is not None and got.be_moved is True
    assert mgr.count_for_pair("GBPUSD") == 1  # still one


# --- Persistence wiring ---------------------------------------------------


def test_upsert_persists_to_disk(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    mgr = PositionManager(PositionsState(path=path))
    mgr.upsert(_pos())
    assert path.exists()
    reloaded = PositionManager.load_from_path(path)
    assert reloaded.get("D1") is not None


def test_load_from_default_path_returns_empty_when_missing(tmp_path: Path) -> None:
    nonexistent = tmp_path / "no.json"
    mgr = PositionManager.load_from_path(nonexistent)
    assert len(mgr) == 0
