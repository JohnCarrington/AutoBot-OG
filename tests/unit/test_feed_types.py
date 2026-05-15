"""Tests for feed.types — Candle, FeedEvent, FeedEventKind."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from feed.types import Candle, FeedEvent, FeedEventKind


_TS = datetime(2026, 5, 15, 13, 0, tzinfo=timezone.utc)


def _candle(**overrides) -> Candle:
    defaults = dict(
        pair="GBPUSD",
        close_time=_TS,
        open=1.30000,
        high=1.30100,
        low=1.29950,
        close=1.30050,
        volume=250.0,
        source="LS_NATIVE_5M",
    )
    defaults.update(overrides)
    return Candle(**defaults)


def test_candle_is_frozen() -> None:
    c = _candle()
    with pytest.raises(FrozenInstanceError):
        c.close = 1.4  # type: ignore[misc]


def test_candle_round_trip_fields() -> None:
    c = _candle()
    assert c.pair == "GBPUSD"
    assert c.close_time == _TS
    assert c.open == 1.30000
    assert c.high == 1.30100
    assert c.low == 1.29950
    assert c.close == 1.30050
    assert c.volume == 250.0
    assert c.source == "LS_NATIVE_5M"


def test_feed_event_kinds_are_distinct_strings() -> None:
    seen = {k.value for k in FeedEventKind}
    assert seen == {
        "BAR_UPDATE",
        "BAR_CLOSE",
        "FEED_STALE",
        "FEED_RESUMED",
        "GAP_FILLED",
    }


def test_feed_event_default_debug_is_independent_dict() -> None:
    e1 = FeedEvent(kind=FeedEventKind.BAR_UPDATE, pair="GBPUSD", candle=None, timestamp=_TS)
    e2 = FeedEvent(kind=FeedEventKind.BAR_UPDATE, pair="EURUSD", candle=None, timestamp=_TS)
    # Mutating one debug must not bleed into the other (default_factory test).
    e1.debug["x"] = 1
    assert e2.debug == {}


def test_feed_event_holds_candle() -> None:
    c = _candle()
    e = FeedEvent(
        kind=FeedEventKind.BAR_CLOSE,
        pair="GBPUSD",
        candle=c,
        timestamp=_TS,
        debug={"reason": "cons_end_flip"},
    )
    assert e.candle is c
    assert e.debug == {"reason": "cons_end_flip"}


def test_feed_event_is_frozen() -> None:
    e = FeedEvent(
        kind=FeedEventKind.FEED_STALE, pair="*", candle=None, timestamp=_TS,
    )
    with pytest.raises(FrozenInstanceError):
        e.kind = FeedEventKind.FEED_RESUMED  # type: ignore[misc]
