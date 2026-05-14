"""Tests for execution.sl_management.evaluate_sl_amend."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from execution.sl_management import evaluate_sl_amend
from execution.types import ExecutionPosition
from regime.labels import Direction, RegimeLabel


_TS = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)


def _pos(**overrides) -> ExecutionPosition:
    """A LONG GBPUSD TREND position, 15-pip initial stop."""
    defaults = dict(
        deal_id="D1",
        deal_reference="REF",
        pair="GBPUSD",
        direction=Direction.BULLISH,
        regime_at_entry=RegimeLabel.TREND,
        strategy_name="ema_continuation",
        size_units=1.0,
        entry_price=1.30000,
        initial_sl_price=1.29850,  # 15p risk
        current_sl_price=1.29850,
        suggested_tp_price=None,
        entry_time_utc=_TS,
        signal_source_candle_ts=_TS,
        be_moved=False,
        trail_active=False,
    )
    defaults.update(overrides)
    return ExecutionPosition(**defaults)


def _m5(rows: list[dict]) -> pd.DataFrame:
    """Build an M5 DataFrame with hand-populated swing columns + EMA20."""
    return pd.DataFrame(rows, index=pd.DatetimeIndex(
        [_TS for _ in rows]
    ))


# --- BE move ---------------------------------------------------------------


def test_be_move_fires_at_plus_one_r_long() -> None:
    p = _pos()  # entry 1.30000, SL 1.29850, 15p risk
    # +1R is 1.30150.
    amend = evaluate_sl_amend(p, _m5([]), current_price=1.30150)
    assert amend is not None
    assert amend.reason == "be_move_at_1r"
    # BE buffer = 1 pip → SL at 1.30010 for long.
    assert amend.new_sl_price == pytest.approx(1.30010)


def test_be_move_does_not_fire_below_one_r() -> None:
    p = _pos()
    # +0.5R = 1.30075.
    amend = evaluate_sl_amend(p, _m5([]), current_price=1.30075)
    assert amend is None


def test_be_move_fires_at_plus_one_r_short() -> None:
    p = _pos(
        direction=Direction.BEARISH,
        entry_price=1.30000,
        initial_sl_price=1.30150,
        current_sl_price=1.30150,
    )
    # +1R for short = 1.29850.
    amend = evaluate_sl_amend(p, _m5([]), current_price=1.29850)
    assert amend is not None
    assert amend.reason == "be_move_at_1r"
    # Buffer on short = entry - 1p = 1.29990.
    assert amend.new_sl_price == pytest.approx(1.29990)


def test_be_move_skipped_when_current_sl_already_tighter() -> None:
    # The trade somehow ran past +1R while a tighter SL already exists.
    p = _pos(current_sl_price=1.30050)  # already above entry+buffer
    amend = evaluate_sl_amend(p, _m5([]), current_price=1.30150)
    # BE buffer would give 1.30010 — that's *looser* than current 1.30050.
    assert amend is None


# --- Trail (post-BE) -------------------------------------------------------


def _trail_bars(*, swing_low: float, swing_high: float, ema20: float) -> pd.DataFrame:
    """6-bar M5 frame with one swing low at idx 2 and one swing high at idx 4."""
    rows = []
    for i in range(6):
        rows.append(
            {
                "open": 1.30050,
                "high": 1.30070 if i == 4 else 1.30060,
                "low": 1.29960 if i == 2 else 1.30030,
                "close": 1.30050,
                "ema_20": ema20,
                "swing_high": (i == 4),
                "swing_low": (i == 2),
                "swing_high_price": swing_high if i == 4 else float("nan"),
                "swing_low_price": swing_low if i == 2 else float("nan"),
            }
        )
    # Overwrite the swing low / high price extremes on those bars.
    rows[2]["low"] = swing_low
    rows[4]["high"] = swing_high
    return _m5(rows)


def test_trail_picks_conservative_long_swing_primary() -> None:
    # ema_continuation → swing primary. LONG conservative = higher value.
    # Swing low at 1.29960 (10p below entry), EMA20 at 1.30020 (just above entry).
    # Both are valid trail candidates (below current price 1.30100); conservative = 1.30020.
    p = _pos(
        strategy_name="ema_continuation",
        be_moved=True,
        trail_active=True,
        current_sl_price=1.30010,  # BE level
    )
    df = _trail_bars(swing_low=1.29960, swing_high=1.30200, ema20=1.30020)
    amend = evaluate_sl_amend(p, df, current_price=1.30100)
    assert amend is not None
    # Conservative for ema_continuation is whichever is closer to price.
    # primary (swing) = 1.29960; secondary (ema20) = 1.30020.
    # LONG conservative (max) = 1.30020.
    assert amend.new_sl_price == pytest.approx(1.30020)
    assert amend.reason == "trail_ema20_secondary"


def test_trail_picks_swing_when_higher_than_ema20() -> None:
    p = _pos(
        strategy_name="ema_continuation",
        be_moved=True,
        trail_active=True,
        current_sl_price=1.30010,
    )
    # Swing low 1.30040 (recent HL); EMA20 = 1.30020. Swing wins.
    df = _trail_bars(swing_low=1.30040, swing_high=1.30200, ema20=1.30020)
    amend = evaluate_sl_amend(p, df, current_price=1.30100)
    assert amend is not None
    assert amend.new_sl_price == pytest.approx(1.30040)
    assert amend.reason == "trail_swing_primary"


def test_trail_short_picks_min_candidate() -> None:
    # SHORT: bb_reclaim — ema20 primary, swing secondary. Conservative = min.
    p = _pos(
        strategy_name="bb_reclaim",
        direction=Direction.BEARISH,
        entry_price=1.30000,
        initial_sl_price=1.30150,
        current_sl_price=1.29990,  # BE level for short
        be_moved=True,
        trail_active=True,
    )
    # Need ema20 above current price and swing_high above current price for SHORT.
    rows = [
        {"open": 1.29950, "high": 1.29960, "low": 1.29940, "close": 1.29950,
         "ema_20": 1.29980, "swing_high": (i == 4), "swing_low": False,
         "swing_high_price": 1.30100 if i == 4 else float("nan"),
         "swing_low_price": float("nan")}
        for i in range(6)
    ]
    rows[4]["high"] = 1.30100
    df = _m5(rows)
    amend = evaluate_sl_amend(p, df, current_price=1.29950)
    assert amend is not None
    # ema20 primary = 1.29980; swing secondary = 1.30100. SHORT conservative = min = 1.29980.
    assert amend.new_sl_price == pytest.approx(1.29980)
    assert amend.reason == "trail_ema20_primary"


def test_trail_rejects_ema20_on_wrong_side_long() -> None:
    # EMA20 above current price on a LONG — not a valid candidate.
    p = _pos(
        strategy_name="ema_continuation",
        be_moved=True,
        trail_active=True,
        current_sl_price=1.30010,
    )
    df = _trail_bars(swing_low=1.30040, swing_high=1.30200, ema20=1.30150)
    amend = evaluate_sl_amend(p, df, current_price=1.30100)
    # Should fall back to swing primary alone.
    assert amend is not None
    assert amend.new_sl_price == pytest.approx(1.30040)
    assert amend.reason == "trail_swing_primary"


def test_trail_never_widens() -> None:
    p = _pos(
        strategy_name="ema_continuation",
        be_moved=True,
        trail_active=True,
        current_sl_price=1.30050,  # already tighter than candidates below
    )
    df = _trail_bars(swing_low=1.29960, swing_high=1.30200, ema20=1.30020)
    amend = evaluate_sl_amend(p, df, current_price=1.30100)
    assert amend is None  # both candidates worse than 1.30050


def test_trail_requires_min_delta() -> None:
    p = _pos(
        strategy_name="ema_continuation",
        be_moved=True,
        trail_active=True,
        current_sl_price=1.30020,
    )
    # Swing 1.30021 (0.1p improvement) — below min 1.0p delta.
    df = _trail_bars(swing_low=1.30021, swing_high=1.30200, ema20=1.30021)
    amend = evaluate_sl_amend(p, df, current_price=1.30100)
    assert amend is None


def test_trail_inactive_until_be_move() -> None:
    """Locked decision: trail does NOT fire pre-BE."""
    p = _pos(be_moved=False, trail_active=False, current_sl_price=1.29850)
    df = _trail_bars(swing_low=1.29960, swing_high=1.30200, ema20=1.30020)
    # +0.5R price; no BE yet.
    amend = evaluate_sl_amend(p, df, current_price=1.30075)
    assert amend is None


def test_trail_empty_dataframe_returns_none() -> None:
    p = _pos(be_moved=True, trail_active=True, current_sl_price=1.30010)
    amend = evaluate_sl_amend(p, _m5([]), current_price=1.30100)
    assert amend is None


def test_trail_with_no_swing_data_falls_back_to_ema20() -> None:
    p = _pos(
        strategy_name="ema_continuation",
        be_moved=True,
        trail_active=True,
        current_sl_price=1.30010,
    )
    # 6 bars, no swing markers at all, EMA20 at 1.30020.
    rows = [
        {"open": 1.30050, "high": 1.30060, "low": 1.30030, "close": 1.30050,
         "ema_20": 1.30020, "swing_high": False, "swing_low": False,
         "swing_high_price": float("nan"), "swing_low_price": float("nan")}
        for _ in range(6)
    ]
    df = _m5(rows)
    amend = evaluate_sl_amend(p, df, current_price=1.30100)
    assert amend is not None
    assert amend.new_sl_price == pytest.approx(1.30020)
    assert amend.reason == "trail_ema20_secondary"


def test_unknown_strategy_returns_none() -> None:
    p = _pos(
        strategy_name="experimental_pattern",  # type: ignore[arg-type]
        be_moved=True,
        trail_active=True,
        current_sl_price=1.30010,
    )
    df = _trail_bars(swing_low=1.29960, swing_high=1.30200, ema20=1.30020)
    amend = evaluate_sl_amend(p, df, current_price=1.30100)
    assert amend is None
