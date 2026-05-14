"""Unit tests for src.regime.engine."""
from __future__ import annotations

import pandas as pd

from regime.engine import RegimeEngine
from regime.labels import Confidence, Direction, RegimeLabel


def _h1(
    *,
    structural_pattern: str = "INSUFFICIENT_DATA",
    slope: float = 0.0,
    bb_width: float = 2.0,
    macd_hist: float = 0.0,
    name: object = None,
) -> pd.Series:
    s = pd.Series(
        {
            "structural_pattern": structural_pattern,
            "ema_slope_norm_50_10": slope,
            "bb_width_norm_20_2": bb_width,
            "macd_hist_12_26_9": macd_hist,
        }
    )
    s.name = name
    return s


def _m5_trend(
    *, slope: float, ema: float, close: float, name: object = None
) -> pd.Series:
    s = pd.Series(
        {
            "ema_slope_norm_50_10": slope,
            "ema_50": ema,
            "close": close,
        }
    )
    s.name = name
    return s


def _m5_range(
    *,
    close: float,
    upper: float,
    lower: float,
    width: float,
    name: object = None,
) -> pd.Series:
    s = pd.Series(
        {
            "close": close,
            "bb_upper_20_2": upper,
            "bb_lower_20_2": lower,
            "bb_width_norm_20_2": width,
        }
    )
    s.name = name
    return s


# --- Initial state ----------------------------------------------------------


def test_initial_state_transition() -> None:
    eng = RegimeEngine()
    assert eng.current_regime == RegimeLabel.TRANSITION
    assert eng.current_direction is None
    assert eng.is_live() is False
    state = eng.get_state()
    assert state["current_regime"] == "TRANSITION"
    assert state["last_regime_change_time"] is None
    assert state["m5_confirmation_count"] == 0


# --- H1 commit + M5 confirmation flow ----------------------------------------


def test_h1_commits_to_trend_pending() -> None:
    eng = RegimeEngine()
    h1 = _h1(structural_pattern="HH+HL", slope=0.45, bb_width=2.0, macd_hist=0.1)
    eng.process_h1_close(h1, prev_h1_row=None)
    # Current stays TRANSITION because TREND needs M5 confirmation.
    assert eng.current_regime == RegimeLabel.TRANSITION
    assert eng.pending_regime == RegimeLabel.TREND
    assert eng.pending_direction == Direction.BULLISH
    assert eng.m5_confirmation_count == 0
    assert eng.is_live() is False


def test_m5_confirms_three_closes() -> None:
    eng = RegimeEngine()
    eng.process_h1_close(
        _h1(structural_pattern="HH+HL", slope=0.45, bb_width=2.0, macd_hist=0.1)
    )
    valid = _m5_trend(slope=0.10, ema=100.0, close=101.0, name="t1")
    eng.process_m5_close(valid)
    assert eng.m5_confirmation_count == 1
    eng.process_m5_close(_m5_trend(slope=0.10, ema=100.0, close=101.0, name="t2"))
    assert eng.m5_confirmation_count == 2
    # Third agreeing M5 close should commit.
    eng.process_m5_close(_m5_trend(slope=0.10, ema=100.0, close=101.0, name="t3"))
    assert eng.current_regime == RegimeLabel.TREND
    assert eng.current_direction == Direction.BULLISH
    assert eng.pending_regime is None
    assert eng.is_live() is True
    assert eng.last_regime_change_time == "t3"


def test_m5_disagreement_resets_counter() -> None:
    eng = RegimeEngine()
    eng.process_h1_close(
        _h1(structural_pattern="HH+HL", slope=0.45, bb_width=2.0, macd_hist=0.1)
    )
    eng.process_m5_close(_m5_trend(slope=0.10, ema=100.0, close=101.0))
    eng.process_m5_close(_m5_trend(slope=0.10, ema=100.0, close=101.0))
    assert eng.m5_confirmation_count == 2
    # Bearish M5 close — doesn't validate bullish pending regime.
    eng.process_m5_close(_m5_trend(slope=-0.10, ema=100.0, close=99.0))
    assert eng.m5_confirmation_count == 0
    # Still pending; not yet live.
    assert eng.pending_regime == RegimeLabel.TREND
    assert eng.current_regime == RegimeLabel.TRANSITION


def test_h1_regime_change_resets_m5() -> None:
    eng = RegimeEngine()
    # Get into TREND first.
    eng.process_h1_close(
        _h1(structural_pattern="HH+HL", slope=0.45, bb_width=2.0, macd_hist=0.1)
    )
    for _ in range(3):
        eng.process_m5_close(_m5_trend(slope=0.10, ema=100.0, close=101.0))
    assert eng.current_regime == RegimeLabel.TREND
    # Now H1 emits RANGE (slope flat, bb compressed).
    eng.process_h1_close(
        _h1(structural_pattern="INSUFFICIENT_DATA", slope=0.05, bb_width=1.4),
        prev_h1_row=_h1(slope=0.10, bb_width=1.4),
    )
    # Pending is RANGE, current still TREND, m5 counter reset to 0.
    assert eng.pending_regime == RegimeLabel.RANGE
    assert eng.current_regime == RegimeLabel.TREND
    assert eng.m5_confirmation_count == 0


# --- Hysteresis --------------------------------------------------------------


def test_hysteresis_trend_entry_exit() -> None:
    eng = RegimeEngine()
    # Enter TREND with slope > 0.35.
    eng.process_h1_close(
        _h1(structural_pattern="INSUFFICIENT_DATA", slope=0.40, bb_width=2.0)
    )
    for _ in range(3):
        eng.process_m5_close(_m5_trend(slope=0.10, ema=100.0, close=101.0))
    assert eng.current_regime == RegimeLabel.TREND

    # Slope dips to 0.25 — still above exit threshold of 0.15 → stay TREND.
    eng.process_h1_close(
        _h1(structural_pattern="INSUFFICIENT_DATA", slope=0.25, bb_width=2.0),
        prev_h1_row=_h1(slope=0.40, bb_width=2.0),
    )
    assert eng.current_regime == RegimeLabel.TREND
    assert eng.pending_regime is None  # no transition pending

    # Slope drops to 0.05 — below 0.15 exit → leave TREND.
    eng.process_h1_close(
        _h1(structural_pattern="INSUFFICIENT_DATA", slope=0.05, bb_width=2.0),
        prev_h1_row=_h1(slope=0.25, bb_width=2.0),
    )
    # The naive classification with slope=0.05 and bb=2.0 is RANGE.
    assert eng.pending_regime == RegimeLabel.RANGE


def test_hysteresis_range_entry_exit() -> None:
    eng = RegimeEngine()
    # Enter RANGE: slope flat AND bb width < 1.8.
    eng.process_h1_close(
        _h1(structural_pattern="INSUFFICIENT_DATA", slope=0.05, bb_width=1.5)
    )
    for _ in range(3):
        eng.process_m5_close(
            _m5_range(close=100.0, upper=101.0, lower=99.0, width=1.5)
        )
    assert eng.current_regime == RegimeLabel.RANGE

    # Slope drifts into the 0.15..0.35 grey band; naive classify_h1 emits
    # TRANSITION ("slope_transitional"). bb_width is still inside the
    # RANGE hysteresis band (<= 2.5) and prev-bar growth is sub-20% so no
    # volatility-expansion override fires. Hysteresis must keep us RANGE.
    eng.process_h1_close(
        _h1(structural_pattern="INSUFFICIENT_DATA", slope=0.20, bb_width=2.0),
        prev_h1_row=_h1(slope=0.05, bb_width=1.9),
    )
    assert eng.current_regime == RegimeLabel.RANGE
    assert eng.pending_regime is None

    # Width jumps to 2.6 — clears the 2.5 exit threshold. classify_h1
    # returns VOLATILE via "range_with_expansion"; the engine commits
    # immediately because VOLATILE bypasses the M5 gate.
    eng.process_h1_close(
        _h1(structural_pattern="INSUFFICIENT_DATA", slope=0.05, bb_width=2.6),
        prev_h1_row=_h1(slope=0.20, bb_width=2.5),
    )
    assert eng.current_regime == RegimeLabel.VOLATILE


# --- VOLATILE behaviour ------------------------------------------------------


def test_volatile_immediately_live_no_m5_gate() -> None:
    eng = RegimeEngine()
    # Structure conflict pushes us straight into VOLATILE.
    eng.process_h1_close(
        _h1(structural_pattern="HH+LL", slope=0.40, bb_width=2.0)
    )
    assert eng.current_regime == RegimeLabel.VOLATILE
    assert eng.pending_regime is None
    assert eng.is_live() is True


def test_volatile_exit_requires_three_quiet_h1_closes() -> None:
    eng = RegimeEngine()
    # Enter VOLATILE.
    eng.process_h1_close(
        _h1(structural_pattern="HH+LL", slope=0.40, bb_width=2.0)
    )
    assert eng.current_regime == RegimeLabel.VOLATILE
    # One quiet H1 — still VOLATILE.
    eng.process_h1_close(
        _h1(structural_pattern="INSUFFICIENT_DATA", slope=0.40, bb_width=2.0),
        prev_h1_row=_h1(slope=0.40, bb_width=2.0),
    )
    assert eng.current_regime == RegimeLabel.VOLATILE
    # Two quiet H1 — still VOLATILE.
    eng.process_h1_close(
        _h1(structural_pattern="INSUFFICIENT_DATA", slope=0.40, bb_width=2.0),
        prev_h1_row=_h1(slope=0.40, bb_width=2.0),
    )
    assert eng.current_regime == RegimeLabel.VOLATILE
    # Three quiet H1 — engine now stages the next regime as pending.
    eng.process_h1_close(
        _h1(structural_pattern="INSUFFICIENT_DATA", slope=0.40, bb_width=2.0),
        prev_h1_row=_h1(slope=0.40, bb_width=2.0),
    )
    # Naive classification was TREND bullish (slope > 0.35).
    assert eng.pending_regime == RegimeLabel.TREND
    assert eng.pending_direction == Direction.BULLISH


# --- State serialisation -----------------------------------------------------


def test_regime_change_time_recorded() -> None:
    eng = RegimeEngine()
    # Commit a VOLATILE regime (commits immediately, so we can read the time).
    eng.process_h1_close(
        _h1(structural_pattern="HH+LL", slope=0.40, bb_width=2.0, name="2025-01-01T00:00:00")
    )
    state = eng.get_state()
    assert state["last_regime_change_time"] == "2025-01-01T00:00:00"


def test_debug_dict_populated() -> None:
    eng = RegimeEngine()
    h1 = _h1(structural_pattern="HH+HL", slope=0.40, bb_width=2.0, macd_hist=0.05)
    eng.process_h1_close(h1)
    debug = eng.get_state()["debug"]
    assert debug["slope_norm"] == 0.40
    assert debug["bb_width_norm"] == 2.0
    assert debug["macd_hist"] == 0.05
    assert debug["naive_regime"] == "TREND"
    assert debug["naive_direction"] == "BULLISH"
    assert debug["naive_reason"] == "classified"
    assert debug["structural_pattern"] == "HH+HL"


def test_confidence_is_exposed_via_engine_attribute() -> None:
    # Confidence is not in RegimeState, but the engine attribute is
    # the contract used by the applier output.
    eng = RegimeEngine()
    eng.process_h1_close(
        _h1(structural_pattern="HH+HL", slope=0.40, bb_width=2.0, macd_hist=0.10)
    )
    assert eng.pending_confidence == Confidence.HIGH


def test_get_state_keys_match_typeddict() -> None:
    eng = RegimeEngine()
    state = eng.get_state()
    assert set(state.keys()) == {
        "current_regime",
        "current_direction",
        "pending_regime",
        "m5_confirmation_count",
        "last_regime_change_time",
        "reason",
        "debug",
    }
