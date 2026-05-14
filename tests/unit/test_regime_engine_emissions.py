"""Tests for the RegimeEngine emission-log extension (Phase 4 hook)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from regime.engine import EMISSION_LOG_MAXLEN, RegimeEmission, RegimeEngine
from regime.labels import Direction, RegimeLabel


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


def _m5_trend(*, slope: float, ema: float, close: float, name=None) -> pd.Series:
    s = pd.Series(
        {"ema_slope_norm_50_10": slope, "ema_50": ema, "close": close}
    )
    s.name = name
    return s


# --- Emission shape ----------------------------------------------------------


def test_emission_is_logged_on_every_h1_close() -> None:
    eng = RegimeEngine()
    ts = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    eng.process_h1_close(
        _h1(structural_pattern="HH+HL", slope=0.45, bb_width=2.0, name=ts)
    )
    assert len(eng._emission_log) == 1
    e = eng._emission_log[-1]
    assert isinstance(e, RegimeEmission)
    assert e.timestamp == ts
    assert e.kind == "H1"


def test_emission_is_logged_on_every_m5_close() -> None:
    eng = RegimeEngine()
    h1_ts = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    eng.process_h1_close(
        _h1(structural_pattern="HH+HL", slope=0.45, bb_width=2.0, name=h1_ts)
    )
    m5_ts = h1_ts + timedelta(minutes=5)
    eng.process_m5_close(
        _m5_trend(slope=0.10, ema=100.0, close=101.0, name=m5_ts)
    )
    assert len(eng._emission_log) == 2
    assert eng._emission_log[-1].kind == "M5"
    assert eng._emission_log[-1].timestamp == m5_ts


def test_emission_committed_flag_only_true_when_regime_changes() -> None:
    eng = RegimeEngine()
    # First H1: stage pending=TREND, current still TRANSITION → not a commit.
    eng.process_h1_close(
        _h1(
            structural_pattern="HH+HL",
            slope=0.45,
            bb_width=2.0,
            name=datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
        )
    )
    assert eng._emission_log[-1].committed is False
    assert eng.current_regime == RegimeLabel.TRANSITION

    # Three agreeing M5s — third one commits TREND.
    for i, ts in enumerate(
        [datetime(2025, 1, 1, 0, 5, tzinfo=timezone.utc),
         datetime(2025, 1, 1, 0, 10, tzinfo=timezone.utc),
         datetime(2025, 1, 1, 0, 15, tzinfo=timezone.utc)]
    ):
        eng.process_m5_close(
            _m5_trend(slope=0.10, ema=100.0, close=101.0, name=ts)
        )
        if i < 2:
            assert eng._emission_log[-1].committed is False
        else:
            assert eng._emission_log[-1].committed is True
            assert eng._emission_log[-1].regime == RegimeLabel.TREND


def test_emission_was_m5_reset_only_on_counter_drop_from_nonzero() -> None:
    eng = RegimeEngine()
    eng.process_h1_close(
        _h1(
            structural_pattern="HH+HL",
            slope=0.45,
            bb_width=2.0,
            name=datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
        )
    )

    # First M5 with no agreement: counter stays at 0 → was_m5_reset is False
    # (0 → 0 is not a reset).
    eng.process_m5_close(
        _m5_trend(slope=-0.10, ema=100.0, close=99.0,
                  name=datetime(2025, 1, 1, 0, 5, tzinfo=timezone.utc))
    )
    assert eng.m5_confirmation_count == 0
    assert eng._emission_log[-1].was_m5_reset is False

    # Agreeing M5: counter 0 → 1, not a reset.
    eng.process_m5_close(
        _m5_trend(slope=0.10, ema=100.0, close=101.0,
                  name=datetime(2025, 1, 1, 0, 10, tzinfo=timezone.utc))
    )
    assert eng.m5_confirmation_count == 1
    assert eng._emission_log[-1].was_m5_reset is False

    # Disagreeing M5: counter 1 → 0, IS a reset.
    eng.process_m5_close(
        _m5_trend(slope=-0.10, ema=100.0, close=99.0,
                  name=datetime(2025, 1, 1, 0, 15, tzinfo=timezone.utc))
    )
    assert eng.m5_confirmation_count == 0
    assert eng._emission_log[-1].was_m5_reset is True


def test_emission_captures_is_live_after_state_mutation() -> None:
    eng = RegimeEngine()
    # Direct VOLATILE entry via structure conflict — commits immediately.
    eng.process_h1_close(
        _h1(
            structural_pattern="HH+LL",
            slope=0.45,
            bb_width=2.0,
            name=datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
        )
    )
    # After committing VOLATILE, is_live should be True and recorded.
    assert eng.is_live() is True
    assert eng._emission_log[-1].is_live is True
    assert eng._emission_log[-1].regime == RegimeLabel.VOLATILE


# --- get_recent_emissions ---------------------------------------------------


def test_get_recent_emissions_filters_by_window() -> None:
    eng = RegimeEngine()
    base = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
    for i in range(10):
        ts = base + timedelta(minutes=10 * i)
        eng.process_h1_close(
            _h1(
                structural_pattern="HH+HL",
                slope=0.45,
                bb_width=2.0,
                name=ts,
            )
        )
    now = base + timedelta(minutes=100)
    # 60-minute window from now: emissions at minutes 40, 50, ..., 90 included.
    window = eng.get_recent_emissions(window_minutes=60, now_utc=now)
    assert len(window) == 6
    assert all(e.timestamp >= now - timedelta(minutes=60) for e in window)
    # Chronologically ordered.
    timestamps = [e.timestamp for e in window]
    assert timestamps == sorted(timestamps)


def test_get_recent_emissions_skips_non_datetime_timestamps() -> None:
    """Test scaffolding uses integer indices; window filter ignores them."""
    eng = RegimeEngine()
    eng.process_h1_close(
        _h1(structural_pattern="HH+HL", slope=0.45, bb_width=2.0, name=42)
    )
    eng.process_h1_close(
        _h1(
            structural_pattern="HH+HL",
            slope=0.45,
            bb_width=2.0,
            name=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
    )
    window = eng.get_recent_emissions(
        window_minutes=60,
        now_utc=datetime(2025, 1, 1, 0, 30, tzinfo=timezone.utc),
    )
    # Integer-indexed emission filtered out; datetime one included.
    assert len(window) == 1
    assert isinstance(window[0].timestamp, datetime)


def test_get_recent_emissions_rejects_zero_window() -> None:
    eng = RegimeEngine()
    with pytest.raises(ValueError, match="window_minutes"):
        eng.get_recent_emissions(
            window_minutes=0, now_utc=datetime.now(tz=timezone.utc)
        )


def test_get_recent_emissions_empty_log_returns_empty_list() -> None:
    eng = RegimeEngine()
    out = eng.get_recent_emissions(
        window_minutes=60,
        now_utc=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    assert out == []


# --- regime_live_at_last_h1_close ------------------------------------------


def test_regime_live_at_last_h1_close_false_on_fresh_engine() -> None:
    eng = RegimeEngine()
    assert eng.regime_live_at_last_h1_close() is False


def test_regime_live_at_last_h1_close_after_volatile_commit() -> None:
    eng = RegimeEngine()
    # Direct VOLATILE entry commits immediately → is_live becomes True.
    eng.process_h1_close(
        _h1(
            structural_pattern="HH+LL",
            slope=0.45,
            bb_width=2.0,
            name=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
    )
    assert eng.regime_live_at_last_h1_close() is True


def test_regime_live_at_last_h1_close_false_during_pending_trend() -> None:
    eng = RegimeEngine()
    # H1 stages TREND pending; current still TRANSITION → not live.
    eng.process_h1_close(
        _h1(
            structural_pattern="HH+HL",
            slope=0.45,
            bb_width=2.0,
            name=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
    )
    assert eng.regime_live_at_last_h1_close() is False


def test_regime_live_at_last_h1_close_not_updated_by_m5_only() -> None:
    eng = RegimeEngine()
    eng.process_h1_close(
        _h1(
            structural_pattern="HH+HL",
            slope=0.45,
            bb_width=2.0,
            name=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
    )
    # Three M5s commit TREND → is_live becomes True via M5 path.
    for i in range(3):
        eng.process_m5_close(
            _m5_trend(
                slope=0.10,
                ema=100.0,
                close=101.0,
                name=datetime(2025, 1, 1, 0, 5 * (i + 1), tzinfo=timezone.utc),
            )
        )
    assert eng.is_live() is True
    # But regime_live_at_last_h1_close still reflects the last H1, where
    # is_live() was False (pending TREND, not yet committed).
    assert eng.regime_live_at_last_h1_close() is False

    # Now another H1 close: same TREND → matches_current, refreshes state.
    eng.process_h1_close(
        _h1(
            structural_pattern="HH+HL",
            slope=0.45,
            bb_width=2.0,
            name=datetime(2025, 1, 1, 1, 0, tzinfo=timezone.utc),
        )
    )
    # Now the H1 saw a committed TREND → live.
    assert eng.regime_live_at_last_h1_close() is True


# --- Log capacity -----------------------------------------------------------


def test_emission_log_capacity_is_bounded() -> None:
    eng = RegimeEngine()
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    # Push twice the maxlen to force eviction.
    for i in range(EMISSION_LOG_MAXLEN + 50):
        eng.process_h1_close(
            _h1(
                structural_pattern="HH+HL",
                slope=0.45,
                bb_width=2.0,
                name=base + timedelta(hours=i),
            )
        )
    # Deque enforced its own maxlen.
    assert len(eng._emission_log) == EMISSION_LOG_MAXLEN
    # The oldest entries dropped off.
    earliest = eng._emission_log[0].timestamp
    assert earliest > base
