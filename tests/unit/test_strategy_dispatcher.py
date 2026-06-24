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
from common import Direction
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


def test_big_news_inside_window_runs_news_only(
    monkeypatch, empty_frames
) -> None:
    """Step 5b: on BIG_NEWS_DAY inside a HIGH-impact release window,
    only ``detect_news`` runs; structure_break / ema_pullback are
    muted."""
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
    monkeypatch.setattr(dispatcher, "is_in_release_window", lambda *a, **k: True)
    out = dispatcher.detect_all_setups(
        df_m5=empty_frames[0],
        df_h1=empty_frames[1],
        day_type=DayType.BIG_NEWS_DAY,
        structure_state=_stub_structure_state(),
        pair="GBPUSD",
        current_time=_NOW,
    )
    names = sorted(sig.strategy_name for sig in out)
    assert names == ["news"]
    assert all(sig.day_type == DayType.BIG_NEWS_DAY for sig in out)
    assert sb.calls == []  # type: ignore[attr-defined]
    assert ema.calls == []  # type: ignore[attr-defined]
    assert bb.calls == []  # type: ignore[attr-defined]


def test_big_news_outside_window_runs_structure_only(
    monkeypatch, empty_frames
) -> None:
    """Step 5b: on BIG_NEWS_DAY outside any release window, only the
    structure detectors run; ``detect_news`` is skipped (no fresh
    release to read)."""
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
    monkeypatch.setattr(dispatcher, "is_in_release_window", lambda *a, **k: False)
    out = dispatcher.detect_all_setups(
        df_m5=empty_frames[0],
        df_h1=empty_frames[1],
        day_type=DayType.BIG_NEWS_DAY,
        structure_state=_stub_structure_state(),
        pair="GBPUSD",
        current_time=_NOW,
    )
    names = sorted(sig.strategy_name for sig in out)
    assert names == ["ema_pullback", "structure_break"]
    assert all(sig.day_type == DayType.BIG_NEWS_DAY for sig in out)
    assert news.calls == []  # type: ignore[attr-defined]
    assert bb.calls == []  # type: ignore[attr-defined]


def test_pre_big_news_never_window_suppressed(
    monkeypatch, empty_frames
) -> None:
    """Step 5b: PRE_BIG_NEWS is NEVER window-suppressed — both
    structure detectors run regardless of where the clock sits."""
    sb = _stub("structure_break")
    ema = _stub("ema_pullback")
    news = _stub("news")
    _patch_table(
        monkeypatch,
        detect_structure_break=sb,
        detect_ema_pullback=ema,
        detect_news=news,
    )
    # Even with the window predicate forced True, PRE_BIG_NEWS branch
    # must not consult it.
    monkeypatch.setattr(dispatcher, "is_in_release_window", lambda *a, **k: True)
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
    assert news.calls == []  # type: ignore[attr-defined]


def test_normal_never_window_suppressed(monkeypatch, empty_frames) -> None:
    """Step 5b: NORMAL is NEVER window-suppressed."""
    bb = _stub("bb_bounce")
    _patch_table(monkeypatch, detect_bb_bounce=bb)
    monkeypatch.setattr(dispatcher, "is_in_release_window", lambda *a, **k: True)
    out = dispatcher.detect_all_setups(
        df_m5=empty_frames[0],
        df_h1=empty_frames[1],
        day_type=DayType.NORMAL,
        structure_state=_stub_structure_state(),
        pair="GBPUSD",
        current_time=_NOW,
    )
    assert [sig.strategy_name for sig in out] == ["bb_bounce"]


# --- Stub detectors return None --------------------------------------------


def test_unpatched_dispatch_with_neutral_structure_yields_empty(
    empty_frames,
) -> None:
    """With the real DISPATCH table and a neutral / UNKNOWN structure
    state, no detector's gates pass.

    Step 5b: on BIG_NEWS_DAY in a fresh test process the news_calendar
    cache is empty, so ``is_in_release_window`` returns False and the
    active set is the two structure detectors. Both gate on
    ``TREND_CONTINUATION`` + a directional ``htf_bias``, neither of
    which the stub state provides — so the result is the empty list.
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


# --- Real-table identity (flagged in step 2b) -----------------------------


def test_real_dispatch_table_maps_to_real_detectors() -> None:
    """The REAL ``dispatcher.DISPATCH`` (not a monkeypatched one) routes
    each DayType to the real detector functions.

    Catches a fat-fingered table edit — every other dispatcher test
    rebuilds the table via ``_patch_table`` and so cannot notice the
    actual module-level tuple regressing. After step 5b:

    - NORMAL → (detect_bb_bounce,)
    - PRE_BIG_NEWS → (detect_structure_break, detect_ema_pullback)
    - BIG_NEWS_DAY → (detect_news, detect_structure_break,
                       detect_ema_pullback)  — the *potential* set;
                       the active subset on BIG_NEWS_DAY depends on
                       the release-window predicate (see
                       ``test_big_news_inside_window_runs_news_only`` /
                       ``test_big_news_outside_window_runs_structure_only``).

    All four are real strategy modules — ``detect_news`` (step 5b)
    replaced the previous return-None stub.
    """
    from strategies.bb_bounce import detect_bb_bounce as real_bb
    from strategies.ema_pullback import detect_ema_pullback as real_ema
    from strategies.news import detect_news as real_news
    from strategies.structure_break import (
        detect_structure_break as real_sb,
    )

    assert dispatcher.DISPATCH[DayType.NORMAL] == (real_bb,)
    assert dispatcher.DISPATCH[DayType.PRE_BIG_NEWS] == (
        real_sb,
        real_ema,
    )
    assert dispatcher.DISPATCH[DayType.BIG_NEWS_DAY] == (
        real_news,
        real_sb,
        real_ema,
    )
    assert dispatcher.detect_news.__module__ == "strategies.news"
    assert real_sb.__module__ == "strategies.structure_break"


# --- B-5 split mutual exclusion (step 3b) ---------------------------------


def _split_test_m5():
    """Minimal M5 frame with ATR for the live detectors."""
    import pandas as pd
    idx = pd.DatetimeIndex(
        [_NOW.replace(minute=m) for m in (50, 55, 0)]
    )
    return pd.DataFrame(
        [
            {"open": 1.30050, "high": 1.30060, "low": 1.30040,
             "close": 1.30050, "atr_14": 0.0020},
            {"open": 1.30050, "high": 1.30060, "low": 1.30040,
             "close": 1.30050, "atr_14": 0.0020},
            {"open": 1.30050, "high": 1.30060, "low": 1.30040,
             "close": 1.30050, "atr_14": 0.0020},
        ],
        index=idx,
    )


def _split_test_h1(macd_hist: float = -0.10):
    import pandas as pd
    return pd.DataFrame([{"macd_hist_12_26_9": macd_hist}])


def _split_level(side: str, price: float):
    from structure_engine import StructureLevel
    return StructureLevel(
        pair="GBPUSD",
        level_type=("SUPPORT" if side == "LOW" else "RESISTANCE"),
        price=price,
        zone_low=price - 0.0004,
        zone_high=price + 0.0004,
        timeframe="H1",
        score=7.0,
        touch_count=2,
        last_touched_ts=None,
        source="swing_h1",
        debug={},
    )


def _split_structure(*, htf_bias: str, reaction: str, acceptance_state: str):
    return StructureState(
        pair="GBPUSD",
        timestamp=_NOW.isoformat(),
        is_valid=True,
        htf_bias=htf_bias,  # type: ignore[arg-type]
        local_bias=htf_bias,  # type: ignore[arg-type]
        nearest_support=_split_level("LOW", 1.30000),
        nearest_resistance=_split_level("HIGH", 1.30200),
        liquidity_above=None,
        liquidity_below=None,
        current_reaction=reaction,  # type: ignore[arg-type]
        acceptance_state=acceptance_state,  # type: ignore[arg-type]
        structure_mode="TREND_CONTINUATION",
        confidence=0.7,
        reason="test",
        levels=[],
        debug={},
    )


def test_acceptance_break_fires_structure_break_only() -> None:
    """3b mutual exclusion: a SUPPORT_ACCEPTANCE_BREAK reaction routes
    to structure_break alone; ema_pullback returns None on it."""
    from strategies.ema_pullback import detect_ema_pullback
    from strategies.structure_break import detect_structure_break

    state = _split_structure(
        htf_bias="BEARISH",
        reaction="SUPPORT_ACCEPTANCE_BREAK",
        acceptance_state="ACCEPTED_BELOW_SUPPORT",
    )
    sb_sig = detect_structure_break(
        _split_test_m5(), _split_test_h1(), DayType.BIG_NEWS_DAY,
        state, "GBPUSD", _NOW,
    )
    ema_sig = detect_ema_pullback(
        _split_test_m5(), _split_test_h1(), DayType.BIG_NEWS_DAY,
        state, "GBPUSD", _NOW,
    )
    assert sb_sig is not None
    assert sb_sig.strategy_name == "structure_break"
    assert ema_sig is None


def test_failed_reclaim_fires_ema_pullback_only() -> None:
    """3b mirror: a FAILED_RECLAIM_BELOW_SUPPORT reaction routes to
    ema_pullback alone; structure_break returns None on it."""
    from strategies.ema_pullback import detect_ema_pullback
    from strategies.structure_break import detect_structure_break

    state = _split_structure(
        htf_bias="BEARISH",
        reaction="FAILED_RECLAIM_BELOW_SUPPORT",
        acceptance_state="REJECTED_BELOW_SUPPORT",
    )
    sb_sig = detect_structure_break(
        _split_test_m5(), _split_test_h1(), DayType.BIG_NEWS_DAY,
        state, "GBPUSD", _NOW,
    )
    ema_sig = detect_ema_pullback(
        _split_test_m5(), _split_test_h1(), DayType.BIG_NEWS_DAY,
        state, "GBPUSD", _NOW,
    )
    assert ema_sig is not None
    assert ema_sig.strategy_name == "ema_pullback"
    assert sb_sig is None


def test_bullish_acceptance_break_fires_structure_break_only() -> None:
    """3b mutual exclusion (BULLISH mirror)."""
    from strategies.ema_pullback import detect_ema_pullback
    from strategies.structure_break import detect_structure_break

    state = _split_structure(
        htf_bias="BULLISH",
        reaction="RESISTANCE_ACCEPTANCE_BREAK",
        acceptance_state="ACCEPTED_ABOVE_RESISTANCE",
    )
    sb_sig = detect_structure_break(
        _split_test_m5(), _split_test_h1(macd_hist=0.10),
        DayType.PRE_BIG_NEWS, state, "GBPUSD", _NOW,
    )
    ema_sig = detect_ema_pullback(
        _split_test_m5(), _split_test_h1(macd_hist=0.10),
        DayType.PRE_BIG_NEWS, state, "GBPUSD", _NOW,
    )
    assert sb_sig is not None
    assert sb_sig.strategy_name == "structure_break"
    assert ema_sig is None


def test_bullish_failed_reclaim_fires_ema_pullback_only() -> None:
    """3b mirror (BULLISH FAILED_RECLAIM)."""
    from strategies.ema_pullback import detect_ema_pullback
    from strategies.structure_break import detect_structure_break

    state = _split_structure(
        htf_bias="BULLISH",
        reaction="FAILED_RECLAIM_ABOVE_RESISTANCE",
        acceptance_state="REJECTED_ABOVE_RESISTANCE",
    )
    sb_sig = detect_structure_break(
        _split_test_m5(), _split_test_h1(macd_hist=0.10),
        DayType.PRE_BIG_NEWS, state, "GBPUSD", _NOW,
    )
    ema_sig = detect_ema_pullback(
        _split_test_m5(), _split_test_h1(macd_hist=0.10),
        DayType.PRE_BIG_NEWS, state, "GBPUSD", _NOW,
    )
    assert ema_sig is not None
    assert ema_sig.strategy_name == "ema_pullback"
    assert sb_sig is None


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
