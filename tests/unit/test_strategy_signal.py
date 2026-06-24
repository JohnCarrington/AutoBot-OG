"""Tests for the Signal dataclass + helpers."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from config.pair_config import pip_size_for, pip_to_price, price_to_pips
from day_type import DayType
from regime.labels import Direction
from strategies.signal import Signal, compute_invalid_after


def _signal(
    *,
    pair: str = "GBPUSD",
    direction: Direction = Direction.BULLISH,
    day_type: DayType = DayType.NORMAL,
    strategy_name: str = "bb_bounce",
    suggested_entry_price: float = 1.30000,
    suggested_sl_price: float = 1.29850,
    suggested_tp_price: float | None = 1.30200,
    confidence_score: float = 0.85,
    source_candle_ts: datetime | None = None,
    debug: dict | None = None,
) -> Signal:
    ts = source_candle_ts or datetime(2025, 5, 14, 12, 0, tzinfo=timezone.utc)
    return Signal(
        pair=pair,
        direction=direction,
        day_type=day_type,
        strategy_name=strategy_name,
        suggested_entry_price=suggested_entry_price,
        suggested_sl_price=suggested_sl_price,
        suggested_tp_price=suggested_tp_price,
        confidence_score=confidence_score,
        source_candle_ts=ts,
        invalid_after_candle_ts=compute_invalid_after(ts),
        debug=debug or {},
    )


def test_signal_is_frozen() -> None:
    sig = _signal()
    with pytest.raises(FrozenInstanceError):
        sig.pair = "EURUSD"  # type: ignore[misc]


def test_signal_uses_enum_types() -> None:
    sig = _signal(direction=Direction.BEARISH, day_type=DayType.BIG_NEWS_DAY)
    assert sig.direction is Direction.BEARISH
    assert sig.day_type is DayType.BIG_NEWS_DAY


def test_compute_invalid_after_adds_five_minutes() -> None:
    ts = datetime(2025, 5, 14, 12, 0, tzinfo=timezone.utc)
    assert compute_invalid_after(ts) == ts + timedelta(minutes=5)


def test_signal_invalid_after_matches_helper() -> None:
    ts = datetime(2025, 5, 14, 12, 0, tzinfo=timezone.utc)
    sig = _signal(source_candle_ts=ts)
    assert sig.invalid_after_candle_ts == ts + timedelta(minutes=5)


def test_signal_optional_tp_is_none_for_trend_style_strategies() -> None:
    sig = _signal(suggested_tp_price=None)
    assert sig.suggested_tp_price is None


def test_pip_size_for_gbpusd_is_four_decimal() -> None:
    assert pip_size_for("GBPUSD") == 0.0001
    assert pip_size_for("gbpusd") == 0.0001


def test_pip_size_for_jpy_pair_is_two_decimal() -> None:
    assert pip_size_for("USDJPY") == 0.01
    assert pip_size_for("GBPJPY") == 0.01


def test_pip_size_for_unknown_pair_falls_back_to_four_decimal() -> None:
    assert pip_size_for("ZZZZZZ") == 0.0001


def test_pip_to_price_roundtrip_gbpusd() -> None:
    assert pip_to_price("GBPUSD", 15) == pytest.approx(0.0015)
    assert price_to_pips("GBPUSD", 0.0015) == pytest.approx(15.0)


def test_pip_to_price_roundtrip_jpy() -> None:
    assert pip_to_price("USDJPY", 15) == pytest.approx(0.15)
    assert price_to_pips("USDJPY", 0.15) == pytest.approx(15.0)


def test_price_to_pips_sign_preserving() -> None:
    assert price_to_pips("GBPUSD", -0.0005) == pytest.approx(-5.0)
