"""Tests for bot.main — the entrypoint composition.

We don't exercise the full _build_runtime (that would hit the real IG
SDK and Lightstreamer factory). Instead we test the helpers that have
no IO dependency: config loading and token extraction.
"""
from __future__ import annotations

import pytest

from bot.main import (
    _emit_shutdown_alert,
    _emit_startup_alert,
    _extract_tokens,
    _git_command,
    _load_config,
)


def test_load_config_defaults_to_pair_config_pairs(monkeypatch) -> None:
    monkeypatch.delenv("BOT_PAIRS", raising=False)
    cfg = _load_config()
    # From config.pair_config.PAIRS — locked v1 list.
    assert "GBPUSD" in cfg.pairs
    assert all(epic.startswith("CS.D.") for epic in cfg.pair_to_epic.values())


def test_load_config_respects_bot_pairs_env_var(monkeypatch) -> None:
    monkeypatch.setenv("BOT_PAIRS", "GBPUSD,EURUSD")
    cfg = _load_config()
    assert cfg.pairs == ("GBPUSD", "EURUSD")
    assert cfg.pair_to_epic == {
        "GBPUSD": "CS.D.GBPUSD.TODAY.IP",
        "EURUSD": "CS.D.EURUSD.TODAY.IP",
    }


def test_load_config_strips_whitespace_and_upper_cases_pairs(monkeypatch) -> None:
    monkeypatch.setenv("BOT_PAIRS", " gbpusd , eurusd ,")
    cfg = _load_config()
    assert cfg.pairs == ("GBPUSD", "EURUSD")


def test_extract_tokens_from_session_headers() -> None:
    class _S:
        class _Service:
            class session:
                headers = {"CST": "cst-tok", "X-SECURITY-TOKEN": "xst-tok"}
        service = _Service()
    cst, xst = _extract_tokens(_S())
    assert cst == "cst-tok"
    assert xst == "xst-tok"


def test_extract_tokens_case_insensitive_keys() -> None:
    class _S:
        class _Service:
            class session:
                headers = {"cst": "C", "x-security-token": "X"}
        service = _Service()
    cst, xst = _extract_tokens(_S())
    assert (cst, xst) == ("C", "X")


def test_extract_tokens_raises_on_missing_headers() -> None:
    class _S:
        class _Service:
            session = None
        service = _Service()
    with pytest.raises(RuntimeError, match="no session headers"):
        _extract_tokens(_S())


def test_extract_tokens_raises_on_missing_cst() -> None:
    class _S:
        class _Service:
            class session:
                headers = {"X-SECURITY-TOKEN": "X"}
        service = _Service()
    with pytest.raises(RuntimeError, match="CST"):
        _extract_tokens(_S())


# ---------------------------------------------------------------------------
# Phase 9 commit 2b — STARTUP / SHUTDOWN alert helpers + git fallback
# ---------------------------------------------------------------------------


class _RecordingAlerter:
    def __init__(self) -> None:
        self.sent: list = []

    def send(self, alert) -> None:
        self.sent.append(alert)

    def tick(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_git_command_returns_unknown_on_failure(monkeypatch) -> None:
    """No-git environment (deployed from tarball, container without
    git binary) must not crash the bot — degrade to "unknown"."""
    import subprocess as sp
    def _raise(*args, **kw):
        raise FileNotFoundError("git not on PATH")
    monkeypatch.setattr(sp, "run", _raise)
    assert _git_command(["rev-parse", "--short", "HEAD"]) == "unknown"


def test_git_command_returns_unknown_on_empty_output(monkeypatch) -> None:
    """Whitespace-only output also degrades to "unknown" so the alert
    body never reads "Build:  ()" with empty fields."""
    import subprocess as sp
    class _R:
        stdout = "   \n"
    monkeypatch.setattr(sp, "run", lambda *a, **kw: _R())
    assert _git_command(["rev-parse", "--short", "HEAD"]) == "unknown"


def test_emit_startup_alert_payload_shape(monkeypatch) -> None:
    """STARTUP alert format: bot emoji + Account / Pairs / Hydration /
    Build lines. Build hash + branch come from git or "unknown"."""
    # ``bot.main`` (attribute) is the func because ``bot/__init__.py``
    # re-exports it; reach the submodule via sys.modules instead.
    import sys
    import bot.main  # noqa: F401  — populate sys.modules
    main_mod = sys.modules["bot.main"]
    monkeypatch.setattr(main_mod, "_git_short_hash", lambda: "abc1234")
    monkeypatch.setattr(
        main_mod, "_git_branch_name", lambda: "feature/alerts-integration",
    )
    alerter = _RecordingAlerter()
    _emit_startup_alert(
        alerter=alerter,  # type: ignore[arg-type]
        ig_env="DEMO",
        pairs=("GBPUSD", "EURUSD"),
        hydration_summary={"cached_bars": 180, "rest_bars": 70},
    )
    assert len(alerter.sent) == 1
    a = alerter.sent[0]
    from alerts import AlertCategory, AlertSeverity
    assert a.event_subtype == "STARTUP"
    assert a.severity is AlertSeverity.INFO
    assert a.category is AlertCategory.SYSTEM
    assert a.pair is None
    body = a.full_text
    assert "BOT STARTUP" in body
    assert "Account: DEMO" in body
    assert "Pairs: 2 (GBPUSD, EURUSD)" in body
    assert "Hydration: 180 cached, 70 REST" in body
    assert "Build: abc1234 (feature/alerts-integration)" in body


def test_emit_startup_alert_with_unknown_git(monkeypatch) -> None:
    """When git is unavailable both the hash and branch render as
    "unknown" — the alert still ships."""
    import sys
    import bot.main  # noqa: F401
    main_mod = sys.modules["bot.main"]
    monkeypatch.setattr(main_mod, "_git_short_hash", lambda: "unknown")
    monkeypatch.setattr(main_mod, "_git_branch_name", lambda: "unknown")
    alerter = _RecordingAlerter()
    _emit_startup_alert(
        alerter=alerter,  # type: ignore[arg-type]
        ig_env="LIVE",
        pairs=("GBPUSD",),
        hydration_summary={"cached_bars": 0, "rest_bars": 100},
    )
    body = alerter.sent[0].full_text
    assert "Build: unknown (unknown)" in body
    assert "Account: LIVE" in body


def test_emit_shutdown_alert_clean_is_info(monkeypatch) -> None:
    alerter = _RecordingAlerter()
    _emit_shutdown_alert(alerter=alerter, crashed=False)  # type: ignore[arg-type]
    a = alerter.sent[0]
    from alerts import AlertCategory, AlertSeverity
    assert a.event_subtype == "SHUTDOWN"
    assert a.severity is AlertSeverity.INFO
    assert a.category is AlertCategory.SYSTEM
    assert a.pair is None
    assert "BOT SHUTDOWN" in a.full_text
    assert "clean" in a.full_text.lower()


def test_emit_shutdown_alert_crashed_is_critical(monkeypatch) -> None:
    """Crashed shutdown emits CRITICAL — bypasses coalescing so the
    alert ships immediately even if the close-drain is slow."""
    alerter = _RecordingAlerter()
    _emit_shutdown_alert(alerter=alerter, crashed=True)  # type: ignore[arg-type]
    a = alerter.sent[0]
    from alerts import AlertSeverity
    assert a.severity is AlertSeverity.CRITICAL
    assert "CRASHED" in a.full_text
