"""Tests for structure_alerts.constants — cooldowns, log path,
price quantisation, severity→cooldown mapping."""
from __future__ import annotations

import importlib

import pytest

from alerts import AlertSeverity


# ---------------------------------------------------------------------------
# Cooldowns
# ---------------------------------------------------------------------------


def test_default_cooldowns_match_spec() -> None:
    from structure_alerts import constants

    assert constants.INFO_COOLDOWN_SEC == 2 * 3600
    assert constants.WARNING_COOLDOWN_SEC == 3600
    assert constants.CRITICAL_COOLDOWN_SEC == 30 * 60


def test_cooldown_by_severity_covers_every_severity() -> None:
    from structure_alerts.constants import COOLDOWN_BY_SEVERITY

    for sev in AlertSeverity:
        assert sev in COOLDOWN_BY_SEVERITY
        assert COOLDOWN_BY_SEVERITY[sev] > 0


def test_cooldown_env_override(monkeypatch) -> None:
    # constants reads env at import time; reimport with overrides set.
    monkeypatch.setenv("STRUCTURE_ALERTS_INFO_COOLDOWN_SEC", "60")
    monkeypatch.setenv("STRUCTURE_ALERTS_WARNING_COOLDOWN_SEC", "120")
    monkeypatch.setenv("STRUCTURE_ALERTS_CRITICAL_COOLDOWN_SEC", "30")
    import structure_alerts.constants as constants_mod

    reloaded = importlib.reload(constants_mod)
    try:
        assert reloaded.INFO_COOLDOWN_SEC == 60
        assert reloaded.WARNING_COOLDOWN_SEC == 120
        assert reloaded.CRITICAL_COOLDOWN_SEC == 30
        assert reloaded.COOLDOWN_BY_SEVERITY[AlertSeverity.INFO] == 60
    finally:
        # Restore module to default state so other tests see the
        # locked values. monkeypatch handles env-var rollback but
        # the reloaded module retains the override values until we
        # reload again.
        monkeypatch.delenv("STRUCTURE_ALERTS_INFO_COOLDOWN_SEC", raising=False)
        monkeypatch.delenv("STRUCTURE_ALERTS_WARNING_COOLDOWN_SEC", raising=False)
        monkeypatch.delenv("STRUCTURE_ALERTS_CRITICAL_COOLDOWN_SEC", raising=False)
        importlib.reload(constants_mod)


# ---------------------------------------------------------------------------
# Persistence path
# ---------------------------------------------------------------------------


def test_log_path_default() -> None:
    from structure_alerts.constants import STRUCTURE_ALERTS_LOG_PATH

    assert STRUCTURE_ALERTS_LOG_PATH == "data/alerts/structure_alerts.jsonl"


def test_log_path_env_override(monkeypatch) -> None:
    monkeypatch.setenv("STRUCTURE_ALERTS_LOG_PATH", "/tmp/x.jsonl")
    import structure_alerts.constants as constants_mod

    reloaded = importlib.reload(constants_mod)
    try:
        assert reloaded.STRUCTURE_ALERTS_LOG_PATH == "/tmp/x.jsonl"
    finally:
        monkeypatch.delenv("STRUCTURE_ALERTS_LOG_PATH", raising=False)
        importlib.reload(constants_mod)


def test_structure_alerts_log_path_resolves_at_call_time(monkeypatch) -> None:
    """M4: ``structure_alerts_log_path()`` reads the env per call, so an
    operator override on a running process is honoured. Mirrors
    :func:`structure_engine.logging._log_path` (Phase 11 L-2 fix).
    Without per-call reads, the audit log silently keeps writing to
    the import-time path despite the operator's reconfigure.
    """
    from structure_alerts.constants import structure_alerts_log_path

    monkeypatch.delenv("STRUCTURE_ALERTS_LOG_PATH", raising=False)
    default_path = structure_alerts_log_path()
    # Default — falls back to the module-level constant (import-time
    # capture). Asserting the default is data/alerts/... here pins
    # that the fallback doesn't accidentally regress to "".
    assert default_path == "data/alerts/structure_alerts.jsonl"

    monkeypatch.setenv("STRUCTURE_ALERTS_LOG_PATH", "/tmp/runtime-a.jsonl")
    assert structure_alerts_log_path() == "/tmp/runtime-a.jsonl"

    # Operator changes the env again — same function picks up the
    # new value without an import reload.
    monkeypatch.setenv("STRUCTURE_ALERTS_LOG_PATH", "/tmp/runtime-b.jsonl")
    assert structure_alerts_log_path() == "/tmp/runtime-b.jsonl"

    # Env unset — falls back to the import-time captured value.
    monkeypatch.delenv("STRUCTURE_ALERTS_LOG_PATH", raising=False)
    assert structure_alerts_log_path() == default_path


# ---------------------------------------------------------------------------
# Price quantisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pair,price,expected",
    [
        # Spec §9 example: GBPUSD support acceptance at 1.33400 -> 13340.
        ("GBPUSD", 1.33400, 13340),
        # Sub-pip jitter collapses to the same integer.
        ("GBPUSD", 1.33405, 13340),
        ("GBPUSD", 1.33396, 13340),
        # One pip up flips the integer.
        ("GBPUSD", 1.33410, 13341),
        # Four-decimal pairs.
        ("EURUSD", 1.10500, 11050),
        ("USDCAD", 1.35200, 13520),
        # Two-decimal JPY pair. 152.345 / 0.01 lands on exact 15234.5
        # in float and banker's rounding picks the even neighbour
        # 15234; the next pip up at 152.36 quantises cleanly to 15236.
        ("USDJPY", 152.345, 15234),
        ("USDJPY", 152.36, 15236),
    ],
)
def test_quantise_price_canonical_examples(pair, price, expected) -> None:
    from structure_alerts.constants import quantise_price

    assert quantise_price(pair, price) == expected


def test_quantise_price_unknown_pair_falls_back_to_four_decimals() -> None:
    from structure_alerts.constants import quantise_price

    # pip_size_for() defaults to 0.0001 for unknown pairs.
    assert quantise_price("XAUUSD", 1.23456) == 12346
