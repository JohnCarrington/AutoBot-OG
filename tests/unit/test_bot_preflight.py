"""Tests for bot.preflight — run_static_checks + verify_subscriptions."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bot.preflight import run_static_checks, verify_subscriptions


_REQUIRED = ("IG_USERNAME", "IG_PASSWORD", "IG_API_KEY", "IG_ACC_TYPE")


def _set_env(monkeypatch) -> None:
    monkeypatch.setenv("IG_USERNAME", "user")
    monkeypatch.setenv("IG_PASSWORD", "pass")
    monkeypatch.setenv("IG_API_KEY", "key")
    monkeypatch.setenv("IG_ACC_TYPE", "DEMO")


class _FakeSession:
    def __init__(self) -> None:
        self.account_id = "ACC123"
        self.service = type("S", (), {"ACC_NUMBER": "ACC123"})()
        self.acc_type = "DEMO"


# ---------------------------------------------------------------------------
# Static checks
# ---------------------------------------------------------------------------


def test_static_checks_pass_with_full_env_and_writable_dirs(tmp_path: Path, monkeypatch) -> None:
    _set_env(monkeypatch)
    report = run_static_checks(
        archive_dir=str(tmp_path / "candles"),
        execution_state_dir=str(tmp_path / "execution"),
        session_factory=lambda: _FakeSession(),
    )
    assert report.ok, report.fail_messages()
    names = [r.name for r in report.results]
    assert names == ["env_vars", "archive_dir", "execution_state_dir", "ig_credentials_parse", "ig_session"]


def test_static_checks_short_circuit_on_missing_env_var(tmp_path: Path, monkeypatch) -> None:
    _set_env(monkeypatch)
    monkeypatch.delenv("IG_API_KEY", raising=False)
    report = run_static_checks(
        archive_dir=str(tmp_path / "candles"),
        execution_state_dir=str(tmp_path / "execution"),
        session_factory=lambda: _FakeSession(),
    )
    assert not report.ok
    # Short-circuit: only env_vars check ran.
    assert [r.name for r in report.results] == ["env_vars"]
    assert "IG_API_KEY" in report.results[0].message


def test_static_checks_short_circuit_on_unwritable_dir(tmp_path: Path, monkeypatch) -> None:
    _set_env(monkeypatch)
    # Make a path under a regular file — mkdir of subdir under a file fails.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    report = run_static_checks(
        archive_dir=str(blocker / "candles"),
        execution_state_dir=str(tmp_path / "execution"),
        session_factory=lambda: _FakeSession(),
    )
    assert not report.ok
    last = report.results[-1]
    assert last.name == "archive_dir"
    assert not last.ok


def test_static_checks_ig_session_failure_short_circuits(tmp_path: Path, monkeypatch) -> None:
    _set_env(monkeypatch)

    def boom() -> Any:
        raise RuntimeError("auth refused")

    report = run_static_checks(
        archive_dir=str(tmp_path / "candles"),
        execution_state_dir=str(tmp_path / "execution"),
        session_factory=boom,
    )
    assert not report.ok
    last = report.results[-1]
    assert last.name == "ig_session"
    assert "auth refused" in last.message


def test_static_checks_session_missing_account_id_is_failure(tmp_path: Path, monkeypatch) -> None:
    _set_env(monkeypatch)
    bad_session = _FakeSession()
    bad_session.account_id = None
    bad_session.service = type("S", (), {"ACC_NUMBER": None})()

    report = run_static_checks(
        archive_dir=str(tmp_path / "candles"),
        execution_state_dir=str(tmp_path / "execution"),
        session_factory=lambda: bad_session,
    )
    assert not report.ok
    assert report.results[-1].name == "ig_session"
    assert "account_id" in report.results[-1].message


# ---------------------------------------------------------------------------
# Subscription verification
# ---------------------------------------------------------------------------


class _FakeSub:
    def __init__(self) -> None:
        self.subscribed_pairs: tuple[str, ...] = ()


def test_verify_subscriptions_succeeds_when_pairs_appear() -> None:
    sub = _FakeSub()
    sub.subscribed_pairs = ("GBPUSD", "EURUSD")
    fake_now = [0.0]

    def fake_clock():
        return fake_now[0]

    def fake_sleep(s: float):
        fake_now[0] += s

    result = verify_subscriptions(
        subscriber=sub,  # type: ignore[arg-type]
        expected_pairs=["GBPUSD", "EURUSD"],
        timeout_sec=1.0,
        poll_sec=0.1,
        sleep=fake_sleep,
        clock=fake_clock,
    )
    assert result.ok


def test_verify_subscriptions_polls_until_present() -> None:
    sub = _FakeSub()
    fake_now = [0.0]

    poll_count = {"n": 0}

    def fake_clock():
        return fake_now[0]

    def fake_sleep(s: float):
        fake_now[0] += s
        poll_count["n"] += 1
        # After 3 polls, the subscriber finally reports both pairs.
        if poll_count["n"] >= 3:
            sub.subscribed_pairs = ("GBPUSD", "EURUSD")

    result = verify_subscriptions(
        subscriber=sub,  # type: ignore[arg-type]
        expected_pairs=["GBPUSD", "EURUSD"],
        timeout_sec=10.0,
        poll_sec=0.1,
        sleep=fake_sleep,
        clock=fake_clock,
    )
    assert result.ok


def test_verify_subscriptions_times_out_when_pairs_missing() -> None:
    sub = _FakeSub()
    sub.subscribed_pairs = ("GBPUSD",)  # EURUSD never lands
    fake_now = [0.0]

    def fake_clock():
        return fake_now[0]

    def fake_sleep(s: float):
        fake_now[0] += s

    result = verify_subscriptions(
        subscriber=sub,  # type: ignore[arg-type]
        expected_pairs=["GBPUSD", "EURUSD"],
        timeout_sec=0.5,
        poll_sec=0.1,
        sleep=fake_sleep,
        clock=fake_clock,
    )
    assert not result.ok
    assert "EURUSD" in result.message
    assert "GBPUSD" in result.message  # the "saw" list
