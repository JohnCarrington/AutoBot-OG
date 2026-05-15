"""Tests for alerts.formatter — single + coalesced rendering, emoji map."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from alerts.formatter import AlertFormatter
from alerts.types import Alert, AlertCategory, AlertSeverity


_TS = datetime(2026, 5, 15, 13, 0, tzinfo=timezone.utc)


def _alert(**overrides) -> Alert:
    defaults = dict(
        category=AlertCategory.TRADE,
        event_subtype="TRADE_OPENED",
        severity=AlertSeverity.INFO,
        pair="GBPUSD",
        full_text="GBPUSD bullish @ 1.30050 SL=1.29900",
        short_text="bullish @ 1.30050",
        timestamp=_TS,
    )
    defaults.update(overrides)
    return Alert(**defaults)


# ---------------------------------------------------------------------------
# emoji_for / severity map
# ---------------------------------------------------------------------------


def test_emoji_for_each_severity() -> None:
    info = AlertFormatter.emoji_for(AlertSeverity.INFO)
    warn = AlertFormatter.emoji_for(AlertSeverity.WARNING)
    crit = AlertFormatter.emoji_for(AlertSeverity.CRITICAL)
    assert info != warn != crit
    assert info == "ℹ️"
    assert warn == "⚠️"
    assert crit == "\U0001f6a8"  # 🚨


# ---------------------------------------------------------------------------
# format_single
# ---------------------------------------------------------------------------


def test_format_single_includes_emoji_subtype_and_full_text() -> None:
    text = AlertFormatter.format_single(_alert(severity=AlertSeverity.INFO))
    assert text.startswith("ℹ️")
    assert "TRADE_OPENED" in text
    assert "GBPUSD bullish @ 1.30050 SL=1.29900" in text


def test_format_single_uses_warning_emoji_for_warning() -> None:
    text = AlertFormatter.format_single(_alert(
        severity=AlertSeverity.WARNING, event_subtype="FEED_STALE",
        full_text="LS disconnected",
    ))
    assert text.startswith("⚠️")
    assert "FEED_STALE" in text


def test_format_single_uses_critical_emoji_for_critical() -> None:
    text = AlertFormatter.format_single(_alert(
        severity=AlertSeverity.CRITICAL,
        event_subtype="FAILURE_THRESHOLD_TRIPPED",
        full_text="5 consecutive event failures",
    ))
    assert "\U0001f6a8" in text


# ---------------------------------------------------------------------------
# format_batch — coalesced summaries
# ---------------------------------------------------------------------------


def test_format_batch_with_one_alert_renders_as_single() -> None:
    batch = [_alert(full_text="bullish trade")]
    text = AlertFormatter.format_batch(batch, window_seconds=30)
    assert text == AlertFormatter.format_single(batch[0])
    assert "×" not in text  # no coalesced header


def test_format_batch_with_2_to_5_alerts_uses_bullet_list() -> None:
    batch = [
        _alert(short_text="bullish @ 1.30050"),
        _alert(short_text="bullish @ 1.30100"),
        _alert(short_text="bullish @ 1.30150"),
    ]
    text = AlertFormatter.format_batch(batch, window_seconds=30)
    lines = text.split("\n")
    # Header
    assert lines[0].startswith("ℹ️ TRADE_OPENED")
    assert "×3" in lines[0]
    assert "GBPUSD" in lines[0]
    assert "in 30s" in lines[0]
    # Three bullets
    assert lines[1] == "• bullish @ 1.30050"
    assert lines[2] == "• bullish @ 1.30100"
    assert lines[3] == "• bullish @ 1.30150"
    # No truncation line for N <= 5
    assert "more" not in text


def test_format_batch_truncates_when_more_than_max_bullets() -> None:
    batch = [_alert(short_text=f"alert {i}") for i in range(8)]
    text = AlertFormatter.format_batch(batch, window_seconds=30)
    lines = text.split("\n")
    # Header + 5 bullets + "... and 3 more" = 7 lines
    assert len(lines) == 7
    assert "×8" in lines[0]
    assert lines[6] == "... and 3 more"


def test_format_batch_drops_pair_segment_when_pair_is_none() -> None:
    batch = [
        _alert(pair=None, category=AlertCategory.SYSTEM, event_subtype="STARTUP", short_text="cold-start"),
        _alert(pair=None, category=AlertCategory.SYSTEM, event_subtype="STARTUP", short_text="warm-start"),
    ]
    text = AlertFormatter.format_batch(batch, window_seconds=30)
    assert "GBPUSD" not in text
    assert "STARTUP" in text


def test_format_batch_rejects_empty_list() -> None:
    with pytest.raises(ValueError):
        AlertFormatter.format_batch([], window_seconds=30)


def test_format_batch_uses_first_alerts_severity_in_header() -> None:
    """Coalesce key includes severity implicitly (same subtype → same sev)
    but assert explicitly since the header reads off [0]."""
    batch = [
        _alert(severity=AlertSeverity.WARNING, short_text="orphan A"),
        _alert(severity=AlertSeverity.WARNING, short_text="orphan B"),
    ]
    text = AlertFormatter.format_batch(batch, window_seconds=30)
    assert text.startswith("⚠️")
