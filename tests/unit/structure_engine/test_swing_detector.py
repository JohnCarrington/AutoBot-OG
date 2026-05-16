"""Unit tests for :mod:`structure_engine.swing_detector`."""
from __future__ import annotations

import pandas as pd
import pytest

from structure_engine.swing_detector import detect_swings

from .conftest import make_ohlc


def _pyramid(*, peak_idx: int, n: int, base: float = 1.30000, peak_delta: float = 0.0020) -> list[dict]:
    """Build ``n`` rows where bar ``peak_idx`` is the highest high."""
    rows = []
    for i in range(n):
        offset = abs(i - peak_idx)
        high_off = peak_delta - offset * 0.0001
        rows.append(
            {
                "open": base, "close": base,
                "high": base + high_off, "low": base - 0.0003,
                "atr_14": 0.0010,
            }
        )
    return rows


def _valley(*, trough_idx: int, n: int, base: float = 1.30000, trough_delta: float = 0.0020) -> list[dict]:
    rows = []
    for i in range(n):
        offset = abs(i - trough_idx)
        low_off = trough_delta - offset * 0.0001
        rows.append(
            {
                "open": base, "close": base,
                "high": base + 0.0003, "low": base - low_off,
                "atr_14": 0.0010,
            }
        )
    return rows


def test_detect_swing_high_h1_window_2() -> None:
    """A clean peak with 2 lower bars on each side is detected as HIGH."""
    df = make_ohlc(_pyramid(peak_idx=2, n=5))
    swings = detect_swings(df, "H1")
    assert any(s.type == "HIGH" and s.bar_index == 2 for s in swings)


def test_detect_swing_low_m5_window_3() -> None:
    """M5 default window is 3 — peak needs 3 higher bars each side."""
    df = make_ohlc(_valley(trough_idx=3, n=7))
    swings = detect_swings(df, "M5")
    assert any(s.type == "LOW" and s.bar_index == 3 for s in swings)


def test_swing_requires_strict_inequality() -> None:
    """Flat plateau (equal high) is not a swing."""
    rows = [
        {"high": 1.30010, "low": 1.29990, "open": 1.30000, "close": 1.30000},
        {"high": 1.30020, "low": 1.29990, "open": 1.30000, "close": 1.30000},
        {"high": 1.30020, "low": 1.29990, "open": 1.30000, "close": 1.30000},  # tie at peak
        {"high": 1.30020, "low": 1.29990, "open": 1.30000, "close": 1.30000},
        {"high": 1.30010, "low": 1.29990, "open": 1.30000, "close": 1.30000},
    ]
    df = make_ohlc(rows)
    swings = detect_swings(df, "H1")
    # No strict swing high — ties disqualify under spec §5.
    assert all(s.type != "HIGH" or s.bar_index != 2 for s in swings)


def test_last_window_bars_never_emitted() -> None:
    """The final ``window`` bars cannot be confirmed swings."""
    df = make_ohlc(_pyramid(peak_idx=4, n=5))  # peak at very end → no confirm
    swings = detect_swings(df, "H1")
    assert all(s.bar_index < 5 - 2 for s in swings)  # window=2 for H1


def test_empty_dataframe_returns_empty_list() -> None:
    assert detect_swings(pd.DataFrame(), "H1") == []


def test_short_dataframe_returns_empty_list() -> None:
    df = make_ohlc(_pyramid(peak_idx=1, n=3))  # too short for window=2
    assert detect_swings(df, "H1") == []


def test_unknown_timeframe_returns_empty_list() -> None:
    df = make_ohlc(_pyramid(peak_idx=2, n=5))
    # Type-checker would reject this, but the runtime guards against it.
    assert detect_swings(df, "FOO") == []  # type: ignore[arg-type]


def test_strength_is_bounded_0_to_1() -> None:
    df = make_ohlc(_pyramid(peak_idx=2, n=5))
    swings = detect_swings(df, "H1")
    for s in swings:
        assert 0.0 <= s.strength <= 1.0
