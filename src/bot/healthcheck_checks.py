"""Pre-market healthcheck — individual check functions (Phase 10).

Each check is a pure function that returns a :class:`CheckResult`.
The caller (:py:mod:`bot.healthcheck`) accumulates results, decides
the overall exit code, and dispatches a CRITICAL Telegram alert if
any check failed.

Status semantics
----------------

- ``pass`` — check ran cleanly, system is healthy on this dimension.
- ``warn`` — check could not run (probe unavailable, env-not-systemd)
  or returned a state that is acceptable on first run (e.g. position
  state file absent because the bot has never traded). Operator
  should be aware but no Telegram alert fires.
- ``fail`` — check ran and detected a state that should block the
  next trading session. Triggers exit 1 + CRITICAL alert.

Per-check status table (see plan):

==============================  ==========  =====================  ==============
Check                           pass         warn                   fail
==============================  ==========  =====================  ==============
state_files_parse               clean       absent (first-run)     corrupt JSON
candle_archives                 every pair  one or more missing    —
ig_auth                         session ok  —                      auth/network
disk_space                      ≥1GB free   partition not found    <1GB
journalctl_errors               ≤threshold  unavailable / timeout  >threshold
lightstreamer_endpoint          TCP ok      —                      refused/timeout
==============================  ==========  =====================  ==============

The candle-archive check returns ``warn`` (never ``fail``) because
absence is ambiguous between data loss and first-run hydration —
the bot's own hydration step on next start will distinguish them.

The disk-space check uses :py:func:`shutil.disk_usage` against the
data directory's parent partition. The IG auth check delegates to
:py:func:`feed.ig_rest.auth.create_ig_service` so the same code path
that the bot uses on startup is exercised.
"""
from __future__ import annotations

import json
import logging
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Optional


logger = logging.getLogger(__name__)


CheckStatus = Literal["pass", "warn", "fail"]


# Healthcheck-layer tunables. Defaults match the Phase 10 plan; tests
# override via direct kwargs.
HEALTHCHECK_DISK_SPACE_MIN_GB: int = 1
HEALTHCHECK_JOURNALCTL_THRESHOLD: int = 20
HEALTHCHECK_JOURNALCTL_SINCE: str = "24 hours ago"
HEALTHCHECK_JOURNALCTL_UNIT: str = "autobot-og.service"
HEALTHCHECK_JOURNALCTL_TIMEOUT_SEC: float = 10.0
HEALTHCHECK_TCP_TIMEOUT_SEC: float = 5.0


# IG_ACC_TYPE → Lightstreamer host mapping (locked Phase 10 decision).
# The healthcheck derives the host from the same env var that drives
# the bot's LS subscriber selection — single source of truth.
_LS_HOST_BY_ACC_TYPE: dict[str, str] = {
    "DEMO": "demo-apd.marketdatasystems.com",
    "LIVE": "apd.marketdatasystems.com",
}
_LS_DEFAULT_PORT: int = 443


@dataclass(frozen=True)
class CheckResult:
    """One healthcheck result.

    ``debug`` carries diagnostic context (counts, paths, exception
    repr) for the structured journalctl log line. ``message`` is the
    one-line operator-facing summary surfaced in the Telegram body
    when the overall result is ``fail``.
    """

    name: str
    status: CheckStatus
    message: str
    debug: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# State-file parse checks
# ---------------------------------------------------------------------------


def check_state_files_parse(
    *,
    circuit_breaker_path: Optional[Path] = None,
    positions_path: Optional[Path] = None,
) -> CheckResult:
    """Verify the two persistent JSON state files load cleanly.

    Both paths default to the production locations
    (``data/risk/circuit_breaker_state.json`` and
    ``data/execution/positions.json``). Tests override.

    Per-file status:
    - File absent → warn for that file (first-run is the common case).
    - JSON parse error → fail (state corruption — operator action
      required).
    - File loads → pass for that file.

    Aggregate: any-fail → fail; all-warn → warn; otherwise pass.
    """
    from execution.state.positions_state import (
        DEFAULT_STATE_PATH as _POSITIONS_DEFAULT_PATH,
    )
    from risk.state.circuit_breaker_state import (
        DEFAULT_STATE_PATH as _CB_DEFAULT_PATH,
    )

    cb_path = Path(circuit_breaker_path) if circuit_breaker_path else _CB_DEFAULT_PATH
    pos_path = Path(positions_path) if positions_path else _POSITIONS_DEFAULT_PATH

    results: list[tuple[str, str, str]] = []  # (path, status, message)
    for label, path in (("circuit_breaker", cb_path), ("positions", pos_path)):
        if not path.exists():
            results.append((label, "warn", f"{path} absent (first-run)"))
            continue
        try:
            with path.open("r", encoding="utf-8") as fh:
                json.load(fh)
        except json.JSONDecodeError as exc:
            results.append(
                (label, "fail", f"{path} JSON parse error: {exc.msg}")
            )
        except OSError as exc:
            results.append(
                (label, "fail", f"{path} IO error: {exc}")
            )
        else:
            results.append((label, "pass", f"{path} parsed cleanly"))

    statuses = [r[1] for r in results]
    if "fail" in statuses:
        agg = "fail"
    elif all(s == "warn" for s in statuses):
        agg = "warn"
    else:
        agg = "pass"
    summary = "; ".join(f"{label}: {msg}" for label, _, msg in results)
    return CheckResult(
        name="state_files_parse",
        status=agg,
        message=summary,
        debug={
            "circuit_breaker_path": str(cb_path),
            "positions_path": str(pos_path),
            "per_file": [{"label": l, "status": s, "message": m} for l, s, m in results],
        },
    )


# ---------------------------------------------------------------------------
# Candle archive presence check
# ---------------------------------------------------------------------------


def check_candle_archives(
    *,
    pairs: Iterable[str],
    archive_dir: Optional[Path] = None,
    csv_template: Optional[str] = None,
) -> CheckResult:
    """Verify each configured pair has a candle archive CSV.

    Returns warn (not fail) if any pair is missing — absence is
    ambiguous between data loss and first-run hydration. The bot's
    own hydration step on next start will distinguish them.
    """
    from feed.constants import (
        FEED_ARCHIVE_CSV_TEMPLATE as _FEED_TEMPLATE,
        FEED_ARCHIVE_DIR as _FEED_DIR,
    )

    pairs_t = tuple(pairs)
    base = Path(archive_dir) if archive_dir else Path(_FEED_DIR)
    template = csv_template or _FEED_TEMPLATE

    missing: list[str] = []
    present: list[str] = []
    for pair in pairs_t:
        path = base / template.format(pair=pair)
        if path.exists():
            present.append(pair)
        else:
            missing.append(pair)

    if missing:
        return CheckResult(
            name="candle_archives",
            status="warn",
            message=(
                f"{len(missing)}/{len(pairs_t)} pair archive(s) missing: "
                f"{', '.join(missing)}"
            ),
            debug={
                "archive_dir": str(base),
                "template": template,
                "pairs": list(pairs_t),
                "missing": missing,
                "present": present,
            },
        )
    return CheckResult(
        name="candle_archives",
        status="pass",
        message=f"all {len(pairs_t)} pair archive(s) present",
        debug={
            "archive_dir": str(base),
            "template": template,
            "pairs": list(pairs_t),
        },
    )


# ---------------------------------------------------------------------------
# IG authentication check
# ---------------------------------------------------------------------------


def check_ig_auth(
    *,
    session_factory: Optional[Callable[[], Any]] = None,
) -> CheckResult:
    """Attempt an IG session via :func:`feed.ig_rest.auth.create_ig_service`.

    The factory seam lets tests inject a stub that raises canned
    exceptions or returns a fake session object. In production the
    default is :func:`feed.ig_rest.auth.create_ig_service`, the same
    function the bot calls on startup — so any auth path the bot
    uses is exercised here.
    """
    if session_factory is None:
        from feed.ig_rest.auth import create_ig_service as _default_factory
        session_factory = _default_factory
    try:
        session = session_factory()
    except Exception as exc:  # noqa: BLE001 — broker auth surfaces every kind of error
        return CheckResult(
            name="ig_auth",
            status="fail",
            message=f"IG auth failed: {type(exc).__name__}: {exc}",
            debug={"exception_type": type(exc).__name__, "exception": str(exc)},
        )
    acc_type = getattr(session, "acc_type", None)
    return CheckResult(
        name="ig_auth",
        status="pass",
        message=f"IG session created (acc_type={acc_type})",
        debug={"acc_type": acc_type},
    )


# ---------------------------------------------------------------------------
# Disk-space check
# ---------------------------------------------------------------------------


def check_disk_space(
    *,
    path: Optional[Path] = None,
    min_free_gb: int = HEALTHCHECK_DISK_SPACE_MIN_GB,
) -> CheckResult:
    """Verify ≥``min_free_gb`` GiB free on the partition holding ``path``.

    Defaults to the project's ``data/`` directory if ``path`` is None.
    Falls back to the cwd if data/ doesn't exist.
    """
    target = Path(path) if path else Path("data")
    if not target.exists():
        target = Path.cwd()
    try:
        usage = shutil.disk_usage(target)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="disk_space",
            status="warn",
            message=f"disk_usage({target}) failed: {type(exc).__name__}: {exc}",
            debug={"path": str(target), "exception": str(exc)},
        )
    free_gb = usage.free / (1024 ** 3)
    if free_gb < min_free_gb:
        return CheckResult(
            name="disk_space",
            status="fail",
            message=f"only {free_gb:.2f} GiB free on {target} (need ≥{min_free_gb} GiB)",
            debug={
                "path": str(target),
                "free_bytes": usage.free,
                "free_gb": free_gb,
                "min_free_gb": min_free_gb,
            },
        )
    return CheckResult(
        name="disk_space",
        status="pass",
        message=f"{free_gb:.2f} GiB free on {target}",
        debug={
            "path": str(target),
            "free_gb": free_gb,
            "min_free_gb": min_free_gb,
        },
    )


# ---------------------------------------------------------------------------
# journalctl error-rate check
# ---------------------------------------------------------------------------


def check_journalctl_errors(
    *,
    threshold: int = HEALTHCHECK_JOURNALCTL_THRESHOLD,
    since: str = HEALTHCHECK_JOURNALCTL_SINCE,
    unit: str = HEALTHCHECK_JOURNALCTL_UNIT,
    timeout_sec: float = HEALTHCHECK_JOURNALCTL_TIMEOUT_SEC,
    runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
) -> CheckResult:
    """Count ERROR/CRITICAL log lines in the last ``since`` window.

    Dev-environment fallback: any of the following degrades to warn
    rather than fail (the dev box may not run systemd at all):

    - ``FileNotFoundError`` (no ``journalctl`` binary on PATH)
    - ``subprocess.TimeoutExpired`` (>10s, journal is slow / stuck)
    - non-zero return code (unit doesn't exist on this host)

    The ``runner`` seam lets tests inject a stub that returns canned
    :class:`CompletedProcess` instances or raises specific exceptions.
    """
    cmd = [
        "journalctl",
        "-u", unit,
        "--since", since,
        "--priority=err",
        "--no-pager",
        "-q",
    ]
    invoker = runner or subprocess.run
    try:
        result = invoker(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except FileNotFoundError:
        return CheckResult(
            name="journalctl_errors",
            status="warn",
            message="journalctl not available (dev environment)",
            debug={"unit": unit, "since": since},
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="journalctl_errors",
            status="warn",
            message=f"journalctl timed out (>{timeout_sec:.0f}s)",
            debug={"unit": unit, "since": since, "timeout_sec": timeout_sec},
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="journalctl_errors",
            status="warn",
            message=f"journalctl invocation failed: {type(exc).__name__}: {exc}",
            debug={"unit": unit, "exception": str(exc)},
        )
    if result.returncode != 0:
        # Most commonly: unit does not exist on this host (dev env).
        return CheckResult(
            name="journalctl_errors",
            status="warn",
            message=(
                f"journalctl exit={result.returncode} (likely "
                f"unit-not-found on this host)"
            ),
            debug={
                "unit": unit,
                "returncode": result.returncode,
                "stderr": (result.stderr or "")[:500],
            },
        )
    error_count = sum(
        1 for line in (result.stdout or "").splitlines() if line.strip()
    )
    if error_count > threshold:
        return CheckResult(
            name="journalctl_errors",
            status="fail",
            message=(
                f"{error_count} ERROR/CRITICAL log line(s) in last "
                f"{since} (threshold={threshold})"
            ),
            debug={
                "unit": unit,
                "since": since,
                "count": error_count,
                "threshold": threshold,
            },
        )
    return CheckResult(
        name="journalctl_errors",
        status="pass",
        message=f"{error_count} error line(s) in {since} (threshold={threshold})",
        debug={
            "unit": unit,
            "since": since,
            "count": error_count,
            "threshold": threshold,
        },
    )


# ---------------------------------------------------------------------------
# Lightstreamer endpoint reachability
# ---------------------------------------------------------------------------


def lightstreamer_host_for(acc_type: Optional[str]) -> str:
    """Return the LS host for the given IG_ACC_TYPE (DEMO or LIVE).

    Centralised so the healthcheck and any future ops tooling pick
    the same value. Unknown acc_type defaults to DEMO (safer — a
    failed live-host check on a misconfigured dev box is a more
    confusing signal than a passing demo-host check).
    """
    if acc_type is None:
        return _LS_HOST_BY_ACC_TYPE["DEMO"]
    return _LS_HOST_BY_ACC_TYPE.get(acc_type.upper(), _LS_HOST_BY_ACC_TYPE["DEMO"])


def check_lightstreamer_endpoint(
    *,
    acc_type: Optional[str] = None,
    host: Optional[str] = None,
    port: int = _LS_DEFAULT_PORT,
    timeout_sec: float = HEALTHCHECK_TCP_TIMEOUT_SEC,
    connector: Optional[Callable[..., socket.socket]] = None,
) -> CheckResult:
    """TCP-connect to the Lightstreamer endpoint and close immediately.

    No TLS handshake (avoids cert-validation noise), no LS protocol —
    just verifies the endpoint accepts a TCP connection within
    ``timeout_sec`` seconds. Sufficient signal for "is LS reachable
    from this droplet" without depending on the LS library.

    ``acc_type`` (default from the IG_ACC_TYPE env var) selects the
    host via :func:`lightstreamer_host_for`. Explicit ``host``
    overrides; tests use the ``connector`` seam to swap
    :func:`socket.create_connection`.
    """
    resolved_host = host or lightstreamer_host_for(acc_type)
    connect = connector or socket.create_connection
    try:
        sock = connect((resolved_host, port), timeout=timeout_sec)
    except OSError as exc:
        return CheckResult(
            name="lightstreamer_endpoint",
            status="fail",
            message=(
                f"TCP connect to {resolved_host}:{port} failed: "
                f"{type(exc).__name__}: {exc}"
            ),
            debug={
                "host": resolved_host,
                "port": port,
                "acc_type": acc_type,
                "exception": str(exc),
            },
        )
    try:
        sock.close()
    except Exception:  # noqa: BLE001 — close errors are uninteresting
        pass
    return CheckResult(
        name="lightstreamer_endpoint",
        status="pass",
        message=f"TCP {resolved_host}:{port} reachable",
        debug={"host": resolved_host, "port": port, "acc_type": acc_type},
    )


__all__ = [
    "CheckResult",
    "CheckStatus",
    "HEALTHCHECK_DISK_SPACE_MIN_GB",
    "HEALTHCHECK_JOURNALCTL_SINCE",
    "HEALTHCHECK_JOURNALCTL_THRESHOLD",
    "HEALTHCHECK_JOURNALCTL_TIMEOUT_SEC",
    "HEALTHCHECK_JOURNALCTL_UNIT",
    "HEALTHCHECK_TCP_TIMEOUT_SEC",
    "check_candle_archives",
    "check_disk_space",
    "check_ig_auth",
    "check_journalctl_errors",
    "check_lightstreamer_endpoint",
    "check_state_files_parse",
    "lightstreamer_host_for",
]
