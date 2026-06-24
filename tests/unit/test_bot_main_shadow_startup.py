"""Tests for the SHADOW_MODE STARTUP banner + env reader (Phase 10)."""
from __future__ import annotations

import sys

import pytest

from bot.main import _emit_startup_alert, _read_shadow_mode_env


# ---------------------------------------------------------------------------
# _read_shadow_mode_env — strict truthy parse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["true", "TRUE", "True", "1", "yes", "YES", "on", "ON"])
def test_shadow_mode_env_truthy_values(monkeypatch, raw) -> None:
    monkeypatch.setenv("BOT_SHADOW_MODE", raw)
    assert _read_shadow_mode_env() is True


@pytest.mark.parametrize("raw", ["", "false", "0", "no", "off", "trues", "1.0", "  "])
def test_shadow_mode_env_falsy_values(monkeypatch, raw) -> None:
    monkeypatch.setenv("BOT_SHADOW_MODE", raw)
    assert _read_shadow_mode_env() is False


def test_shadow_mode_env_unset_defaults_to_false(monkeypatch) -> None:
    monkeypatch.delenv("BOT_SHADOW_MODE", raising=False)
    assert _read_shadow_mode_env() is False


def test_shadow_mode_env_strips_whitespace(monkeypatch) -> None:
    monkeypatch.setenv("BOT_SHADOW_MODE", "  true  ")
    assert _read_shadow_mode_env() is True


# ---------------------------------------------------------------------------
# STARTUP banner with SHADOW_MODE
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


def _patch_git(monkeypatch, hash_: str = "abc1234", branch: str = "develop") -> None:
    import bot.main  # noqa: F401
    main_mod = sys.modules["bot.main"]
    monkeypatch.setattr(main_mod, "_git_short_hash", lambda: hash_)
    monkeypatch.setattr(main_mod, "_git_branch_name", lambda: branch)


def test_startup_alert_default_no_shadow_marker(monkeypatch) -> None:
    """shadow_mode=False (default): no [SHADOW MODE] in body or short."""
    _patch_git(monkeypatch)
    alerter = _RecordingAlerter()
    _emit_startup_alert(
        alerter=alerter,  # type: ignore[arg-type]
        ig_env="DEMO",
        pairs=("GBPUSD",),
        hydration_summary={"cached_bars": 100, "rest_bars": 50},
    )
    body = alerter.sent[0].full_text
    assert "[SHADOW MODE]" not in body
    short = alerter.sent[0].short_text
    assert "[SHADOW]" not in short
    # debug carries the flag.
    assert alerter.sent[0].debug["shadow_mode"] is False


def test_startup_alert_with_shadow_mode_appends_marker(monkeypatch) -> None:
    """shadow_mode=True: title line ends with [SHADOW MODE]; short
    text appended with [SHADOW]; debug payload records the flag."""
    _patch_git(monkeypatch)
    alerter = _RecordingAlerter()
    _emit_startup_alert(
        alerter=alerter,  # type: ignore[arg-type]
        ig_env="DEMO",
        pairs=("GBPUSD",),
        hydration_summary={"cached_bars": 100, "rest_bars": 50},
        shadow_mode=True,
    )
    a = alerter.sent[0]
    body = a.full_text
    # First line carries the marker.
    first_line = body.split("\n", 1)[0]
    assert first_line == "\U0001f916 BOT STARTUP [SHADOW MODE]"
    # Short text includes [SHADOW].
    assert "[SHADOW]" in a.short_text
    assert a.debug["shadow_mode"] is True


def test_startup_alert_shadow_mode_preserves_other_lines(monkeypatch) -> None:
    """The shadow marker is additive — Account, Pairs, Hydration,
    Build lines remain unchanged."""
    _patch_git(monkeypatch)
    alerter = _RecordingAlerter()
    _emit_startup_alert(
        alerter=alerter,  # type: ignore[arg-type]
        ig_env="LIVE",
        pairs=("GBPUSD", "EURUSD"),
        hydration_summary={
            "cached_bars": 100, "rest_bars": 50, "degraded_pairs": ["EURUSD"],
        },
        shadow_mode=True,
    )
    body = alerter.sent[0].full_text
    expected_lines = [
        "\U0001f916 BOT STARTUP [SHADOW MODE]",
        "Account: LIVE",
        "Pairs: 2 (GBPUSD, EURUSD)",
        "Hydration: 100 cached, 50 REST (degraded: EURUSD)",
        "Build: abc1234 (develop)",
    ]
    assert body == "\n".join(expected_lines)


# ---------------------------------------------------------------------------
# H1 layer 1 (Phase 10 review): startup-abort guard
# ---------------------------------------------------------------------------


class _FakePositionForGuard:
    def __init__(self, deal_id: str) -> None:
        self.deal_id = deal_id


class _FakePositionManager:
    def __init__(self, positions: list) -> None:
        self._positions = list(positions)

    def all(self) -> list:
        return list(self._positions)


class _FakeBotForGuard:
    def __init__(self, positions: list) -> None:
        self._pm = _FakePositionManager(positions)

    def position_manager_for_startup_check(self):
        return self._pm


def _patch_main_for_guard_test(monkeypatch, *, shadow_mode: bool, positions: list,
                               sent: list, closed: list) -> None:
    """Stub bot.main collaborators so main() runs the guard branch
    without touching IG / FeedManager / preflight."""
    import bot.main  # noqa: F401
    main_mod = sys.modules["bot.main"]

    monkeypatch.setenv("BOT_SHADOW_MODE", "true" if shadow_mode else "false")
    monkeypatch.setattr(main_mod, "load_dotenv", lambda *a, **kw: None)
    monkeypatch.setattr(main_mod, "setup_logging", lambda **kw: None)
    monkeypatch.setattr(main_mod, "_load_config", lambda: type("C", (), {
        "pairs": ("GBPUSD",),
        "pair_to_epic": {"GBPUSD": "CS.D.GBPUSD.TODAY.IP"},
        "log_level": "INFO",
        "log_file": None,
    })())
    # Static preflight passes.
    monkeypatch.setattr(
        main_mod.preflight_mod, "run_static_checks",
        lambda: type("R", (), {
            "ok": True, "results": (), "fail_messages": lambda self=None: [],
        })(),
    )
    # Stub TelegramAlerter to record sent + close.
    class _StubAlerter:
        def send(self, alert): sent.append(alert)
        def tick(self): pass
        def close(self): closed.append(True)
    monkeypatch.setattr(main_mod, "TelegramAlerter", _StubAlerter)
    # Stub _build_runtime so main() never touches the real runtime tree.
    monkeypatch.setattr(
        main_mod, "_build_runtime",
        lambda config, *, alerter=None, shadow_mode=False: (
            _FakeBotForGuard(positions), object(),
        ),
    )


def test_bot_main_refuses_to_start_in_shadow_mode_with_existing_positions(monkeypatch) -> None:
    """H1 layer 1: shadow_mode=true + positions present → CRITICAL
    STARTUP_ABORTED alert + exit code 3 (EXIT_ABORT). Bot never
    reaches signal-handling / hydrate / start_live."""
    sent: list = []
    closed: list = []
    _patch_main_for_guard_test(
        monkeypatch, shadow_mode=True,
        positions=[_FakePositionForGuard("DEAL_1"), _FakePositionForGuard("DEAL_2")],
        sent=sent, closed=closed,
    )
    from bot.main import EXIT_ABORT, main
    code = main()
    assert code == EXIT_ABORT == 3
    # CRITICAL STARTUP_ABORTED alert dispatched.
    aborts = [a for a in sent if a.event_subtype == "STARTUP_ABORTED"]
    assert len(aborts) == 1
    a = aborts[0]
    from alerts import AlertCategory, AlertSeverity
    assert a.severity is AlertSeverity.CRITICAL
    assert a.category is AlertCategory.SYSTEM
    assert "DEAL_1" in a.full_text
    assert "DEAL_2" in a.full_text
    assert a.debug["reason"] == "shadow_mode_with_existing_positions"
    # Alerter was closed (drain).
    assert closed == [True]


def test_shadow_mode_clean_startup_with_no_positions(monkeypatch) -> None:
    """Negative case: shadow_mode=true + zero positions → guard
    passes, bot proceeds past the abort branch.

    We stub _build_runtime to return a fake bot with no positions and
    let main() proceed; we then exercise the post-guard signal-wiring
    path via a recursive failure (mark_ready is missing) which
    confirms execution flowed past the guard. Cleaner: verify EXIT_ABORT
    is NOT returned (i.e., the function would have continued).
    """
    sent: list = []
    closed: list = []
    _patch_main_for_guard_test(
        monkeypatch, shadow_mode=True,
        positions=[],  # zero positions — guard should pass
        sent=sent, closed=closed,
    )
    # Make subsequent steps fail-fast so we don't hang the test on the
    # block-on-shutdown wait. Stub the bot's hydrate to raise; main()
    # will return 1 (hydration failure path), NOT 3 (EXIT_ABORT).
    import bot.main  # noqa: F401
    main_mod = sys.modules["bot.main"]
    class _BotWithBrokenHydrate:
        def position_manager_for_startup_check(self):
            return _FakePositionManager([])
        def hydrate(self):
            raise RuntimeError("test stub: hydrate fail")
        def stop(self, **kw):
            pass
    monkeypatch.setattr(
        main_mod, "_build_runtime",
        lambda config, *, alerter=None, shadow_mode=False: (
            _BotWithBrokenHydrate(), object(),
        ),
    )
    # Signal handler registration calls signal.signal — let it run.
    from bot.main import EXIT_ABORT, main
    code = main()
    # Got past the abort guard (zero positions); failed at hydrate.
    assert code != EXIT_ABORT
    assert code == 1  # hydration / start failure path
    # No STARTUP_ABORTED alert.
    aborts = [a for a in sent if a.event_subtype == "STARTUP_ABORTED"]
    assert aborts == []


# ---------------------------------------------------------------------------
# M5 (Phase 10 review): _build_runtime threads shadow_mode to BotLoop
# ---------------------------------------------------------------------------


def test_build_runtime_threads_shadow_mode_to_bot_loop(monkeypatch) -> None:
    """M5: a future refactor that drops the shadow_mode kwarg from
    _build_runtime would silently default the BotLoop to live mode
    while the [SHADOW MODE] STARTUP banner still fires (banner is
    independently wired). Pin the wiring by capturing the kwargs
    BotLoop receives."""
    import sys
    import bot.main  # noqa: F401
    main_mod = sys.modules["bot.main"]
    captured_kwargs: list = []

    class _RecordingBotLoop:
        def __init__(self, **kw):
            captured_kwargs.append(kw)
            self._shadow_mode = kw["shadow_mode"]

    # Stub every collaborator _build_runtime calls so we don't touch
    # real IG / FeedManager / etc.
    class _FakeSession:
        acc_type = "DEMO"
        account_id = "ACC1"
        service = type("S", (), {
            "ACC_NUMBER": "ACC1",
            "session": type("X", (), {
                "headers": {"CST": "c", "X-SECURITY-TOKEN": "x"},
            })(),
        })()
    monkeypatch.setattr(main_mod, "create_ig_service", lambda: _FakeSession())
    monkeypatch.setattr(main_mod, "IGClient", lambda **kw: object())
    monkeypatch.setattr(
        main_mod.PositionManager, "load_from_path",
        classmethod(lambda cls: object()),
    )
    monkeypatch.setattr(main_mod, "RiskGuard", lambda **kw: object())
    monkeypatch.setattr(main_mod, "Executor", lambda **kw: object())

    from feed.feed_manager import FeedManager
    monkeypatch.setattr(
        FeedManager, "from_pairs",
        classmethod(lambda cls, *a, **kw: object()),
    )
    monkeypatch.setattr(main_mod, "BotLoop", _RecordingBotLoop)

    from bot.types import BotRuntimeConfig
    cfg = BotRuntimeConfig(
        pairs=("GBPUSD",),
        pair_to_epic={"GBPUSD": "CS.D.GBPUSD.TODAY.IP"},
    )
    main_mod._build_runtime(cfg, alerter=None, shadow_mode=True)
    assert len(captured_kwargs) == 1
    assert captured_kwargs[0]["shadow_mode"] is True


def test_build_runtime_default_shadow_mode_is_false(monkeypatch) -> None:
    """M5 negative case: default shadow_mode is False — safety
    default. Catches a regression that flipped the default to True."""
    import sys
    import bot.main  # noqa: F401
    main_mod = sys.modules["bot.main"]
    captured: list = []

    class _RecordingBotLoop:
        def __init__(self, **kw):
            captured.append(kw)

    class _FakeSession:
        acc_type = "DEMO"
        account_id = "ACC1"
        service = type("S", (), {
            "ACC_NUMBER": "ACC1",
            "session": type("X", (), {
                "headers": {"CST": "c", "X-SECURITY-TOKEN": "x"},
            })(),
        })()
    monkeypatch.setattr(main_mod, "create_ig_service", lambda: _FakeSession())
    monkeypatch.setattr(main_mod, "IGClient", lambda **kw: object())
    monkeypatch.setattr(
        main_mod.PositionManager, "load_from_path",
        classmethod(lambda cls: object()),
    )
    monkeypatch.setattr(main_mod, "RiskGuard", lambda **kw: object())
    monkeypatch.setattr(main_mod, "Executor", lambda **kw: object())
    from feed.feed_manager import FeedManager
    monkeypatch.setattr(
        FeedManager, "from_pairs",
        classmethod(lambda cls, *a, **kw: object()),
    )
    monkeypatch.setattr(main_mod, "BotLoop", _RecordingBotLoop)

    from bot.types import BotRuntimeConfig
    cfg = BotRuntimeConfig(
        pairs=("GBPUSD",),
        pair_to_epic={"GBPUSD": "CS.D.GBPUSD.TODAY.IP"},
    )
    main_mod._build_runtime(cfg)  # no shadow_mode kwarg → default
    assert captured[0]["shadow_mode"] is False


# ---------------------------------------------------------------------------
# H2 (Phase 10 review): healthcheck.service whitelists exit 2 as success
# ---------------------------------------------------------------------------


def test_healthcheck_service_unit_declares_success_exit_status() -> None:
    """H2: the systemd unit must whitelist exit 2 (warn) as success
    so `systemctl is-failed` doesn't fire on the locked warn semantic.
    Without this directive, systemd treats every non-zero exit as
    failed and the operator gets weekday warn-noise alerts.
    """
    from pathlib import Path
    repo_root = Path(__file__).resolve().parent.parent.parent
    unit_text = (
        repo_root / "deploy" / "systemd" / "autobot-og-healthcheck.service"
    ).read_text(encoding="utf-8")
    assert "SuccessExitStatus=0 2" in unit_text, (
        "healthcheck.service must declare SuccessExitStatus=0 2 so "
        "the locked warn-exit semantic (exit 2 = first-run, no alert) "
        "doesn't get reported as a failed unit by systemd. See H2 in "
        "docs/healthcheck_deploy_adversarial_review_2026-05-15.md."
    )
