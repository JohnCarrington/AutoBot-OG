"""Tests for bot.healthcheck.main — exit-code aggregation + alert dispatch."""
from __future__ import annotations

import logging

import pytest

from bot.healthcheck import (
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_WARN,
    _emit_healthcheck_failed_alert,
    _exit_code_for,
    _run_all_checks,
    main,
)
from bot.healthcheck_checks import CheckResult


# ---------------------------------------------------------------------------
# _exit_code_for — pure aggregation
# ---------------------------------------------------------------------------


def test_exit_code_all_pass_is_zero() -> None:
    results = [
        CheckResult(name="a", status="pass", message="ok"),
        CheckResult(name="b", status="pass", message="ok"),
    ]
    assert _exit_code_for(results) == EXIT_PASS == 0


def test_exit_code_warns_with_no_fails_is_two() -> None:
    """Locked semantics: any warn but no fail → exit 2 (warn).
    systemd treats 2 as success; operator awareness only."""
    results = [
        CheckResult(name="a", status="pass", message="ok"),
        CheckResult(name="b", status="warn", message="first-run"),
    ]
    assert _exit_code_for(results) == EXIT_WARN == 2


def test_exit_code_one_fail_is_one() -> None:
    """Any fail (regardless of warns/passes) → exit 1."""
    results = [
        CheckResult(name="a", status="pass", message="ok"),
        CheckResult(name="b", status="fail", message="broken"),
        CheckResult(name="c", status="warn", message="hmm"),
    ]
    assert _exit_code_for(results) == EXIT_FAIL == 1


def test_exit_code_multiple_fails_is_still_one() -> None:
    results = [
        CheckResult(name="a", status="fail", message="x"),
        CheckResult(name="b", status="fail", message="y"),
    ]
    assert _exit_code_for(results) == EXIT_FAIL


def test_exit_code_empty_results_is_zero() -> None:
    """Edge case: no checks ran → degenerate pass."""
    assert _exit_code_for([]) == EXIT_PASS


# ---------------------------------------------------------------------------
# _emit_healthcheck_failed_alert — body shape
# ---------------------------------------------------------------------------


class _RecordingAlerter:
    """L4 (Phase 9 cleanup pattern): assert isinstance(alert, Alert)."""

    def __init__(self) -> None:
        self.sent: list = []

    def send(self, alert) -> None:
        from alerts import Alert
        assert isinstance(alert, Alert), (
            f"_RecordingAlerter.send expected an Alert, got {type(alert).__name__}"
        )
        self.sent.append(alert)

    def tick(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_emit_healthcheck_failed_alert_lists_failures() -> None:
    alerter = _RecordingAlerter()
    results = [
        CheckResult(name="ig_auth", status="fail", message="auth rejected"),
        CheckResult(name="disk_space", status="fail", message="0.05 GiB free"),
        CheckResult(name="state_files_parse", status="warn", message="absent (first-run)"),
        CheckResult(name="lightstreamer_endpoint", status="pass", message="ok"),
    ]
    _emit_healthcheck_failed_alert(alerter=alerter, results=results)  # type: ignore[arg-type]
    assert len(alerter.sent) == 1
    a = alerter.sent[0]
    from alerts import AlertCategory, AlertSeverity
    assert a.event_subtype == "HEALTHCHECK_FAILED"
    assert a.severity is AlertSeverity.CRITICAL
    assert a.category is AlertCategory.SYSTEM
    assert a.pair is None
    body = a.full_text
    # Body lists fail names + messages.
    assert "ig_auth: auth rejected" in body
    assert "disk_space: 0.05 GiB free" in body
    # 2 hard failure(s) in the title line.
    assert "2 hard failure(s)" in body
    # Warns surface as secondary context.
    assert "state_files_parse" in body
    assert "absent" in body
    # Pass entries do NOT appear in the body.
    assert "lightstreamer_endpoint" not in body
    # Debug payload carries structured names.
    assert a.debug["failed"] == ["ig_auth", "disk_space"]
    assert a.debug["warned"] == ["state_files_parse"]


def test_emit_healthcheck_failed_alert_omits_warning_section_when_no_warns() -> None:
    alerter = _RecordingAlerter()
    results = [
        CheckResult(name="ig_auth", status="fail", message="auth rejected"),
    ]
    _emit_healthcheck_failed_alert(alerter=alerter, results=results)  # type: ignore[arg-type]
    body = alerter.sent[0].full_text
    assert "Warnings:" not in body


def test_emit_healthcheck_failed_alert_short_text_summary() -> None:
    alerter = _RecordingAlerter()
    results = [
        CheckResult(name="ig_auth", status="fail", message="x"),
        CheckResult(name="disk_space", status="warn", message="y"),
    ]
    _emit_healthcheck_failed_alert(alerter=alerter, results=results)  # type: ignore[arg-type]
    a = alerter.sent[0]
    assert "1 fail" in a.short_text
    assert "1 warn" in a.short_text


# ---------------------------------------------------------------------------
# _run_all_checks — wraps an unexpected exception as fail
# ---------------------------------------------------------------------------


def test_run_all_checks_handles_unexpected_exception(monkeypatch) -> None:
    """If a check function raises (programmer error), the runner
    converts the exception to a fail CheckResult so we don't lose
    visibility on the rest of the suite."""
    import bot.healthcheck as hc_mod

    def _boom(*args, **kw):
        raise RuntimeError("internal blowup")

    # Replace the candle_archives check to raise.
    monkeypatch.setattr(hc_mod, "check_candle_archives", _boom)
    # And neutralise IG auth + LS so we don't try to hit real services.
    monkeypatch.setattr(
        hc_mod, "check_ig_auth",
        lambda **kw: CheckResult(name="ig_auth", status="pass", message="ok"),
    )
    monkeypatch.setattr(
        hc_mod, "check_lightstreamer_endpoint",
        lambda **kw: CheckResult(name="lightstreamer_endpoint", status="pass", message="ok"),
    )
    monkeypatch.setattr(
        hc_mod, "check_journalctl_errors",
        lambda **kw: CheckResult(name="journalctl_errors", status="pass", message="ok"),
    )
    monkeypatch.setattr(
        hc_mod, "check_disk_space",
        lambda **kw: CheckResult(name="disk_space", status="pass", message="ok"),
    )
    monkeypatch.setattr(
        hc_mod, "check_state_files_parse",
        lambda **kw: CheckResult(name="state_files_parse", status="pass", message="ok"),
    )
    results = _run_all_checks(pairs=("GBPUSD",), acc_type="DEMO")
    archive_result = next(r for r in results if r.name == "candle_archives")
    assert archive_result.status == "fail"
    assert "raised unexpectedly" in archive_result.message
    assert "RuntimeError" in archive_result.message


# ---------------------------------------------------------------------------
# main — end-to-end exit-code + alert dispatch
# ---------------------------------------------------------------------------


def _patch_all_checks_to(monkeypatch, statuses: dict[str, str]) -> None:
    """Swap each check fn for a stub returning the given status."""
    import bot.healthcheck as hc_mod
    for name in (
        "check_state_files_parse",
        "check_candle_archives",
        "check_disk_space",
        "check_journalctl_errors",
        "check_lightstreamer_endpoint",
        "check_ig_auth",
    ):
        s = statuses.get(name, "pass")
        monkeypatch.setattr(
            hc_mod, name,
            lambda *a, _s=s, _n=name, **kw: CheckResult(
                name=_n.replace("check_", ""), status=_s, message=f"{_n} stub:{_s}",
            ),
        )


def test_main_all_pass_returns_zero_no_alert(monkeypatch) -> None:
    _patch_all_checks_to(monkeypatch, {})  # all default pass
    sent: list = []

    class _Alerter:
        def __init__(self): pass
        def send(self, a): sent.append(a)
        def tick(self): pass
        def close(self): pass

    import bot.healthcheck as hc_mod
    monkeypatch.setattr(hc_mod, "TelegramAlerter", _Alerter)
    monkeypatch.setattr(
        hc_mod, "_load_config",
        lambda: type("C", (), {"pairs": ("GBPUSD",)})(),
    )
    code = main()
    assert code == EXIT_PASS
    assert sent == []  # no alert on pass


def test_main_one_warn_returns_two_no_alert(monkeypatch) -> None:
    _patch_all_checks_to(monkeypatch, {"check_state_files_parse": "warn"})
    sent: list = []

    class _Alerter:
        def __init__(self): pass
        def send(self, a): sent.append(a)
        def tick(self): pass
        def close(self): pass

    import bot.healthcheck as hc_mod
    monkeypatch.setattr(hc_mod, "TelegramAlerter", _Alerter)
    monkeypatch.setattr(
        hc_mod, "_load_config",
        lambda: type("C", (), {"pairs": ("GBPUSD",)})(),
    )
    code = main()
    assert code == EXIT_WARN
    # Warn never alerts — locked semantic.
    assert sent == []


def test_main_one_fail_returns_one_with_alert(monkeypatch) -> None:
    _patch_all_checks_to(monkeypatch, {"check_ig_auth": "fail"})
    sent: list = []
    closed: list = []

    class _Alerter:
        def __init__(self): pass
        def send(self, a): sent.append(a)
        def tick(self): pass
        def close(self): closed.append(True)

    import bot.healthcheck as hc_mod
    monkeypatch.setattr(hc_mod, "TelegramAlerter", _Alerter)
    monkeypatch.setattr(
        hc_mod, "_load_config",
        lambda: type("C", (), {"pairs": ("GBPUSD",)})(),
    )
    code = main()
    assert code == EXIT_FAIL
    assert len(sent) == 1
    from alerts import AlertSeverity
    assert sent[0].severity is AlertSeverity.CRITICAL
    assert sent[0].event_subtype == "HEALTHCHECK_FAILED"
    # Alerter was closed (drain) before exit.
    assert closed == [True]


def test_main_alerter_closed_even_if_send_raises(monkeypatch) -> None:
    """The close() must run even if send() raises — otherwise the
    alerter's pending state could leak across runs of the daemonless
    healthcheck (each main() builds a fresh alerter, but the pattern
    shouldn't depend on close-before-exit being a happy path only)."""
    _patch_all_checks_to(monkeypatch, {"check_ig_auth": "fail"})
    closed: list = []

    class _Alerter:
        def __init__(self): pass
        def send(self, a):
            raise RuntimeError("alerter exploded")
        def tick(self): pass
        def close(self): closed.append(True)

    import bot.healthcheck as hc_mod
    monkeypatch.setattr(hc_mod, "TelegramAlerter", _Alerter)
    monkeypatch.setattr(
        hc_mod, "_load_config",
        lambda: type("C", (), {"pairs": ("GBPUSD",)})(),
    )
    with pytest.raises(RuntimeError, match="alerter exploded"):
        main()
    # close() ran via the try/finally despite send() raising.
    assert closed == [True]
