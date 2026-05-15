"""Pre-market healthcheck entrypoint (Phase 10).

Run via systemd timer at 05:45 UTC weekdays. Exits with one of three
codes per the locked plan:

- ``0`` — every check passed.
- ``2`` — at least one check ``warn`` (typically first-run state),
  no failures. systemd treats this as success; operator awareness only.
- ``1`` — at least one check ``fail``. CRITICAL Telegram alert
  dispatched (HEALTHCHECK_FAILED, SYSTEM category) summarising the
  failed checks, then exit 1 so systemd's ``status`` shows a failed
  oneshot run.

The Telegram alerter is constructed inline (this script is a
separate process from the bot) and drained immediately via
``close()`` so the CRITICAL alert ships before the script exits.
The healthcheck deliberately ignores ``BOT_SHADOW_MODE`` — pre-market
diagnostics are mode-agnostic.

Usage::

    python -m bot.healthcheck

systemd unit at ``deploy/systemd/autobot-og-healthcheck.service``
calls this with the bot's ``.env`` loaded via ``EnvironmentFile=``.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

from alerts import Alert, AlertCategory, AlertSeverity, TelegramAlerter

from .constants import BOT_LOG_FILE, BOT_LOG_LEVEL
from .healthcheck_checks import (
    CheckResult,
    check_candle_archives,
    check_disk_space,
    check_ig_auth,
    check_journalctl_errors,
    check_lightstreamer_endpoint,
    check_state_files_parse,
)
from .logging_setup import setup_logging
from .main import _load_config


logger = logging.getLogger("bot.healthcheck")


# Exit-code constants (locked Phase 10 decision).
EXIT_PASS: int = 0
EXIT_WARN: int = 2
EXIT_FAIL: int = 1


def main(argv: Optional[list[str]] = None) -> int:
    """Run all checks, dispatch alert on hard failure, return exit code."""
    load_dotenv()
    setup_logging(level=BOT_LOG_LEVEL, log_file=BOT_LOG_FILE or None)
    logger.info("AutoBot-OG healthcheck starting")

    config = _load_config()
    import os
    acc_type = (os.getenv("IG_ACC_TYPE") or "").upper() or None

    results: list[CheckResult] = _run_all_checks(
        pairs=config.pairs, acc_type=acc_type,
    )
    for r in results:
        logger.info(
            "healthcheck[%s]: status=%s msg=%s",
            r.name, r.status, r.message,
        )

    exit_code = _exit_code_for(results)
    if exit_code == EXIT_FAIL:
        # Construct an alerter inline; CRITICAL bypasses coalescing
        # so a single send + close drains the pending state.
        alerter = TelegramAlerter()
        try:
            _emit_healthcheck_failed_alert(alerter=alerter, results=results)
        finally:
            alerter.close()
    logger.info(
        "AutoBot-OG healthcheck exiting with code %d (%s)",
        exit_code,
        {EXIT_PASS: "pass", EXIT_WARN: "warn", EXIT_FAIL: "fail"}[exit_code],
    )
    return exit_code


def _run_all_checks(
    *, pairs: tuple[str, ...], acc_type: Optional[str],
) -> list[CheckResult]:
    """Run every check in sequence; return results in registration order.

    Order matters for human-readable journalctl output: cheap local
    checks first (file IO), network checks last. A check raising
    unexpectedly is itself a fail (we shouldn't lose visibility just
    because a check function blew up).
    """
    results: list[CheckResult] = []
    for name, fn in (
        ("state_files_parse", lambda: check_state_files_parse()),
        ("candle_archives", lambda: check_candle_archives(pairs=pairs)),
        ("disk_space", lambda: check_disk_space()),
        ("journalctl_errors", lambda: check_journalctl_errors()),
        ("lightstreamer_endpoint",
         lambda: check_lightstreamer_endpoint(acc_type=acc_type)),
        ("ig_auth", lambda: check_ig_auth()),
    ):
        try:
            results.append(fn())
        except Exception as exc:  # noqa: BLE001
            logger.exception("Check %s raised unexpectedly", name)
            results.append(
                CheckResult(
                    name=name,
                    status="fail",
                    message=f"check raised unexpectedly: {type(exc).__name__}: {exc}",
                    debug={"exception": str(exc)},
                )
            )
    return results


def _exit_code_for(results: list[CheckResult]) -> int:
    """Aggregate per-check status to one exit code.

    Locked semantics: any fail → 1; otherwise any warn → 2; else 0.
    """
    if any(r.status == "fail" for r in results):
        return EXIT_FAIL
    if any(r.status == "warn" for r in results):
        return EXIT_WARN
    return EXIT_PASS


def _emit_healthcheck_failed_alert(
    *, alerter: TelegramAlerter, results: list[CheckResult],
) -> None:
    """Construct the CRITICAL HEALTHCHECK_FAILED alert.

    Body lists every failed check name + message, plus warns as
    secondary context (operator may want to see "fail X" alongside
    "warn Y" to triage). CRITICAL severity bypasses coalescing.
    """
    failed = [r for r in results if r.status == "fail"]
    warned = [r for r in results if r.status == "warn"]
    fail_lines = "\n".join(f"  - {r.name}: {r.message}" for r in failed)
    warn_lines = (
        "\n".join(f"  - {r.name}: {r.message}" for r in warned)
        if warned else ""
    )
    body = (
        f"\U0001fa7a HEALTHCHECK FAILED — {len(failed)} hard failure(s)\n"
        f"{fail_lines}"
    )
    if warn_lines:
        body += f"\nWarnings:\n{warn_lines}"
    short = (
        f"healthcheck failed ({len(failed)} fail / {len(warned)} warn)"
    )
    alerter.send(
        Alert(
            category=AlertCategory.SYSTEM,
            event_subtype="HEALTHCHECK_FAILED",
            severity=AlertSeverity.CRITICAL,
            pair=None,
            full_text=body,
            short_text=short,
            timestamp=datetime.now(timezone.utc),
            debug={
                "failed": [r.name for r in failed],
                "warned": [r.name for r in warned],
            },
        )
    )


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main", "EXIT_PASS", "EXIT_WARN", "EXIT_FAIL"]
