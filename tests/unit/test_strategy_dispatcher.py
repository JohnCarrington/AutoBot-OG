"""Tests for the strategy dispatcher.

Strategies are stubbed via monkeypatch so the dispatcher's routing
logic is tested in isolation from pattern detection.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

import pandas as pd
import pytest

from day_type import DayType
from regime.labels import Direction, RegimeLabel
from strategies import dispatcher
from strategies.signal import Signal, compute_invalid_after
from structure_engine import StructureState


_NOW = datetime(2025, 5, 14, 13, 0, tzinfo=timezone.utc)


def _stub_structure_state() -> StructureState:
    return StructureState(
        pair="GBPUSD",
        timestamp=_NOW.isoformat(),
        is_valid=True,
        htf_bias="NEUTRAL",
        local_bias="NEUTRAL",
        nearest_support=None,
        nearest_resistance=None,
        liquidity_above=None,
        liquidity_below=None,
        current_reaction="NONE",
        acceptance_state="NONE",
        structure_mode="UNKNOWN",
        confidence=0.0,
        reason="stub",
        levels=[],
        debug={},
    )


def _stub_signal(strategy_name: str, regime: RegimeLabel) -> Signal:  # noqa: ARG001 — regime arg unused in 2a (placeholder day_type), kept for caller-symmetry until 2b
    return Signal(
        pair="GBPUSD",
        direction=Direction.BULLISH,
        day_type=DayType.NORMAL,
        strategy_name=strategy_name,  # type: ignore[arg-type]
        suggested_entry_price=1.30000,
        suggested_sl_price=1.29850,
        suggested_tp_price=None,
        confidence_score=0.75,
        source_candle_ts=_NOW,
        invalid_after_candle_ts=compute_invalid_after(_NOW),
        debug={},
    )


def _state(regime: str) -> dict:
    return {
        "current_regime": regime,
        "current_direction": None,
        "pending_regime": None,
        "m5_confirmation_count": 0,
        "last_regime_change_time": None,
        "reason": "test",
        "debug": {},
    }


def _stub(strategy_name: str, regime: RegimeLabel) -> Callable[..., object]:
    """Build a stub detect_* that records its call and returns a signal."""
    calls: list[dict] = []

    def _fn(df_m5, df_h1, regime_state, structure_state, pair, current_time):  # type: ignore[no-untyped-def]
        calls.append(
            {
                "df_m5_id": id(df_m5),
                "pair": pair,
                "regime": regime_state.get("current_regime"),
                "structure_state_id": id(structure_state),
            }
        )
        return _stub_signal(strategy_name, regime)

    _fn.calls = calls  # type: ignore[attr-defined]
    return _fn


def _none_stub() -> Callable[..., object]:
    def _fn(*_a, **_kw):  # type: ignore[no-untyped-def]
        return None

    return _fn


@pytest.fixture
def empty_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    return pd.DataFrame(), pd.DataFrame()


# --- Routing matrix ---------------------------------------------------------


def test_range_routes_to_bb_reclaim(monkeypatch, empty_frames) -> None:
    stub = _stub("bb_reclaim", RegimeLabel.RANGE)
    monkeypatch.setattr(dispatcher, "detect_bb_reclaim", stub)
    out = dispatcher.detect_all_setups(
        df_m5=empty_frames[0],
        df_h1=empty_frames[1],
        regime_state=_state("RANGE"),
        structure_state=_stub_structure_state(),
        pair="GBPUSD",
        current_time=_NOW,
    )
    assert len(out) == 1
    assert out[0].strategy_name == "bb_reclaim"
    assert len(stub.calls) == 1  # type: ignore[attr-defined]


def test_trend_routes_to_ema_continuation(monkeypatch, empty_frames) -> None:
    stub = _stub("ema_continuation", RegimeLabel.TREND)
    monkeypatch.setattr(dispatcher, "detect_ema_continuation", stub)
    out = dispatcher.detect_all_setups(
        df_m5=empty_frames[0],
        df_h1=empty_frames[1],
        regime_state=_state("TREND"),
        structure_state=_stub_structure_state(),
        pair="GBPUSD",
        current_time=_NOW,
    )
    assert len(out) == 1
    assert out[0].strategy_name == "ema_continuation"


def test_volatile_routes_to_liquidity_sweep(monkeypatch, empty_frames) -> None:
    stub = _stub("liquidity_sweep", RegimeLabel.VOLATILE)
    monkeypatch.setattr(dispatcher, "detect_liquidity_sweep", stub)
    out = dispatcher.detect_all_setups(
        df_m5=empty_frames[0],
        df_h1=empty_frames[1],
        regime_state=_state("VOLATILE"),
        structure_state=_stub_structure_state(),
        pair="GBPUSD",
        current_time=_NOW,
    )
    assert len(out) == 1
    assert out[0].strategy_name == "liquidity_sweep"


def test_transition_returns_empty(monkeypatch, empty_frames) -> None:
    # No strategy should be invoked.
    bb_stub = _stub("bb_reclaim", RegimeLabel.RANGE)
    monkeypatch.setattr(dispatcher, "detect_bb_reclaim", bb_stub)
    out = dispatcher.detect_all_setups(
        df_m5=empty_frames[0],
        df_h1=empty_frames[1],
        regime_state=_state("TRANSITION"),
        structure_state=_stub_structure_state(),
        pair="GBPUSD",
        current_time=_NOW,
    )
    assert out == []
    assert bb_stub.calls == []  # type: ignore[attr-defined]


def test_unknown_regime_returns_empty(monkeypatch, empty_frames) -> None:
    out = dispatcher.detect_all_setups(
        df_m5=empty_frames[0],
        df_h1=empty_frames[1],
        regime_state=_state("NONSENSE"),
        structure_state=_stub_structure_state(),
        pair="GBPUSD",
        current_time=_NOW,
    )
    assert out == []


# --- None propagation ------------------------------------------------------


def test_strategy_returns_none_yields_empty_list(monkeypatch, empty_frames) -> None:
    monkeypatch.setattr(dispatcher, "detect_bb_reclaim", _none_stub())
    out = dispatcher.detect_all_setups(
        df_m5=empty_frames[0],
        df_h1=empty_frames[1],
        regime_state=_state("RANGE"),
        structure_state=_stub_structure_state(),
        pair="GBPUSD",
        current_time=_NOW,
    )
    assert out == []


# --- No cross-talk ----------------------------------------------------------


def test_only_matching_strategy_invoked(monkeypatch, empty_frames) -> None:
    """In RANGE, the TREND and VOLATILE strategies must not be called."""
    bb = _stub("bb_reclaim", RegimeLabel.RANGE)
    ema = _stub("ema_continuation", RegimeLabel.TREND)
    sweep = _stub("liquidity_sweep", RegimeLabel.VOLATILE)
    monkeypatch.setattr(dispatcher, "detect_bb_reclaim", bb)
    monkeypatch.setattr(dispatcher, "detect_ema_continuation", ema)
    monkeypatch.setattr(dispatcher, "detect_liquidity_sweep", sweep)
    dispatcher.detect_all_setups(
        df_m5=empty_frames[0],
        df_h1=empty_frames[1],
        regime_state=_state("RANGE"),
        structure_state=_stub_structure_state(),
        pair="GBPUSD",
        current_time=_NOW,
    )
    assert len(bb.calls) == 1  # type: ignore[attr-defined]
    assert ema.calls == []  # type: ignore[attr-defined]
    assert sweep.calls == []  # type: ignore[attr-defined]


# --- Return type ------------------------------------------------------------


def test_return_type_is_always_list(monkeypatch, empty_frames) -> None:
    monkeypatch.setattr(dispatcher, "detect_bb_reclaim", _none_stub())
    out = dispatcher.detect_all_setups(
        df_m5=empty_frames[0],
        df_h1=empty_frames[1],
        regime_state=_state("RANGE"),
        structure_state=_stub_structure_state(),
        pair="GBPUSD",
        current_time=_NOW,
    )
    assert isinstance(out, list)
