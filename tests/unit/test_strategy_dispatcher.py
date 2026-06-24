"""Tests for the strategy dispatcher.

Detectors are stubbed via monkeypatch so the dispatcher's day-type
table routing is exercised in isolation from pattern detection.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

import pandas as pd
import pytest

from day_type import DayType
from regime.labels import Direction
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


def _stub_signal(strategy_name: str, day_type: DayType) -> Signal:
    return Signal(
        pair="GBPUSD",
        direction=Direction.BULLISH,
        day_type=day_type,
        strategy_name=strategy_name,  # type: ignore[arg-type]
        suggested_entry_price=1.30000,
        suggested_sl_price=1.29850,
        suggested_tp_price=None,
        confidence_score=0.75,
        source_candle_ts=_NOW,
        invalid_after_candle_ts=compute_invalid_after(_NOW),
        debug={},
    )


def _stub(strategy_name: str) -> Callable[..., object]:
    """Build a stub detect_* that records its call and returns a signal."""
    calls: list[dict] = []

    def _fn(df_m5, df_h1, day_type, structure_state, pair, current_time):  # type: ignore[no-untyped-def]
        calls.append(
            {
                "df_m5_id": id(df_m5),
                "pair": pair,
                "day_type": day_type,
                "structure_state_id": id(structure_state),
            }
        )
        return _stub_signal(strategy_name, day_type)

    _fn.calls = calls  # type: ignore[attr-defined]
    return _fn


def _none_stub() -> Callable[..., object]:
    def _fn(*_a, **_kw):  # type: ignore[no-untyped-def]
        return None

    return _fn


@pytest.fixture
def empty_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    return pd.DataFrame(), pd.DataFrame()


def _patch_table(monkeypatch, **detectors: Callable[..., object]) -> None:
    """Rebuild the DISPATCH table with the given detector overrides.

    The dispatcher's DISPATCH table closes over the imported detector
    functions at module-import time. Monkeypatching the module-level
    names changes future lookups but not the existing tuple. We replace
    the entire table to keep the test self-contained.
    """
    bb = detectors.get("detect_bb_bounce", dispatcher.detect_bb_bounce)
    ema = detectors.get("detect_ema_pullback", dispatcher.detect_ema_pullback)
    news = detectors.get("detect_news", dispatcher.detect_news)
    sb = detectors.get("detect_structure_break", dispatcher.detect_structure_break)
    monkeypatch.setattr(
        dispatcher,
        "DISPATCH",
        {
            DayType.BIG_NEWS_DAY: (news, sb, ema),
            DayType.PRE_BIG_NEWS: (sb, ema),
            DayType.NORMAL: (bb,),
        },
    )


# --- Routing matrix ---------------------------------------------------------


def test_normal_routes_to_bb_bounce(monkeypatch, empty_frames) -> None:
    bb = _stub("bb_bounce")
    _patch_table(monkeypatch, detect_bb_bounce=bb)
    out = dispatcher.detect_all_setups(
        df_m5=empty_frames[0],
        df_h1=empty_frames[1],
        day_type=DayType.NORMAL,
        structure_state=_stub_structure_state(),
        pair="GBPUSD",
        current_time=_NOW,
    )
    assert len(out) == 1
    assert out[0].strategy_name == "bb_bounce"
    assert out[0].day_type == DayType.NORMAL
    assert len(bb.calls) == 1  # type: ignore[attr-defined]


def test_pre_big_news_routes_to_structure_break_and_ema_pullback(
    monkeypatch, empty_frames
) -> None:
    ema = _stub("ema_pullback")
    sb = _stub("structure_break")
    bb = _stub("bb_bounce")
    _patch_table(
        monkeypatch,
        detect_ema_pullback=ema,
        detect_structure_break=sb,
        detect_bb_bounce=bb,
    )
    out = dispatcher.detect_all_setups(
        df_m5=empty_frames[0],
        df_h1=empty_frames[1],
        day_type=DayType.PRE_BIG_NEWS,
        structure_state=_stub_structure_state(),
        pair="GBPUSD",
        current_time=_NOW,
    )
    names = sorted(sig.strategy_name for sig in out)
    assert names == ["ema_pullback", "structure_break"]
    assert all(sig.day_type == DayType.PRE_BIG_NEWS for sig in out)
    assert bb.calls == []  # type: ignore[attr-defined]


def test_big_news_routes_to_news_structure_break_and_ema_pullback(
    monkeypatch, empty_frames
) -> None:
    news = _stub("news")
    sb = _stub("structure_break")
    ema = _stub("ema_pullback")
    bb = _stub("bb_bounce")
    _patch_table(
        monkeypatch,
        detect_news=news,
        detect_structure_break=sb,
        detect_ema_pullback=ema,
        detect_bb_bounce=bb,
    )
    out = dispatcher.detect_all_setups(
        df_m5=empty_frames[0],
        df_h1=empty_frames[1],
        day_type=DayType.BIG_NEWS_DAY,
        structure_state=_stub_structure_state(),
        pair="GBPUSD",
        current_time=_NOW,
    )
    names = sorted(sig.strategy_name for sig in out)
    assert names == ["ema_pullback", "news", "structure_break"]
    assert all(sig.day_type == DayType.BIG_NEWS_DAY for sig in out)
    assert bb.calls == []  # type: ignore[attr-defined]


# --- Stub detectors return None --------------------------------------------


def test_step3_step5_stubs_return_none_in_dispatcher(empty_frames) -> None:
    """detect_news and detect_structure_break stubs always return None.

    With unpatched stubs, BIG_NEWS_DAY effectively becomes
    "ema_pullback only" — and ema_pullback's gates won't pass on the
    empty stub structure either, so the result is an empty list.
    """
    out = dispatcher.detect_all_setups(
        df_m5=empty_frames[0],
        df_h1=empty_frames[1],
        day_type=DayType.BIG_NEWS_DAY,
        structure_state=_stub_structure_state(),
        pair="GBPUSD",
        current_time=_NOW,
    )
    assert out == []


# --- None propagation ------------------------------------------------------


def test_strategy_returns_none_yields_empty_list(monkeypatch, empty_frames) -> None:
    _patch_table(monkeypatch, detect_bb_bounce=_none_stub())
    out = dispatcher.detect_all_setups(
        df_m5=empty_frames[0],
        df_h1=empty_frames[1],
        day_type=DayType.NORMAL,
        structure_state=_stub_structure_state(),
        pair="GBPUSD",
        current_time=_NOW,
    )
    assert out == []


# --- Return type ------------------------------------------------------------


def test_return_type_is_always_list(monkeypatch, empty_frames) -> None:
    _patch_table(monkeypatch, detect_bb_bounce=_none_stub())
    out = dispatcher.detect_all_setups(
        df_m5=empty_frames[0],
        df_h1=empty_frames[1],
        day_type=DayType.NORMAL,
        structure_state=_stub_structure_state(),
        pair="GBPUSD",
        current_time=_NOW,
    )
    assert isinstance(out, list)
