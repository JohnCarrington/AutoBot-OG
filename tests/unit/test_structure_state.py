"""Unit tests for src.structure.state."""
from __future__ import annotations

import pandas as pd
import pytest

from structure.fractals import add_fractal_swings
from structure.state import get_structure_state


def _df_with_swings(
    highs: list[float],
    lows: list[float],
    swing_high_idx: list[int],
    swing_low_idx: list[int],
) -> pd.DataFrame:
    """Synthesise a DataFrame with the swing columns set directly.

    This isolates ``get_structure_state`` tests from the fractal detector:
    the caller specifies exactly which bars are swings.
    """
    n = len(highs)
    assert len(lows) == n
    sh = [i in set(swing_high_idx) for i in range(n)]
    sl = [i in set(swing_low_idx) for i in range(n)]
    return pd.DataFrame(
        {
            "high": highs,
            "low": lows,
            "swing_high": sh,
            "swing_low": sl,
        }
    )


def test_state_returns_dict() -> None:
    df = _df_with_swings(
        highs=[1.0, 2.0, 3.0, 2.0, 1.0],
        lows=[0.0, 0.0, 0.0, 0.0, 0.0],
        swing_high_idx=[],
        swing_low_idx=[],
    )
    state = get_structure_state(df, lookback_bars=10)
    assert set(state.keys()) == {
        "last_swing_high",
        "last_swing_low",
        "swing_high_age_bars",
        "swing_low_age_bars",
        "recent_pattern",
    }


def test_state_insufficient_data_no_swings() -> None:
    df = _df_with_swings(
        highs=[1.0] * 10, lows=[0.0] * 10,
        swing_high_idx=[], swing_low_idx=[],
    )
    state = get_structure_state(df, lookback_bars=10)
    assert state["last_swing_high"] is None
    assert state["last_swing_low"] is None
    assert state["swing_high_age_bars"] is None
    assert state["swing_low_age_bars"] is None
    assert state["recent_pattern"] == "INSUFFICIENT_DATA"


def test_state_insufficient_data_one_of_each() -> None:
    # Only one swing high and one swing low -> can't form a pattern.
    df = _df_with_swings(
        highs=[1.0, 2.0, 5.0, 2.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
        lows=[0.0, 0.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, 0.0],
        swing_high_idx=[2], swing_low_idx=[5],
    )
    state = get_structure_state(df, lookback_bars=10)
    assert state["last_swing_high"] == 5.0
    assert state["last_swing_low"] == -1.0
    assert state["recent_pattern"] == "INSUFFICIENT_DATA"


def test_state_clean_uptrend_last_event_is_high() -> None:
    # Swing highs: index 2 @ 5.0, index 8 @ 7.0  (HH).
    # Swing lows:  index 4 @ -1.0, index 6 @ 0.0 (HL).
    # Most recent overall is the swing high at index 8 -> "HH".
    highs = [1.0, 2.0, 5.0, 3.0, 2.0, 3.0, 4.0, 5.0, 7.0, 6.0]
    lows = [0.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    df = _df_with_swings(
        highs=highs, lows=lows,
        swing_high_idx=[2, 8], swing_low_idx=[4, 6],
    )
    state = get_structure_state(df, lookback_bars=10)
    assert state["recent_pattern"] == "HH"
    assert state["last_swing_high"] == 7.0
    assert state["last_swing_low"] == 0.0
    assert state["swing_high_age_bars"] == 1  # n-1 - 8 = 1
    assert state["swing_low_age_bars"] == 3  # n-1 - 6 = 3


def test_state_clean_uptrend_last_event_is_low() -> None:
    # Same prices but reorder so the last event is a swing low.
    # Swing highs: index 2 @ 5.0, index 6 @ 7.0   (HH)
    # Swing lows:  index 4 @ -1.0, index 8 @ 0.0  (HL)
    # Most recent overall = swing low at index 8 -> "HL".
    highs = [1.0, 2.0, 5.0, 3.0, 2.0, 3.0, 7.0, 4.0, 3.0, 2.0]
    lows = [0.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    df = _df_with_swings(
        highs=highs, lows=lows,
        swing_high_idx=[2, 6], swing_low_idx=[4, 8],
    )
    state = get_structure_state(df, lookback_bars=10)
    assert state["recent_pattern"] == "HL"
    assert state["last_swing_high"] == 7.0
    assert state["last_swing_low"] == 0.0


def test_state_clean_downtrend_last_event_is_high() -> None:
    # Swing highs: index 2 @ 7.0, index 8 @ 5.0   (LH)
    # Swing lows:  index 4 @ -1.0, index 6 @ -2.0 (LL)
    # Most recent overall = swing high at index 8 -> "LH".
    highs = [3.0, 4.0, 7.0, 4.0, 3.0, 3.0, 3.0, 4.0, 5.0, 4.0]
    lows = [0.0, 0.0, 0.0, 0.0, -1.0, -1.5, -2.0, 0.0, 0.0, 0.0]
    df = _df_with_swings(
        highs=highs, lows=lows,
        swing_high_idx=[2, 8], swing_low_idx=[4, 6],
    )
    state = get_structure_state(df, lookback_bars=10)
    assert state["recent_pattern"] == "LH"


def test_state_clean_downtrend_last_event_is_low() -> None:
    # Swing highs: index 2 @ 7.0, index 6 @ 5.0   (LH)
    # Swing lows:  index 4 @ -1.0, index 8 @ -2.0 (LL)
    # Most recent = swing low at index 8 -> "LL".
    highs = [3.0, 4.0, 7.0, 4.0, 3.0, 3.0, 5.0, 3.0, 2.0, 1.0]
    lows = [0.0, 0.0, 0.0, 0.0, -1.0, -1.5, 0.0, -1.0, -2.0, -1.0]
    df = _df_with_swings(
        highs=highs, lows=lows,
        swing_high_idx=[2, 6], swing_low_idx=[4, 8],
    )
    state = get_structure_state(df, lookback_bars=10)
    assert state["recent_pattern"] == "LL"


def test_state_swing_age_correct() -> None:
    # Place a swing high at position 5 in a 12-row frame -> age = 12-1-5 = 6.
    # Place a swing low at position 9 -> age = 12-1-9 = 2.
    df = _df_with_swings(
        highs=[1.0] * 12, lows=[0.0] * 12,
        swing_high_idx=[5], swing_low_idx=[9],
    )
    df.loc[5, "high"] = 10.0
    df.loc[9, "low"] = -10.0
    state = get_structure_state(df, lookback_bars=20)
    assert state["swing_high_age_bars"] == 6
    assert state["swing_low_age_bars"] == 2
    assert state["last_swing_high"] == 10.0
    assert state["last_swing_low"] == -10.0


def test_lookback_bounds_excludes_old_swings() -> None:
    # Two swing highs and two swing lows, all in the first half of a 25-row
    # frame. With lookback_bars=5 the trailing window (positions 20..24)
    # contains no swings -> INSUFFICIENT_DATA. With lookback_bars=20 every
    # swing is included and a pattern can be computed.
    highs = [1.0] * 25
    lows = [0.0] * 25
    highs[5] = 5.0
    highs[10] = 6.0
    lows[7] = -1.0
    lows[12] = 0.5
    df = _df_with_swings(
        highs=highs, lows=lows,
        swing_high_idx=[5, 10], swing_low_idx=[7, 12],
    )

    short = get_structure_state(df, lookback_bars=5)
    assert short["recent_pattern"] == "INSUFFICIENT_DATA"
    # Tail-state fields still populated even when pattern can't be computed.
    assert short["last_swing_high"] == 6.0
    assert short["last_swing_low"] == 0.5

    wide = get_structure_state(df, lookback_bars=20)
    # Most recent event = swing low at 12. Prev low @ 7 = -1.0; current = 0.5
    # -> HL (higher low).
    assert wide["recent_pattern"] == "HL"


def test_state_requires_swing_columns() -> None:
    df = pd.DataFrame({"high": [1.0, 2.0, 3.0], "low": [0.0, 0.5, 1.0]})
    with pytest.raises(ValueError, match="swing_high"):
        get_structure_state(df)


def test_state_lookback_must_be_positive() -> None:
    df = _df_with_swings(
        highs=[1.0, 2.0], lows=[0.0, 0.5],
        swing_high_idx=[], swing_low_idx=[],
    )
    with pytest.raises(ValueError, match="lookback_bars"):
        get_structure_state(df, lookback_bars=0)


def test_state_integrates_with_add_fractal_swings() -> None:
    # End-to-end: feed real OHLC through add_fractal_swings, then ask for
    # the tail state. Series is designed to produce: swing high at i=4,
    # swing low at i=9, swing high at i=14, swing low at i=19.
    highs = [
        1, 2, 3, 4, 10, 5, 4, 3, 2, 1.5,
        2, 3, 4, 5, 12, 6, 5, 4, 3, 2.5,
        3, 4, 5,
    ]
    lows = [
        0, 1, 2, 3, 9, 4, 3, 2, 1, 0.5,
        1, 2, 3, 4, 11, 5, 4, 3, 2, 1.5,
        2, 3, 4,
    ]
    df = pd.DataFrame({"high": [float(x) for x in highs],
                       "low": [float(x) for x in lows]})
    out = add_fractal_swings(df)
    # Confirm setup: there are at least 2 highs and 2 lows.
    assert int(out["swing_high"].sum()) >= 2
    assert int(out["swing_low"].sum()) >= 2

    state = get_structure_state(out, lookback_bars=len(out))
    assert state["recent_pattern"] in {"HH", "HL", "LH", "LL"}
    assert state["last_swing_high"] is not None
    assert state["last_swing_low"] is not None
    assert state["swing_high_age_bars"] is not None
    assert state["swing_low_age_bars"] is not None
