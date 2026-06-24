"""Tests for execution.state.positions_state.PositionsState."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from execution.state.positions_state import (
    PositionsState,
    PositionsStateSchemaError,
)
from execution.types import ExecutionPosition, SLAmendment
from day_type import DayType
from regime.labels import Direction


_TS = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)


def _pos(deal_id: str = "D1", **overrides) -> ExecutionPosition:
    # Persistence parses ``day_type_at_entry`` strictly via ``DayType(value)``
    # so the field value here must be a DayType member (not RegimeLabel, even
    # though duck-typing accepts it elsewhere during the 2a/2b transition).
    defaults = dict(
        deal_id=deal_id,
        deal_reference=f"REF_{deal_id}",
        pair="GBPUSD",
        direction=Direction.BULLISH,
        day_type_at_entry=DayType.NORMAL,
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


# --- Empty / fresh ----------------------------------------------------------


def test_fresh_state_is_empty(tmp_path: Path) -> None:
    s = PositionsState(path=tmp_path / "p.json")
    assert len(s) == 0
    assert s.values() == []
    assert s.dirty is False


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    s = PositionsState.load(tmp_path / "missing.json")
    assert len(s) == 0


# --- Upsert / remove --------------------------------------------------------


def test_upsert_marks_dirty(tmp_path: Path) -> None:
    s = PositionsState(path=tmp_path / "p.json")
    s.upsert(_pos())
    assert len(s) == 1
    assert s.dirty is True


def test_upsert_rejects_empty_deal_id(tmp_path: Path) -> None:
    s = PositionsState(path=tmp_path / "p.json")
    with pytest.raises(ValueError, match="deal_id"):
        s.upsert(_pos(deal_id=""))


def test_remove_clears_position(tmp_path: Path) -> None:
    s = PositionsState(path=tmp_path / "p.json")
    s.upsert(_pos("D1"))
    s.save_if_dirty()
    removed = s.remove("D1")
    assert removed is not None
    assert removed.deal_id == "D1"
    assert "D1" not in s


def test_remove_unknown_returns_none(tmp_path: Path) -> None:
    s = PositionsState(path=tmp_path / "p.json")
    assert s.remove("missing") is None
    assert s.dirty is False


# --- Persistence roundtrip --------------------------------------------------


def test_save_and_reload_roundtrips(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    s = PositionsState(path=path)
    p1 = _pos("D1")
    p2 = _pos("D2", direction=Direction.BEARISH, initial_sl_price=1.30150,
              current_sl_price=1.30150)
    s.upsert(p1)
    s.upsert(p2)
    assert s.save_if_dirty() is True
    assert s.dirty is False

    loaded = PositionsState.load(path)
    assert len(loaded) == 2
    got1 = loaded.get("D1")
    got2 = loaded.get("D2")
    assert got1 == p1
    assert got2 == p2


def test_save_if_dirty_noop_when_clean(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    s = PositionsState(path=path)
    s.upsert(_pos())
    s.save_if_dirty()
    assert s.save_if_dirty() is False  # clean already


def test_save_atomic_does_not_leave_tmp_file(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    s = PositionsState(path=path)
    s.upsert(_pos())
    s.save_if_dirty()
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".positions_")]
    assert leftovers == []


def test_sl_history_persists(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    s = PositionsState(path=path)
    amend = SLAmendment(
        at_utc=_TS,
        from_price=1.29850,
        to_price=1.30010,
        reason="be_move_at_1r",
        deal_id_or_reference="D1",
    )
    p = _pos("D1", be_moved=True, trail_active=True, current_sl_price=1.30010,
             sl_history=(amend,))
    s.upsert(p)
    s.save_if_dirty()
    loaded = PositionsState.load(path)
    got = loaded.get("D1")
    assert got is not None
    assert got.be_moved is True
    assert got.trail_active is True
    assert len(got.sl_history) == 1
    assert got.sl_history[0].reason == "be_move_at_1r"


# --- Fail-open on corruption ------------------------------------------------


def test_load_corrupt_json_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_text("{not valid json", encoding="utf-8")
    s = PositionsState.load(path)
    assert len(s) == 0  # fail-open


def test_load_wrong_schema_version_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_text(
        json.dumps({"schema_version": 99, "positions": []}), encoding="utf-8"
    )
    s = PositionsState.load(path)
    assert len(s) == 0


def test_load_invalid_position_entry_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,  # current; the entry-validation path
                "positions": [{"deal_id": "D1"}],  # missing required fields
            }
        ),
        encoding="utf-8",
    )
    s = PositionsState.load(path)
    assert len(s) == 0


def test_deserialize_rejects_non_dict_payload(tmp_path: Path) -> None:
    # Direct deserialise call to exercise the schema-error path.
    from execution.state.positions_state import _deserialize  # noqa: PLC0415

    with pytest.raises(PositionsStateSchemaError):
        _deserialize([])  # type: ignore[arg-type]
