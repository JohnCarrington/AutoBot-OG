"""Tests for bot.types — BotState, FailureCounter, PreFlightReport."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bot.types import (
    BotRuntimeConfig,
    BotState,
    CheckResult,
    FailureCounter,
    PreFlightReport,
)


_TS = datetime(2026, 5, 15, 13, 0, tzinfo=timezone.utc)


def test_bot_state_values_are_stable_strings() -> None:
    assert {s.value for s in BotState} == {
        "STARTING", "NORMAL", "STALE", "RESUMING", "SHUTTING_DOWN",
    }


def test_failure_counter_record_failure_increments() -> None:
    fc = FailureCounter(name="event", threshold=5)
    exc = RuntimeError("boom")
    fc.record_failure(exc, now_utc=_TS)
    assert fc.consecutive == 1
    assert fc.total == 1
    assert "RuntimeError: boom" == fc.last_exception_summary
    assert fc.last_failure_at_utc == _TS
    assert not fc.should_shutdown()


def test_failure_counter_record_success_resets_consecutive_only() -> None:
    fc = FailureCounter(name="periodic", threshold=3)
    fc.record_failure(RuntimeError("x"), now_utc=_TS)
    fc.record_failure(RuntimeError("y"), now_utc=_TS)
    fc.record_success()
    assert fc.consecutive == 0
    # Total is the running lifetime count, not reset.
    assert fc.total == 2


def test_failure_counter_trips_at_threshold() -> None:
    fc = FailureCounter(name="event", threshold=3)
    for _ in range(2):
        fc.record_failure(RuntimeError(), now_utc=_TS)
        assert not fc.should_shutdown()
    fc.record_failure(RuntimeError(), now_utc=_TS)
    assert fc.should_shutdown()


def test_failure_counter_success_after_failure_unwinds_trip() -> None:
    fc = FailureCounter(name="event", threshold=2)
    fc.record_failure(RuntimeError(), now_utc=_TS)
    fc.record_success()
    fc.record_failure(RuntimeError(), now_utc=_TS)
    # Only one consecutive failure now → not yet at threshold.
    assert not fc.should_shutdown()


def test_preflight_report_ok_when_all_pass() -> None:
    report = PreFlightReport(
        results=(
            CheckResult(name="a", ok=True, message="ok"),
            CheckResult(name="b", ok=True, message="ok"),
        )
    )
    assert report.ok is True
    assert report.fail_messages() == []


def test_preflight_report_failure_messages_filter_to_failures_only() -> None:
    report = PreFlightReport(
        results=(
            CheckResult(name="a", ok=True, message="passed"),
            CheckResult(name="b", ok=False, message="missing X"),
            CheckResult(name="c", ok=False, message="bad Y"),
        )
    )
    assert report.ok is False
    msgs = report.fail_messages()
    assert msgs == ["b: missing X", "c: bad Y"]


def test_runtime_config_round_trip() -> None:
    cfg = BotRuntimeConfig(
        pairs=("GBPUSD", "EURUSD"),
        pair_to_epic={"GBPUSD": "CS.D.GBPUSD.TODAY.IP", "EURUSD": "CS.D.EURUSD.TODAY.IP"},
        log_level="DEBUG",
        log_file="/tmp/bot.log",
    )
    assert cfg.pairs == ("GBPUSD", "EURUSD")
    assert cfg.log_level == "DEBUG"
    assert cfg.log_file == "/tmp/bot.log"
