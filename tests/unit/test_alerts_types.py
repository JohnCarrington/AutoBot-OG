"""Tests for alerts.types — Alert, severity/category enums, coalesce_key."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from alerts.types import (
    Alert,
    AlertCategory,
    AlertSeverity,
    EVENT_SUBTYPES,
)


_TS = datetime(2026, 5, 15, 13, 0, tzinfo=timezone.utc)


def _alert(**overrides) -> Alert:
    defaults = dict(
        category=AlertCategory.TRADE,
        event_subtype="TRADE_OPENED",
        severity=AlertSeverity.INFO,
        pair="GBPUSD",
        full_text="GBPUSD bullish @ 1.3 SL=1.29",
        short_text="bullish @ 1.3",
        timestamp=_TS,
    )
    defaults.update(overrides)
    return Alert(**defaults)


def test_severity_enum_values() -> None:
    assert {s.value for s in AlertSeverity} == {"INFO", "WARNING", "CRITICAL"}


def test_category_enum_values() -> None:
    # Phase 12 adds STRUCTURE for the structure-alerting layer
    # (HTF_BIAS_CHANGE, STRUCTURE_MODE_CHANGE, acceptance-breaks,
    # sweep/failed reclaims, NEW_MAJOR_LEVEL, LEVEL_INVALIDATED,
    # HOURLY_SUMMARY).
    assert {c.value for c in AlertCategory} == {
        "TRADE", "RECONCILIATION", "SYSTEM", "STRUCTURE",
    }


def test_event_subtypes_includes_locked_set() -> None:
    # The full closed set from the Phase 9 plan, plus:
    # - AMEND_PERSIST_FAILED added in commit 2b's H1 fix (Session-3
    #   review: broker accepted amend but local persist failed →
    #   CRITICAL alert so operator can reconcile state divergence).
    # - SHADOW_TRADE added in Phase 10 (would-be trade caught by
    #   SHADOW_MODE intercept in BotLoop._evaluate_and_execute).
    # - HEALTHCHECK_FAILED added in Phase 10 (pre-market healthcheck
    #   reported one or more hard failures).
    # - The nine Phase 12 STRUCTURE subtypes — kind→severity mapping
    #   is locked in :mod:`structure_alerts.types.severity_for`.
    expected = {
        "TRADE_OPENED", "TRADE_CLOSED", "AMEND_FAILED", "AMEND_PERSIST_FAILED",
        "SHADOW_TRADE",
        "BROKER_ORPHAN", "MISSING_LOCAL_KEPT", "MANUAL_SL_MOVE",
        "STARTUP", "SHUTDOWN", "FEED_STALE", "FEED_RESUMED",
        "FAILURE_THRESHOLD_TRIPPED", "HEALTHCHECK_FAILED",
        # Phase 10 H1 layer 1 + 2 (Session-3 review):
        "STARTUP_ABORTED", "SHADOW_GUARD_BLOCKED",
        # Phase 12 STRUCTURE (spec §7 A–H + §11 heartbeat):
        "HTF_BIAS_CHANGE", "STRUCTURE_MODE_CHANGE",
        "SUPPORT_ACCEPTANCE_BREAK", "RESISTANCE_ACCEPTANCE_BREAK",
        "SWEEP_RECLAIM", "FAILED_RECLAIM",
        "NEW_MAJOR_LEVEL", "LEVEL_INVALIDATED", "HOURLY_SUMMARY",
    }
    assert set(EVENT_SUBTYPES) == expected


def test_alert_is_frozen() -> None:
    a = _alert()
    with pytest.raises(FrozenInstanceError):
        a.full_text = "mutated"  # type: ignore[misc]


def test_alert_default_debug_is_independent_dict() -> None:
    a1 = _alert()
    a2 = _alert()
    a1.debug["x"] = 1
    assert a2.debug == {}


def test_coalesce_key_tuple_shape() -> None:
    a = _alert(
        category=AlertCategory.TRADE,
        event_subtype="TRADE_OPENED",
        pair="GBPUSD",
        severity=AlertSeverity.INFO,
    )
    assert a.coalesce_key() == (
        AlertCategory.TRADE,
        "TRADE_OPENED",
        "GBPUSD",
        AlertSeverity.INFO,
    )


def test_coalesce_key_distinguishes_pairs() -> None:
    """Locked refinement #1: pair is part of the key."""
    gbpusd = _alert(pair="GBPUSD")
    eurusd = _alert(pair="EURUSD")
    assert gbpusd.coalesce_key() != eurusd.coalesce_key()


def test_coalesce_key_pair_can_be_none_for_system_alerts() -> None:
    a = _alert(
        category=AlertCategory.SYSTEM,
        event_subtype="STARTUP",
        pair=None,
        severity=AlertSeverity.INFO,
    )
    assert a.coalesce_key() == (
        AlertCategory.SYSTEM,
        "STARTUP",
        None,
        AlertSeverity.INFO,
    )


def test_coalesce_key_distinguishes_severity() -> None:
    """M1 (Phase 9 review): severity is part of the key.

    Two alerts with the same (category, event_subtype, pair) but
    different severities must NOT collapse into one bullet list — a
    severity escalation would otherwise hide behind the lower one.
    """
    info = _alert(severity=AlertSeverity.INFO)
    warning = _alert(severity=AlertSeverity.WARNING)
    assert info.coalesce_key() != warning.coalesce_key()
