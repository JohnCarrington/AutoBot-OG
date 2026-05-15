"""Tests for alerts.formatter — single + coalesced rendering, emoji map."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    """M3 (Phase 9 review): with severity now an explicit part of
    the coalesce key (M1), every alert in a batch shares severity
    by construction; the header just reads off ``alerts[0]``
    because the coalescer guarantees the batch is homogeneous."""
    batch = [
        _alert(severity=AlertSeverity.WARNING, short_text="orphan A"),
        _alert(severity=AlertSeverity.WARNING, short_text="orphan B"),
    ]
    text = AlertFormatter.format_batch(batch, window_seconds=30)
    assert text.startswith("⚠️")


# ---------------------------------------------------------------------------
# Timestamp surfacing (L1, Phase 9 review)
# ---------------------------------------------------------------------------


def test_format_single_appends_timestamp_when_present() -> None:
    text = AlertFormatter.format_single(_alert(timestamp=_TS))
    assert text.endswith("(13:00:00 UTC)")


def test_format_single_omits_timestamp_when_absent() -> None:
    text = AlertFormatter.format_single(_alert(timestamp=None))
    assert "UTC" not in text
    assert not text.endswith(")")


def test_format_batch_header_carries_first_alert_timestamp() -> None:
    """The first alert in the batch anchors the burst — its
    timestamp goes on the header, not every bullet."""
    ts1 = datetime(2026, 5, 15, 13, 0, 5, tzinfo=timezone.utc)
    ts2 = datetime(2026, 5, 15, 13, 0, 20, tzinfo=timezone.utc)
    batch = [
        _alert(short_text="bullish @ 1.30050", timestamp=ts1),
        _alert(short_text="bullish @ 1.30100", timestamp=ts2),
    ]
    text = AlertFormatter.format_batch(batch, window_seconds=30)
    lines = text.split("\n")
    # Header carries ts1 (the first alert's timestamp).
    assert lines[0].endswith("(13:00:05 UTC)")
    # Bullets do NOT carry per-event timestamps.
    assert "UTC" not in lines[1]
    assert "UTC" not in lines[2]


def test_format_batch_header_omits_timestamp_when_first_alert_has_none() -> None:
    batch = [
        _alert(short_text="a", timestamp=None),
        _alert(short_text="b", timestamp=_TS),
    ]
    text = AlertFormatter.format_batch(batch, window_seconds=30)
    lines = text.split("\n")
    assert "UTC" not in lines[0]


# ---------------------------------------------------------------------------
# Control-character sanitisation (M5, Phase 9 review)
# ---------------------------------------------------------------------------


def test_format_single_strips_ascii_control_chars() -> None:
    """Bell, ESC and the rest of 0x00-0x1F (except \\t \\n \\r) get dropped
    so they cannot ring Telegram clients or smuggle ANSI escapes."""
    poisoned = "bullish \x07@ 1.30050 \x1b[31mRED\x1b[0m"
    text = AlertFormatter.format_single(_alert(full_text=poisoned))
    assert "\x07" not in text
    assert "\x1b" not in text
    # The visible text survives the strip.
    assert "bullish @ 1.30050" in text
    assert "[31mRED[0m" in text  # only the ESC chars dropped, visible "[31m" remains


def test_format_single_preserves_tab_newline_carriage_return() -> None:
    """The three legitimate whitespace control codes must survive
    sanitisation — multi-line full_text is a real use case."""
    payload = "line1\nline2\tindented\rOK"
    text = AlertFormatter.format_single(_alert(full_text=payload))
    assert "line1\nline2\tindented\rOK" in text


def test_format_batch_strips_control_chars_from_short_text() -> None:
    """Sanitisation applies to short_text too — every per-bullet body."""
    batch = [
        _alert(short_text="a\x00b"),
        _alert(short_text="c\x1fd"),
    ]
    text = AlertFormatter.format_batch(batch, window_seconds=30)
    assert "\x00" not in text
    assert "\x1f" not in text
    assert "• ab" in text
    assert "• cd" in text


def test_format_single_strips_null_byte() -> None:
    """Null byte specifically — would break C-string-based handlers."""
    text = AlertFormatter.format_single(_alert(full_text="be\x00fore"))
    assert "\x00" not in text
    assert "before" in text


# ---------------------------------------------------------------------------
# Timestamp UTC conversion (M1, Session-3 re-review)
# ---------------------------------------------------------------------------


def test_timestamp_with_non_utc_tz_converts_to_utc() -> None:
    """M1 (Session-3 re-review): a non-UTC tz-aware timestamp must
    be converted to UTC before formatting. The trailer always reads
    "UTC", so rendering a New-York-local clock-face with the UTC
    label would silently misinform the operator about when the
    event happened."""
    eastern = timezone(timedelta(hours=-5))
    ts = datetime(2026, 5, 15, 10, 0, 0, tzinfo=eastern)  # 10:00 EST = 15:00 UTC
    text = AlertFormatter.format_single(_alert(timestamp=ts))
    assert "15:00:00 UTC" in text
    assert "10:00:00 UTC" not in text


def test_timestamp_naive_assumed_utc() -> None:
    """A tz-naive timestamp is treated as UTC (no shift). The bot
    only emits tz-aware UTC timestamps; the naive path exists for
    fixtures and direct caller mistakes — assume rather than crash."""
    ts = datetime(2026, 5, 15, 13, 0, 0)  # naive
    text = AlertFormatter.format_single(_alert(timestamp=ts))
    assert "13:00:00 UTC" in text


def test_timestamp_already_utc_unchanged() -> None:
    """Sanity: a tz-aware UTC timestamp passes through unmodified."""
    ts = datetime(2026, 5, 15, 13, 0, 0, tzinfo=timezone.utc)
    text = AlertFormatter.format_single(_alert(timestamp=ts))
    assert "13:00:00 UTC" in text
