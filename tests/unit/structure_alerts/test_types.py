"""Tests for structure_alerts.types — closed-set kinds, severity
mapping, AlertEvent shape."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from alerts import AlertSeverity
from structure_alerts.types import (
    AlertEvent,
    AlertEventKind,
    severity_for,
)


_TS = datetime(2026, 5, 16, 9, 0, tzinfo=timezone.utc)


def _event(**overrides) -> AlertEvent:
    defaults = dict(
        kind=AlertEventKind.HTF_BIAS_CHANGE,
        pair="GBPUSD",
        severity=AlertSeverity.WARNING,
        timestamp=_TS,
        dedupe_key="GBPUSD_HTF_BIAS_BEARISH",
        full_text="HTF bias flipped BULLISH -> BEARISH",
        short_text="HTF BULLISH -> BEARISH",
    )
    defaults.update(overrides)
    return AlertEvent(**defaults)


def test_event_kind_closed_set_is_nine_entries() -> None:
    # Closed-set assertion — a silent rename or addition surfaces here.
    expected = {
        "HTF_BIAS_CHANGE",
        "STRUCTURE_MODE_CHANGE",
        "SUPPORT_ACCEPTANCE_BREAK",
        "RESISTANCE_ACCEPTANCE_BREAK",
        "SWEEP_RECLAIM",
        "FAILED_RECLAIM",
        "NEW_MAJOR_LEVEL",
        "LEVEL_INVALIDATED",
        "HOURLY_SUMMARY",
    }
    assert {k.value for k in AlertEventKind} == expected


@pytest.mark.parametrize(
    "kind,expected",
    [
        (AlertEventKind.HTF_BIAS_CHANGE, AlertSeverity.WARNING),
        (AlertEventKind.STRUCTURE_MODE_CHANGE, AlertSeverity.WARNING),
        (AlertEventKind.SUPPORT_ACCEPTANCE_BREAK, AlertSeverity.CRITICAL),
        (AlertEventKind.RESISTANCE_ACCEPTANCE_BREAK, AlertSeverity.CRITICAL),
        (AlertEventKind.SWEEP_RECLAIM, AlertSeverity.WARNING),
        (AlertEventKind.FAILED_RECLAIM, AlertSeverity.WARNING),
        (AlertEventKind.NEW_MAJOR_LEVEL, AlertSeverity.INFO),
        (AlertEventKind.LEVEL_INVALIDATED, AlertSeverity.INFO),
        (AlertEventKind.HOURLY_SUMMARY, AlertSeverity.INFO),
    ],
)
def test_severity_for_locked_mapping(kind, expected) -> None:
    assert severity_for(kind) is expected


def test_severity_for_covers_every_kind() -> None:
    # Belt-and-braces: if a new AlertEventKind is added without a
    # mapping row, severity_for() will raise KeyError. This loop
    # catches that without the parametrise list having to be kept
    # in sync.
    for kind in AlertEventKind:
        assert isinstance(severity_for(kind), AlertSeverity)


def test_alert_event_is_frozen() -> None:
    e = _event()
    with pytest.raises(FrozenInstanceError):
        e.full_text = "mutated"  # type: ignore[misc]


def test_alert_event_default_debug_is_independent_dict() -> None:
    e1 = _event()
    e2 = _event()
    e1.debug["x"] = 1
    assert e2.debug == {}


def test_alert_event_fields_round_trip() -> None:
    e = _event(
        kind=AlertEventKind.SUPPORT_ACCEPTANCE_BREAK,
        severity=AlertSeverity.CRITICAL,
        dedupe_key="GBPUSD_SUPPORT_ACCEPTANCE_13340",
        debug={"prev_price": 1.33400, "curr_close": 1.33310},
    )
    assert e.kind is AlertEventKind.SUPPORT_ACCEPTANCE_BREAK
    assert e.severity is AlertSeverity.CRITICAL
    assert e.pair == "GBPUSD"
    assert e.dedupe_key == "GBPUSD_SUPPORT_ACCEPTANCE_13340"
    assert e.debug["prev_price"] == 1.33400
