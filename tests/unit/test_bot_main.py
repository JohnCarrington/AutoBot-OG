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
    """L4 (Phase 9 cleanup): assert isinstance(alert, Alert) so a future
    regression that passes a dict / namespace surfaces here instead of
    slipping through silently."""

    def __init__(self) -> None:
        self.sent: list = []

    def send(self, alert) -> None:
        from alerts import Alert
        assert isinstance(alert, Alert), (
            f"_RecordingAlerter.send expected an Alert instance, "
            f"got {type(alert).__name__}"
        )
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
    # L1 (Phase 9 cleanup): pin the full structure so a refactor that
    # drops the emoji, reorders lines, or joins with `, ` instead of
    # `\n` fails this test instead of silently passing substring
    # checks. The plan locked the format — the test should hold it.
    expected_lines = [
        "\U0001f916 BOT STARTUP",
        "Account: DEMO",
        "Pairs: 2 (GBPUSD, EURUSD)",
        "Hydration: 180 cached, 70 REST",
        "Build: abc1234 (feature/alerts-integration)",
    ]
    assert a.full_text == "\n".join(expected_lines)


def test_emit_startup_alert_surfaces_degraded_pairs(monkeypatch) -> None:
    """M3 (Phase 9 cleanup): when hydration succeeded but one or more
    pairs fell back to cache-only (REST top-up failed), the STARTUP
    alert appends a ``(degraded: <pair list>)`` suffix to the
    Hydration line. The operator's first health-check signal is
    honest about per-pair state, not just the aggregate row counts.
    """
    import sys
    import bot.main  # noqa: F401
    main_mod = sys.modules["bot.main"]
    monkeypatch.setattr(main_mod, "_git_short_hash", lambda: "abc1234")
    monkeypatch.setattr(main_mod, "_git_branch_name", lambda: "develop")
    alerter = _RecordingAlerter()
    _emit_startup_alert(
        alerter=alerter,  # type: ignore[arg-type]
        ig_env="DEMO",
        pairs=("GBPUSD", "EURUSD"),
        hydration_summary={
            "cached_bars": 180,
            "rest_bars": 70,
            "degraded_pairs": ["EURUSD"],
        },
    )
    body = alerter.sent[0].full_text
    assert "Hydration: 180 cached, 70 REST (degraded: EURUSD)" in body
    # debug payload also carries the structured info for log-only consumers.
    assert alerter.sent[0].debug["hydration"]["degraded_pairs"] == ["EURUSD"]


def test_emit_startup_alert_no_degraded_suffix_when_clean(monkeypatch) -> None:
    """When degraded_pairs is empty (or absent), the Hydration line
    has no trailing suffix — the operator sees a clean health line."""
    import sys
    import bot.main  # noqa: F401
    main_mod = sys.modules["bot.main"]
    monkeypatch.setattr(main_mod, "_git_short_hash", lambda: "abc1234")
    monkeypatch.setattr(main_mod, "_git_branch_name", lambda: "develop")
    alerter = _RecordingAlerter()
    _emit_startup_alert(
        alerter=alerter,  # type: ignore[arg-type]
        ig_env="DEMO",
        pairs=("GBPUSD",),
        hydration_summary={
            "cached_bars": 180,
            "rest_bars": 70,
            "degraded_pairs": [],
        },
    )
    body = alerter.sent[0].full_text
    assert "Hydration: 180 cached, 70 REST\n" in body
    assert "degraded" not in body


def test_emit_startup_alert_calls_git_helpers_once_each(monkeypatch) -> None:
    """M7 (Phase 9 cleanup): the pre-cleanup body called
    ``_git_short_hash`` twice (once in body, once in short_text). On a
    git-unresponsive host that's two 2s timeouts → 4s STARTUP stall.
    Pin the single-invocation contract so a future regression doesn't
    silently re-introduce the duplication."""
    import sys
    import bot.main  # noqa: F401
    main_mod = sys.modules["bot.main"]
    hash_calls: list = []
    branch_calls: list = []
    monkeypatch.setattr(
        main_mod,
        "_git_short_hash",
        lambda: hash_calls.append(1) or "abc1234",
    )
    monkeypatch.setattr(
        main_mod,
        "_git_branch_name",
        lambda: branch_calls.append(1) or "develop",
    )
    alerter = _RecordingAlerter()
    _emit_startup_alert(
        alerter=alerter,  # type: ignore[arg-type]
        ig_env="DEMO",
        pairs=("GBPUSD",),
        hydration_summary={"cached_bars": 0, "rest_bars": 0},
    )
    assert len(hash_calls) == 1
    assert len(branch_calls) == 1


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
    # L1 (Phase 9 cleanup): pin the full body shape, not just substrings.
    assert a.full_text == "\U0001f916 BOT SHUTDOWN — clean exit"


def test_emit_shutdown_alert_crashed_is_critical(monkeypatch) -> None:
    """Crashed shutdown emits CRITICAL — bypasses coalescing so the
    alert ships immediately even if the close-drain is slow."""
    alerter = _RecordingAlerter()
    _emit_shutdown_alert(alerter=alerter, crashed=True)  # type: ignore[arg-type]
    a = alerter.sent[0]
    from alerts import AlertSeverity
    assert a.severity is AlertSeverity.CRITICAL
    # L1 (Phase 9 cleanup): exact body, not just "CRASHED" substring.
    assert a.full_text == (
        "\U0001f916 BOT SHUTDOWN (CRASHED) — failure threshold tripped"
    )
