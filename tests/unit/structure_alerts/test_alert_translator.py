"""Tests for structure_alerts.alert_translator.translate_to_phase9_alert."""
from __future__ import annotations

from datetime import datetime, timezone

from alerts import Alert, AlertCategory, AlertSeverity
from structure_alerts.alert_translator import translate_to_phase9_alert
from structure_alerts.types import AlertEvent, AlertEventKind


_TS = datetime(2026, 5, 16, 9, 5, tzinfo=timezone.utc)


def _event(**overrides) -> AlertEvent:
    defaults = dict(
        kind=AlertEventKind.HTF_BIAS_CHANGE,
        pair="GBPUSD",
        severity=AlertSeverity.WARNING,
        timestamp=_TS,
        dedupe_key="GBPUSD_HTF_BIAS_BEARISH",
        full_text="HTF bias BULLISH -> BEARISH",
        short_text="HTF BULLISH -> BEARISH",
        debug={"prev_htf_bias": "BULLISH", "curr_htf_bias": "BEARISH"},
    )
    defaults.update(overrides)
    return AlertEvent(**defaults)


# ---------------------------------------------------------------------------
# Category and event_subtype mapping
# ---------------------------------------------------------------------------


def test_category_is_structure() -> None:
    alert = translate_to_phase9_alert(_event())
    assert isinstance(alert, Alert)
    assert alert.category is AlertCategory.STRUCTURE


def test_event_subtype_is_kind_value() -> None:
    alert = translate_to_phase9_alert(
        _event(kind=AlertEventKind.SUPPORT_ACCEPTANCE_BREAK)
    )
    assert alert.event_subtype == "SUPPORT_ACCEPTANCE_BREAK"


# ---------------------------------------------------------------------------
# Field threading
# ---------------------------------------------------------------------------


def test_severity_threads_through() -> None:
    alert = translate_to_phase9_alert(
        _event(severity=AlertSeverity.CRITICAL)
    )
    assert alert.severity is AlertSeverity.CRITICAL


def test_pair_threads_through() -> None:
    alert = translate_to_phase9_alert(_event(pair="EURUSD"))
    assert alert.pair == "EURUSD"


def test_full_text_and_short_text_thread_through() -> None:
    alert = translate_to_phase9_alert(
        _event(full_text="long body", short_text="short")
    )
    assert alert.full_text == "long body"
    assert alert.short_text == "short"


# ---------------------------------------------------------------------------
# Timestamp policy
# ---------------------------------------------------------------------------


def test_timestamp_defaults_to_event_timestamp() -> None:
    alert = translate_to_phase9_alert(_event())
    assert alert.timestamp == _TS


def test_clock_callable_overrides_event_timestamp() -> None:
    dispatch = datetime(2026, 5, 16, 9, 6, tzinfo=timezone.utc)
    alert = translate_to_phase9_alert(_event(), clock=lambda: dispatch)
    assert alert.timestamp == dispatch


def test_clock_called_once() -> None:
    """Defensive: the translator must not call clock() twice — every
    BotLoop's self._clock() is allowed to be non-idempotent."""
    calls = []
    def clock() -> datetime:
        calls.append(1)
        return _TS
    translate_to_phase9_alert(_event(), clock=clock)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Dedupe-key debug injection
# ---------------------------------------------------------------------------


def test_dedupe_key_injected_into_debug() -> None:
    alert = translate_to_phase9_alert(_event(dedupe_key="GBPUSD_MODE_TREND_CONTINUATION"))
    assert alert.debug["dedupe_key"] == "GBPUSD_MODE_TREND_CONTINUATION"


def test_original_debug_fields_preserved() -> None:
    alert = translate_to_phase9_alert(
        _event(debug={"prev_htf_bias": "BULLISH", "score": 7.5})
    )
    assert alert.debug["prev_htf_bias"] == "BULLISH"
    assert alert.debug["score"] == 7.5
    assert "dedupe_key" in alert.debug


def test_empty_event_debug_still_has_dedupe_key() -> None:
    alert = translate_to_phase9_alert(
        _event(debug={}, dedupe_key="K")
    )
    assert alert.debug == {"dedupe_key": "K"}


def test_translation_does_not_mutate_event_debug() -> None:
    """Defensive: the translator builds a NEW debug dict so the
    source event remains identical across multiple translations
    (e.g., if the same event is logged + alerted)."""
    event = _event(debug={"foo": "bar"})
    original_debug = dict(event.debug)
    translate_to_phase9_alert(event)
    assert event.debug == original_debug
    assert "dedupe_key" not in event.debug


# ---------------------------------------------------------------------------
# End-to-end shape
# ---------------------------------------------------------------------------


def test_translated_alert_carries_full_envelope() -> None:
    """One assertion per Phase 9 Alert field — pin the contract."""
    event = AlertEvent(
        kind=AlertEventKind.SUPPORT_ACCEPTANCE_BREAK,
        pair="GBPUSD",
        severity=AlertSeverity.CRITICAL,
        timestamp=_TS,
        dedupe_key="GBPUSD_SUPPORT_ACCEPTANCE_13340",
        full_text="Support broken at 1.33400 (H1, score was 8.1)",
        short_text="support broken @ 1.33400",
        debug={"price": 1.33400, "score": 8.1},
    )
    alert = translate_to_phase9_alert(event)
    assert alert.category is AlertCategory.STRUCTURE
    assert alert.event_subtype == "SUPPORT_ACCEPTANCE_BREAK"
    assert alert.severity is AlertSeverity.CRITICAL
    assert alert.pair == "GBPUSD"
    assert alert.full_text == "Support broken at 1.33400 (H1, score was 8.1)"
    assert alert.short_text == "support broken @ 1.33400"
    assert alert.timestamp == _TS
    assert alert.debug == {
        "price": 1.33400,
        "score": 8.1,
        "dedupe_key": "GBPUSD_SUPPORT_ACCEPTANCE_13340",
    }
