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


# --- Adversarial-review regression tests (C1, C2, H1, H2, H3, H5) -----------


def test_m5_counter_survives_repeated_h1_emits_same_regime() -> None:
    """C1: H1 re-emitting the same pending regime must NOT reset the counter.

    Reproduces the bug described in the review: with the M5 counter at 2
    and a second identical H1 close arriving, the engine used to reset
    the counter to 0, making a confirm-on-next-M5 case effectively
    impossible to satisfy on choppy data.
    """
    eng = RegimeEngine()
    eng.process_h1_close(
        _h1(structural_pattern="HH+HL", slope=0.45, bb_width=2.0, macd_hist=0.1)
    )
    assert eng.pending_regime == RegimeLabel.TREND
    assert eng.m5_confirmation_count == 0

    eng.process_m5_close(_m5_trend(slope=0.10, ema=100.0, close=101.0))
    eng.process_m5_close(_m5_trend(slope=0.10, ema=100.0, close=101.0))
    assert eng.m5_confirmation_count == 2

    # Second H1 close with identical inputs — pending is unchanged.
    # Counter MUST be preserved.
    eng.process_h1_close(
        _h1(structural_pattern="HH+HL", slope=0.45, bb_width=2.0, macd_hist=0.1),
        prev_h1_row=_h1(slope=0.45, bb_width=2.0),
    )
    assert eng.pending_regime == RegimeLabel.TREND
    assert eng.pending_direction == Direction.BULLISH
    assert eng.m5_confirmation_count == 2  # critical: not reset

    # One more agreeing M5 close commits.
    eng.process_m5_close(_m5_trend(slope=0.10, ema=100.0, close=101.0, name="t3"))
    assert eng.current_regime == RegimeLabel.TREND
    assert eng.is_live() is True


def test_nan_indicator_preserves_current_regime() -> None:
    """C2: a NaN-indicator H1 close must NOT demote a committed regime.

    Reproduces the bug: a single missing-slope row would stage
    pending=TRANSITION and (with the H5 bug) commit it via the no-op M5
    gate, dropping a real TREND on the floor.
    """
    eng = RegimeEngine()
    # Get into a committed TREND first.
    eng.process_h1_close(
        _h1(structural_pattern="HH+HL", slope=0.45, bb_width=2.0, macd_hist=0.1)
    )
    for _ in range(3):
        eng.process_m5_close(_m5_trend(slope=0.10, ema=100.0, close=101.0))
    assert eng.current_regime == RegimeLabel.TREND
    assert eng.current_direction == Direction.BULLISH

    # Now an H1 with NaN slope — classifier returns "insufficient_indicator_data".
    eng.process_h1_close(
        _h1(slope=float("nan"), bb_width=float("nan")),
        prev_h1_row=_h1(slope=0.45, bb_width=2.0),
    )
    # Committed regime preserved; no pending downgrade staged.
    assert eng.current_regime == RegimeLabel.TREND
    assert eng.current_direction == Direction.BULLISH
    assert eng.pending_regime is None
    assert eng.m5_confirmation_count == 0
    # Reason surfaces the no-op so it is visible in diagnostics.
    assert eng.reason == "insufficient_indicator_data"


def test_nan_indicator_on_fresh_engine_stays_transition() -> None:
    """C2 edge: NaN on a never-committed engine leaves it in TRANSITION."""
    eng = RegimeEngine()
    eng.process_h1_close(_h1(slope=float("nan"), bb_width=float("nan")))
    assert eng.current_regime == RegimeLabel.TRANSITION
    assert eng.pending_regime is None
    assert eng.reason == "insufficient_indicator_data"
    assert eng.is_live() is False


def test_slope_sign_flip_exits_trend() -> None:
    """H1: direct sign flip on a committed TREND routes through VOLATILE.

    A sudden +/- flip is more likely a whipsaw or news shock than a clean
    regime change. The engine commits VOLATILE immediately (no M5 gate),
    and the standard VOLATILE_EXIT_QUIET_H1_BARS cooldown then governs
    when (and how) the opposite direction is eventually accepted via the
    normal hysteresis + M5 confirmation path.
    """
    eng = RegimeEngine()
    eng.process_h1_close(
        _h1(structural_pattern="INSUFFICIENT_DATA", slope=0.40, bb_width=2.0)
    )
    for _ in range(3):
        eng.process_m5_close(_m5_trend(slope=0.10, ema=100.0, close=101.0))
    assert eng.current_regime == RegimeLabel.TREND
    assert eng.current_direction == Direction.BULLISH

    # Slope flips sign from +0.40 to -0.40 — would-be naive is
    # TREND/BEARISH but the engine demotes that to VOLATILE.
    eng.process_h1_close(
        _h1(structural_pattern="INSUFFICIENT_DATA", slope=-0.40, bb_width=2.0),
        prev_h1_row=_h1(slope=0.40, bb_width=2.0),
    )
    # VOLATILE commits immediately (no M5 gate); the previous bullish bias
    # is discarded — VOLATILE is direction-less here.
    assert eng.current_regime == RegimeLabel.VOLATILE
    assert eng.current_direction is None
    assert eng.pending_regime is None
    assert eng.reason == "slope_sign_flip"
    # is_live remains True — VOLATILE is executable for sweep strategies.
    assert eng.is_live() is True


def test_slope_sign_flip_then_quiet_eventually_accepts_opposite_trend() -> None:
    """H1 + N1 follow-through with realistic interleaved H1/M5 sequence.

    After a sign-flip routes through VOLATILE, the recovery requires:
    - VOLATILE_EXIT_QUIET_H1_BARS quiet H1 closes → fall-through stages
      pending=TREND/BEARISH
    - 3 agreeing M5 closes → pending committed

    Crucially, those 3 M5 confirmations may arrive across **multiple H1
    windows**. The interleaved H1 closes during recovery must NOT wipe
    the staged pending (N1 fix) and must NOT reset the M5 counter.
    """
    eng = RegimeEngine()
    # Commit TREND bullish.
    eng.process_h1_close(
        _h1(structural_pattern="INSUFFICIENT_DATA", slope=0.40, bb_width=2.0)
    )
    for _ in range(3):
        eng.process_m5_close(_m5_trend(slope=0.10, ema=100.0, close=101.0))
    assert eng.current_regime == RegimeLabel.TREND

    # Sign-flip bar -> VOLATILE.
    eng.process_h1_close(
        _h1(structural_pattern="INSUFFICIENT_DATA", slope=-0.40, bb_width=2.0),
        prev_h1_row=_h1(slope=0.40, bb_width=2.0),
    )
    assert eng.current_regime == RegimeLabel.VOLATILE
    assert eng.reason == "slope_sign_flip"

    quiet_bear = _h1(
        structural_pattern="INSUFFICIENT_DATA", slope=-0.40, bb_width=2.0
    )
    bearish_m5 = _m5_trend(slope=-0.10, ema=100.0, close=99.0)

    # Three quiet H1 closes → fall-through stages pending=TREND/BEARISH.
    for _ in range(3):
        eng.process_h1_close(quiet_bear, prev_h1_row=quiet_bear)
    assert eng.pending_regime == RegimeLabel.TREND
    assert eng.pending_direction == Direction.BEARISH
    assert eng.m5_confirmation_count == 0

    # Interleaved recovery: M5, H1, M5, H1, M5 — N1 says pending and
    # counter must both survive every interleaved H1.
    eng.process_m5_close(bearish_m5)
    assert eng.m5_confirmation_count == 1
    assert eng.pending_regime == RegimeLabel.TREND

    # H1 #N+1 during recovery — would have wiped pending before N1 fix.
    eng.process_h1_close(quiet_bear, prev_h1_row=quiet_bear)
    assert eng.pending_regime == RegimeLabel.TREND, "N1: pending wiped"
    assert eng.pending_direction == Direction.BEARISH
    assert eng.m5_confirmation_count == 1, "N1: counter reset"
    # The engine is still in committed VOLATILE — strategies remain live.
    assert eng.current_regime == RegimeLabel.VOLATILE
    assert eng.is_live() is True

    eng.process_m5_close(bearish_m5)
    assert eng.m5_confirmation_count == 2

    # H1 #N+2 — pending and counter again must survive.
    eng.process_h1_close(quiet_bear, prev_h1_row=quiet_bear)
    assert eng.pending_regime == RegimeLabel.TREND
    assert eng.m5_confirmation_count == 2

    # Final agreeing M5 → commit across multi-H1-window confirmation.
    eng.process_m5_close(bearish_m5)
    assert eng.current_regime == RegimeLabel.TREND
    assert eng.current_direction == Direction.BEARISH
    assert eng.is_live() is True


def test_oscillating_sign_flip_does_not_stick_in_volatile() -> None:
    """N2: alternating H1 slope signs during cooldown still allow recovery
    once a quiet period emerges and M5 confirmations arrive.

    The VOLATILE quiet counter is directional-agnostic — it advances on
    *any* non-VOLATILE naive emission. With N1 fixed, a pending staged
    by fall-through survives subsequent H1 closes long enough for M5
    confirmations to commit it. Before N1 was fixed, oscillating slope
    could trap the engine in VOLATILE permanently.
    """
    eng = RegimeEngine()
    # Commit TREND bullish first.
    eng.process_h1_close(
        _h1(structural_pattern="INSUFFICIENT_DATA", slope=0.40, bb_width=2.0)
    )
    for _ in range(3):
        eng.process_m5_close(_m5_trend(slope=0.10, ema=100.0, close=101.0))
    assert eng.current_regime == RegimeLabel.TREND

    bull = _h1(structural_pattern="INSUFFICIENT_DATA", slope=0.40, bb_width=2.0)
    bear = _h1(structural_pattern="INSUFFICIENT_DATA", slope=-0.40, bb_width=2.0)
    bullish_m5 = _m5_trend(slope=0.10, ema=100.0, close=101.0)

    # Sign-flip → VOLATILE.
    eng.process_h1_close(bear, prev_h1_row=bull)
    assert eng.current_regime == RegimeLabel.VOLATILE

    # Oscillating quiet bars: bull, bear, bull. The third bar's vote
    # (bullish) is staged at fall-through.
    eng.process_h1_close(bull, prev_h1_row=bear)
    eng.process_h1_close(bear, prev_h1_row=bull)
    eng.process_h1_close(bull, prev_h1_row=bear)
    assert eng.pending_regime == RegimeLabel.TREND
    assert eng.pending_direction == Direction.BULLISH

    # Slope stabilises bullish; M5 confirms across interleaved H1.
    eng.process_m5_close(bullish_m5)
    eng.process_m5_close(bullish_m5)
    # Interleaved H1 (same direction as pending) — N1: pending survives.
    eng.process_h1_close(bull, prev_h1_row=bull)
    assert eng.pending_regime == RegimeLabel.TREND
    assert eng.m5_confirmation_count == 2
    eng.process_m5_close(bullish_m5)

    # The engine has accepted a direction — N2 (stuck-in-VOLATILE) is
    # no longer reachable once N1 is patched.
    assert eng.current_regime == RegimeLabel.TREND
    assert eng.current_direction == Direction.BULLISH


def test_is_live_during_pending_downgrade() -> None:
    """H2: a committed regime remains live while a downgrade is pending."""
    eng = RegimeEngine()
    # Commit TREND.
    eng.process_h1_close(
        _h1(structural_pattern="HH+HL", slope=0.45, bb_width=2.0, macd_hist=0.1)
    )
    for _ in range(3):
        eng.process_m5_close(_m5_trend(slope=0.10, ema=100.0, close=101.0))
    assert eng.current_regime == RegimeLabel.TREND
    assert eng.is_live() is True

    # H1 emits a flat-slope downgrade — pending=RANGE is staged.
    eng.process_h1_close(
        _h1(structural_pattern="INSUFFICIENT_DATA", slope=0.05, bb_width=1.4),
        prev_h1_row=_h1(slope=0.10, bb_width=1.4),
    )
    assert eng.pending_regime == RegimeLabel.RANGE
    assert eng.current_regime == RegimeLabel.TREND
    # The committed regime is still in force — strategies keep trading TREND.
    assert eng.is_live() is True


def test_is_live_initial_state_remains_false() -> None:
    """H2 regression: fresh engine + an unconfirmed pending is not live."""
    eng = RegimeEngine()
    assert eng.is_live() is False
    eng.process_h1_close(
        _h1(structural_pattern="HH+HL", slope=0.45, bb_width=2.0, macd_hist=0.1)
    )
    # pending=TREND but nothing committed yet -> NOT live.
    assert eng.pending_regime == RegimeLabel.TREND
    assert eng.current_regime == RegimeLabel.TRANSITION
    assert eng.is_live() is False


def test_range_hysteresis_breaks_for_trend_emergence() -> None:
    """H3: a TREND with structure/direction breaks the RANGE-sticky lock.

    With the old logic, a clean HH+HL print with strong slope was held
    inside RANGE because ``bb_width`` had not yet crossed the upper exit
    threshold. Hysteresis must now defer to a direction-bearing TREND.
    """
    eng = RegimeEngine()
    # Enter RANGE.
    eng.process_h1_close(
        _h1(structural_pattern="INSUFFICIENT_DATA", slope=0.05, bb_width=1.5)
    )
    for _ in range(3):
        eng.process_m5_close(
            _m5_range(close=100.0, upper=101.0, lower=99.0, width=1.5)
        )
    assert eng.current_regime == RegimeLabel.RANGE

    # H1 prints HH+HL with strong slope; bb_width 1.85 is still inside the
    # RANGE hysteresis band (<= 2.5). Old code held us in RANGE — new code
    # must stage TREND.
    eng.process_h1_close(
        _h1(structural_pattern="HH+HL", slope=0.50, bb_width=1.85, macd_hist=0.1),
        prev_h1_row=_h1(slope=0.05, bb_width=1.70),
    )
    assert eng.pending_regime == RegimeLabel.TREND
    assert eng.pending_direction == Direction.BULLISH


def test_m3_volatile_direction_clears_on_directionless_bar() -> None:
    """M3 carry-forward: ``current_direction`` must update every VOLATILE bar.

    Before this fix, the "stay VOLATILE" branch only refreshed
    ``current_direction`` when ``naive_dir`` was non-None — meaning a
    ``volatility_expansion`` bar (which carries the trend candidate's
    direction) followed by a ``structure_conflict`` bar (which carries
    ``None``) left ``current_direction`` claiming the prior bias for the
    rest of the VOLATILE state. The fix drops the conditional so a
    directionless VOLATILE bar properly clears the direction.
    """
    eng = RegimeEngine()

    # H1 #1: HH+HL with a 33% bar-on-bar BB-width jump → classifier
    # emits (VOLATILE, BULLISH, "volatility_expansion"). VOLATILE commits
    # immediately, carrying BULLISH from the underlying TREND candidate.
    prev = _h1(structural_pattern="HH+HL", slope=0.45, bb_width=1.5)
    expansion = _h1(structural_pattern="HH+HL", slope=0.45, bb_width=2.0)
    eng.process_h1_close(expansion, prev_h1_row=prev)
    assert eng.current_regime == RegimeLabel.VOLATILE
    assert eng.current_direction == Direction.BULLISH
    assert eng.reason == "volatility_expansion"

    # H1 #2: HH+LL structure conflict → classifier emits
    # (VOLATILE, None, "structure_conflict"). The "stay VOLATILE" branch
    # fires; current_direction MUST clear to None (M3 fix).
    eng.process_h1_close(
        _h1(structural_pattern="HH+LL", slope=0.45, bb_width=2.0)
    )
    assert eng.current_regime == RegimeLabel.VOLATILE
    assert eng.current_direction is None
    assert eng.reason == "structure_conflict"


def test_m5_validates_rejects_transition() -> None:
    """H5: the M5 validator must refuse a pending=TRANSITION on principle.

    With C2 in place, TRANSITION should never be staged as pending — but
    this guard prevents a future code path from sneaking a no-op
    confirmation through.
    """
    eng = RegimeEngine()
    # Bypass the C2 guard by manually staging TRANSITION (defensive test).
    eng.pending_regime = RegimeLabel.TRANSITION
    eng.pending_direction = None
    eng.m5_confirmation_count = 0
    m5 = _m5_trend(slope=0.0, ema=100.0, close=100.0)
    # Should NOT increment the counter and should NOT commit.
    eng.process_m5_close(m5)
    eng.process_m5_close(m5)
    eng.process_m5_close(m5)
    assert eng.current_regime == RegimeLabel.TRANSITION
    # Counter remains 0 — _m5_validates returned False every time.
    assert eng.m5_confirmation_count == 0
