"""Tests for bot.healthcheck_checks — per-check unit tests.

All checks are pure functions of injected seams (paths, factories,
runners, connectors). Tests never touch the real network, real
journalctl, or real IG endpoints.
"""
from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest

from bot.healthcheck_checks import (
    CheckResult,
    HEALTHCHECK_DISK_SPACE_MIN_GB,
    check_candle_archives,
    check_disk_space,
    check_ig_auth,
    check_journalctl_errors,
    check_lightstreamer_endpoint,
    check_state_files_parse,
    lightstreamer_host_for,
)


# ---------------------------------------------------------------------------
# CheckResult shape
# ---------------------------------------------------------------------------


def test_check_result_is_frozen() -> None:
    r = CheckResult(name="x", status="pass", message="ok")
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        r.status = "fail"  # type: ignore[misc]


def test_check_result_default_debug_is_independent_dict() -> None:
    r1 = CheckResult(name="x", status="pass", message="ok")
    r2 = CheckResult(name="y", status="pass", message="ok")
    r1.debug["k"] = 1
    assert r2.debug == {}


# ---------------------------------------------------------------------------
# state_files_parse
# ---------------------------------------------------------------------------


def test_state_files_parse_both_clean(tmp_path: Path) -> None:
    cb = tmp_path / "cb.json"
    pos = tmp_path / "pos.json"
    cb.write_text(json.dumps({"version": 1, "trips": []}))
    pos.write_text(json.dumps({"version": 1, "positions": []}))
    r = check_state_files_parse(circuit_breaker_path=cb, positions_path=pos)
    assert r.status == "pass"
    assert "parsed cleanly" in r.message
    # Both files appear in the per-file debug payload.
    labels = [p["label"] for p in r.debug["per_file"]]
    assert labels == ["circuit_breaker", "positions"]


def test_state_files_parse_both_absent_warns(tmp_path: Path) -> None:
    """First-run is the common case — neither file exists yet."""
    cb = tmp_path / "missing_cb.json"
    pos = tmp_path / "missing_pos.json"
    r = check_state_files_parse(circuit_breaker_path=cb, positions_path=pos)
    assert r.status == "warn"
    assert "absent" in r.message
    assert "first-run" in r.message


def test_state_files_parse_corrupt_json_fails(tmp_path: Path) -> None:
    cb = tmp_path / "cb.json"
    pos = tmp_path / "pos.json"
    cb.write_text("{not valid json")
    pos.write_text(json.dumps({"version": 1}))
    r = check_state_files_parse(circuit_breaker_path=cb, positions_path=pos)
    assert r.status == "fail"
    assert "JSON parse error" in r.message
    assert "circuit_breaker" in r.message


def test_state_files_parse_one_clean_one_absent_passes(tmp_path: Path) -> None:
    """One present + one absent → pass (the absent file degrades to
    warn for itself, but at least one cleanly-parsed file means the
    aggregate is pass)."""
    cb = tmp_path / "cb.json"
    pos = tmp_path / "missing.json"
    cb.write_text(json.dumps({"version": 1}))
    r = check_state_files_parse(circuit_breaker_path=cb, positions_path=pos)
    assert r.status == "pass"


def test_state_files_parse_one_clean_one_corrupt_fails(tmp_path: Path) -> None:
    cb = tmp_path / "cb.json"
    pos = tmp_path / "pos.json"
    cb.write_text(json.dumps({"version": 1}))
    pos.write_text("garbage")
    r = check_state_files_parse(circuit_breaker_path=cb, positions_path=pos)
    assert r.status == "fail"


# ---------------------------------------------------------------------------
# candle_archives
# ---------------------------------------------------------------------------


def test_candle_archives_all_present(tmp_path: Path) -> None:
    (tmp_path / "GBPUSD_5m.csv").write_text("col1,col2\n")
    (tmp_path / "EURUSD_5m.csv").write_text("col1,col2\n")
    r = check_candle_archives(
        pairs=("GBPUSD", "EURUSD"),
        archive_dir=tmp_path,
        csv_template="{pair}_5m.csv",
    )
    assert r.status == "pass"
    assert "all 2 pair archive(s) present" in r.message


def test_candle_archives_one_missing_warns(tmp_path: Path) -> None:
    (tmp_path / "GBPUSD_5m.csv").write_text("col1,col2\n")
    r = check_candle_archives(
        pairs=("GBPUSD", "EURUSD"),
        archive_dir=tmp_path,
        csv_template="{pair}_5m.csv",
    )
    assert r.status == "warn"
    assert "EURUSD" in r.message
    assert r.debug["missing"] == ["EURUSD"]


def test_candle_archives_all_missing_warns(tmp_path: Path) -> None:
    """All pairs missing is still warn (never fail) — absence is
    ambiguous between data loss and first-run."""
    r = check_candle_archives(
        pairs=("GBPUSD",),
        archive_dir=tmp_path,
        csv_template="{pair}_5m.csv",
    )
    assert r.status == "warn"
    assert "1/1 pair archive(s) missing" in r.message


def test_candle_archives_uses_phase7_constants_by_default(monkeypatch) -> None:
    """When archive_dir / csv_template are None, pulls from
    feed.constants — single source of truth with the writer."""
    import feed.constants as feed_const
    # Just verify the import path exists and the helper accepts None.
    r = check_candle_archives(pairs=())
    # Empty pairs → 0/0 missing → vacuously pass.
    assert r.status == "pass"
    assert r.debug["archive_dir"] == feed_const.FEED_ARCHIVE_DIR


# ---------------------------------------------------------------------------
# ig_auth
# ---------------------------------------------------------------------------


def test_ig_auth_success_returns_pass() -> None:
    fake_session = type("S", (), {"acc_type": "DEMO"})()
    r = check_ig_auth(session_factory=lambda: fake_session)
    assert r.status == "pass"
    assert "DEMO" in r.message


def test_ig_auth_factory_raises_returns_fail() -> None:
    def _boom():
        raise RuntimeError("invalid credentials")

    r = check_ig_auth(session_factory=_boom)
    assert r.status == "fail"
    assert "RuntimeError" in r.message
    assert "invalid credentials" in r.message


def test_ig_auth_network_error_returns_fail() -> None:
    def _network():
        raise OSError("Connection refused")

    r = check_ig_auth(session_factory=_network)
    assert r.status == "fail"
    assert "OSError" in r.message


# ---------------------------------------------------------------------------
# disk_space
# ---------------------------------------------------------------------------


def test_disk_space_above_threshold_passes(tmp_path: Path) -> None:
    # tmp_path is on the test runner's disk — typically GB available.
    r = check_disk_space(path=tmp_path, min_free_gb=1)
    assert r.status == "pass"
    assert r.debug["free_gb"] >= 1


def test_disk_space_below_threshold_fails(tmp_path: Path, monkeypatch) -> None:
    """Force the threshold above what's actually free."""
    import bot.healthcheck_checks as mod
    # Synthesize a tiny disk_usage result.
    fake_usage = type("U", (), {"total": 1_000_000_000, "used": 999_000_000, "free": 500_000})()
    monkeypatch.setattr(mod.shutil, "disk_usage", lambda _p: fake_usage)
    r = check_disk_space(path=tmp_path, min_free_gb=1)
    assert r.status == "fail"
    assert "only" in r.message
    assert "0.00 GiB free" in r.message


def test_disk_space_path_missing_falls_back_to_cwd() -> None:
    """When the requested path doesn't exist, falls back to cwd
    rather than failing — pre-first-run data/ may not exist yet."""
    nonexistent = Path("/this/should/not/exist/anywhere")
    r = check_disk_space(path=nonexistent, min_free_gb=0)
    # cwd usually has space; pass.
    assert r.status == "pass"


def test_disk_space_disk_usage_raises_warns(tmp_path: Path, monkeypatch) -> None:
    import bot.healthcheck_checks as mod

    def _boom(_p):
        raise OSError("permission denied")

    monkeypatch.setattr(mod.shutil, "disk_usage", _boom)
    r = check_disk_space(path=tmp_path)
    assert r.status == "warn"
    assert "permission denied" in r.message


# ---------------------------------------------------------------------------
# journalctl_errors
# ---------------------------------------------------------------------------


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["journalctl"], returncode=returncode, stdout=stdout, stderr=stderr,
    )


def test_journalctl_errors_zero_lines_passes() -> None:
    r = check_journalctl_errors(runner=lambda *a, **kw: _completed(stdout=""))
    assert r.status == "pass"
    assert r.debug["count"] == 0


def test_journalctl_errors_below_threshold_passes() -> None:
    five_lines = "\n".join(f"line {i}" for i in range(5)) + "\n"
    r = check_journalctl_errors(
        runner=lambda *a, **kw: _completed(stdout=five_lines),
        threshold=20,
    )
    assert r.status == "pass"
    assert r.debug["count"] == 5


def test_journalctl_errors_above_threshold_fails() -> None:
    thirty_lines = "\n".join(f"line {i}" for i in range(30)) + "\n"
    r = check_journalctl_errors(
        runner=lambda *a, **kw: _completed(stdout=thirty_lines),
        threshold=20,
    )
    assert r.status == "fail"
    assert "30 ERROR/CRITICAL" in r.message
    assert "threshold=20" in r.message


def test_journalctl_errors_at_threshold_passes() -> None:
    """Boundary: exactly threshold count is pass (>threshold = fail)."""
    twenty = "\n".join(f"line {i}" for i in range(20)) + "\n"
    r = check_journalctl_errors(
        runner=lambda *a, **kw: _completed(stdout=twenty),
        threshold=20,
    )
    assert r.status == "pass"
    assert r.debug["count"] == 20


def test_journalctl_errors_filenotfound_warns() -> None:
    """No journalctl on PATH (dev environment) → warn, not fail."""
    def _no_journalctl(*args, **kw):
        raise FileNotFoundError("journalctl: command not found")

    r = check_journalctl_errors(runner=_no_journalctl)
    assert r.status == "warn"
    assert "not available" in r.message


def test_journalctl_errors_timeout_warns() -> None:
    def _timeout(*args, **kw):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=10.0)

    r = check_journalctl_errors(runner=_timeout, timeout_sec=10.0)
    assert r.status == "warn"
    assert "timed out" in r.message


def test_journalctl_errors_unit_not_found_warns() -> None:
    """Non-zero return (unit doesn't exist on this host) → warn."""
    r = check_journalctl_errors(
        runner=lambda *a, **kw: _completed(returncode=1, stderr="No such unit"),
    )
    assert r.status == "warn"
    assert "exit=1" in r.message


def test_journalctl_errors_blank_lines_in_stdout_not_counted() -> None:
    """Trailing blanks shouldn't inflate the count."""
    out = "line 1\nline 2\n\n\n"
    r = check_journalctl_errors(
        runner=lambda *a, **kw: _completed(stdout=out),
    )
    assert r.debug["count"] == 2


# ---------------------------------------------------------------------------
# lightstreamer_endpoint
# ---------------------------------------------------------------------------


def test_lightstreamer_host_for_demo() -> None:
    assert lightstreamer_host_for("DEMO") == "demo-apd.marketdatasystems.com"
    assert lightstreamer_host_for("demo") == "demo-apd.marketdatasystems.com"


def test_lightstreamer_host_for_live() -> None:
    assert lightstreamer_host_for("LIVE") == "apd.marketdatasystems.com"
    assert lightstreamer_host_for("live") == "apd.marketdatasystems.com"


def test_lightstreamer_host_for_none_defaults_to_demo() -> None:
    assert lightstreamer_host_for(None) == "demo-apd.marketdatasystems.com"


def test_lightstreamer_host_for_unknown_defaults_to_demo() -> None:
    """Unknown acc_type defaults to DEMO — safer signal than a
    confusing 'live host unreachable' on a misconfigured dev box."""
    assert lightstreamer_host_for("XYZ") == "demo-apd.marketdatasystems.com"


class _FakeSocket:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_lightstreamer_endpoint_connect_succeeds_passes() -> None:
    fake = _FakeSocket()
    r = check_lightstreamer_endpoint(
        acc_type="DEMO",
        connector=lambda addr, timeout=None: fake,
    )
    assert r.status == "pass"
    assert "demo-apd.marketdatasystems.com:443" in r.message
    assert fake.closed is True


def test_lightstreamer_endpoint_live_acc_type_uses_live_host() -> None:
    captured: list = []

    def _connector(addr, timeout=None):
        captured.append(addr)
        return _FakeSocket()

    r = check_lightstreamer_endpoint(acc_type="LIVE", connector=_connector)
    assert r.status == "pass"
    assert captured == [("apd.marketdatasystems.com", 443)]


def test_lightstreamer_endpoint_connection_refused_fails() -> None:
    def _refuse(addr, timeout=None):
        raise ConnectionRefusedError("connection refused")

    r = check_lightstreamer_endpoint(acc_type="DEMO", connector=_refuse)
    assert r.status == "fail"
    assert "ConnectionRefusedError" in r.message


def test_lightstreamer_endpoint_timeout_fails() -> None:
    def _timeout(addr, timeout=None):
        raise socket.timeout("timed out")

    r = check_lightstreamer_endpoint(acc_type="DEMO", connector=_timeout)
    assert r.status == "fail"


def test_lightstreamer_endpoint_explicit_host_overrides_acc_type() -> None:
    captured: list = []

    def _connector(addr, timeout=None):
        captured.append(addr)
        return _FakeSocket()

    r = check_lightstreamer_endpoint(
        acc_type="DEMO",
        host="custom.host.example",
        port=8080,
        connector=_connector,
    )
    assert r.status == "pass"
    assert captured == [("custom.host.example", 8080)]


def test_lightstreamer_endpoint_close_failure_does_not_corrupt_pass() -> None:
    """If sock.close() raises, the check still reports pass — the
    important thing is that connect succeeded."""
    class _BrokenClose:
        def close(self):
            raise OSError("close failed")

    r = check_lightstreamer_endpoint(
        acc_type="DEMO",
        connector=lambda addr, timeout=None: _BrokenClose(),
    )
    assert r.status == "pass"
