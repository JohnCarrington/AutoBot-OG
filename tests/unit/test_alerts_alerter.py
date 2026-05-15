"""Tests for alerts.alerter — TelegramAlerter composition + lifecycle."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from alerts.alerter import TelegramAlerter
from alerts.telegram_client import TelegramClient
from alerts.types import Alert, AlertCategory, AlertSeverity


_NOW0 = datetime(2026, 5, 15, 13, 0, tzinfo=timezone.utc)


class _RecordingClient:
    """Stands in for TelegramClient. Records every send call."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.return_value: bool = True

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return self.return_value


def _alert(**overrides) -> Alert:
    pair = overrides.get("pair", "GBPUSD")
    defaults = dict(
        category=AlertCategory.TRADE,
        event_subtype="TRADE_OPENED",
        severity=AlertSeverity.INFO,
        pair=pair,
        full_text=f"{pair} bullish",
        short_text="bullish",
        timestamp=_NOW0,
    )
    defaults.update(overrides)
    return Alert(**defaults)


# ---------------------------------------------------------------------------
# No-op mode
# ---------------------------------------------------------------------------


def test_alerter_logs_warning_once_when_creds_missing(monkeypatch, caplog) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with caplog.at_level(logging.WARNING, logger="alerts.alerter"):
        a = TelegramAlerter()
    assert not a.enabled
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1
    assert "TELEGRAM_BOT_TOKEN" in warns[0].getMessage()


def test_alerter_noop_when_only_token_set(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    a = TelegramAlerter()
    assert not a.enabled


def test_alerter_noop_when_only_chat_id_set(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    a = TelegramAlerter()
    assert not a.enabled


def test_noop_alerter_send_tick_close_dont_touch_client(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    a = TelegramAlerter()
    # Should not raise — and there's no client to record sends.
    a.send(_alert())
    a.tick()
    a.close()
    assert a.pending_count == 0


# ---------------------------------------------------------------------------
# Enabled mode — send/tick/close routing
# ---------------------------------------------------------------------------


def _enabled_alerter(*, clock_box=None, client=None) -> tuple[TelegramAlerter, _RecordingClient]:
    rec = client or _RecordingClient()
    clock_box = clock_box if clock_box is not None else [_NOW0]
    a = TelegramAlerter(
        bot_token="TOK",
        chat_id="CHAT",
        client=rec,  # type: ignore[arg-type]
        clock=lambda: clock_box[0],
        coalesce_window_seconds=30,
    )
    assert a.enabled
    return a, rec  # type: ignore[return-value]


def test_single_alert_held_back_then_flushed_by_tick() -> None:
    box = [_NOW0]
    a, rec = _enabled_alerter(clock_box=box)
    a.send(_alert(short_text="bullish @ 1.30050"))
    # Nothing delivered yet — within coalesce window.
    assert rec.sent == []
    # Advance and tick.
    box[0] = _NOW0 + timedelta(seconds=35)
    a.tick()
    assert len(rec.sent) == 1
    assert "TRADE_OPENED" in rec.sent[0]
    assert "GBPUSD bullish" in rec.sent[0]


def test_critical_alert_delivered_immediately() -> None:
    a, rec = _enabled_alerter()
    a.send(_alert(
        severity=AlertSeverity.CRITICAL,
        category=AlertCategory.SYSTEM,
        event_subtype="FAILURE_THRESHOLD_TRIPPED",
        pair=None,
        full_text="5 consecutive event failures — shutting down",
        short_text="threshold tripped",
    ))
    assert len(rec.sent) == 1
    assert "FAILURE_THRESHOLD_TRIPPED" in rec.sent[0]
    # CRITICAL emoji.
    assert "\U0001f6a8" in rec.sent[0]


def test_burst_of_same_key_alerts_coalesces_to_one_summary() -> None:
    """Three GBPUSD BROKER_ORPHAN findings within 30s → one summary message."""
    box = [_NOW0]
    a, rec = _enabled_alerter(clock_box=box)
    for i in range(3):
        a.send(_alert(
            category=AlertCategory.RECONCILIATION,
            event_subtype="BROKER_ORPHAN",
            severity=AlertSeverity.WARNING,
            short_text=f"orphan {i}",
        ))
        box[0] = _NOW0 + timedelta(seconds=5 * (i + 1))
    assert rec.sent == []  # window not elapsed
    # Flush.
    box[0] = _NOW0 + timedelta(seconds=35)
    a.tick()
    assert len(rec.sent) == 1
    msg = rec.sent[0]
    assert "BROKER_ORPHAN" in msg
    assert "×3" in msg
    assert "GBPUSD" in msg
    # Three bullets present.
    assert "• orphan 0" in msg
    assert "• orphan 1" in msg
    assert "• orphan 2" in msg


def test_different_pair_alerts_send_as_separate_messages() -> None:
    box = [_NOW0]
    a, rec = _enabled_alerter(clock_box=box)
    a.send(_alert(pair="GBPUSD"))
    box[0] = _NOW0 + timedelta(seconds=5)
    a.send(_alert(pair="EURUSD"))
    box[0] = _NOW0 + timedelta(seconds=40)
    a.tick()
    # Two messages.
    assert len(rec.sent) == 2
    pairs_in_messages = [
        "GBPUSD" if "GBPUSD" in m else "EURUSD" for m in rec.sent
    ]
    assert set(pairs_in_messages) == {"GBPUSD", "EURUSD"}


def test_close_flushes_pending_regardless_of_window() -> None:
    a, rec = _enabled_alerter()
    a.send(_alert())
    a.send(_alert(short_text="another"))
    assert rec.sent == []  # within window
    a.close()
    assert len(rec.sent) == 1
    assert "×2" in rec.sent[0]


def test_close_is_safe_when_no_pending_alerts() -> None:
    a, rec = _enabled_alerter()
    a.close()
    assert rec.sent == []


def test_send_during_close_path_does_not_raise() -> None:
    """An alert arriving after close() still goes through the
    coalescer (no special shutdown gate on the alerter)."""
    a, rec = _enabled_alerter()
    a.close()
    a.send(_alert())
    # No exception; alert is now pending in the coalescer.
    assert a.pending_count == 1


# ---------------------------------------------------------------------------
# Exception isolation
# ---------------------------------------------------------------------------


def test_client_failure_does_not_propagate() -> None:
    """If TelegramClient.send raises, the alerter swallows it."""
    class _RaisingClient:
        def send(self, text: str) -> bool:
            raise RuntimeError("network gone")

    a = TelegramAlerter(
        bot_token="TOK", chat_id="CHAT",
        client=_RaisingClient(),  # type: ignore[arg-type]
        coalesce_window_seconds=30,
    )
    # CRITICAL bypasses coalescing → triggers an immediate deliver →
    # the raising client's exception must not escape.
    a.send(_alert(
        severity=AlertSeverity.CRITICAL,
        event_subtype="FAILURE_THRESHOLD_TRIPPED",
        category=AlertCategory.SYSTEM,
        pair=None,
        full_text="x", short_text="x",
    ))
    # Did not raise — that's the assertion.


def test_pending_count_property_reflects_buffered_alerts() -> None:
    a, _ = _enabled_alerter()
    a.send(_alert(pair="GBPUSD"))
    a.send(_alert(pair="EURUSD"))
    assert a.pending_count == 2
    a.close()
    assert a.pending_count == 0


# ---------------------------------------------------------------------------
# Lifecycle quirk: tick() when disabled
# ---------------------------------------------------------------------------


def test_disabled_alerter_pending_count_is_zero(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    a = TelegramAlerter()
    a.send(_alert())  # swallowed
    assert a.pending_count == 0
