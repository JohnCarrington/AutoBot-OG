"""Bot-layer tunables (Phase 8).

Same env-override pattern as ``risk/constants.py``,
``execution/constants.py``, ``feed/constants.py``: defaults match the
locked v1 spec; an environment variable named after the constant lets
ops bump it without a deploy.

What lives here:

- **Failure thresholds.** Two independent 5-strike counters
  (event-handler failures vs periodic-task failures) — see
  :class:`bot.types.FailureCounter`.
- **Reconciliation cadence.** 10 minutes; the scheduling logic is
  inline on every ``BAR_CLOSE`` event, no separate timer thread.
- **Shutdown drain.** Maximum wall-clock seconds the main thread
  waits for in-flight broker calls before forcing the LS shutdown.
- **Pre-flight tolerances.** How long to wait after
  ``feed_manager.start_live()`` for subscriptions to settle.
- **Logging defaults.** Override via ``BOT_LOG_LEVEL`` / ``BOT_LOG_FILE``.

What does **NOT** live here (deliberately):

- EOD enforcement hour. Phase 4's :py:meth:`RiskGuard.positions_to_force_close`
  already encapsulates the DST-aware NY close logic and the "fire
  once per day" guard; Phase 8 just calls it on every ``BAR_CLOSE``.
  Don't second-guess Phase 4.
"""
from __future__ import annotations

import os


def _i(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw is not None else default


def _f(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw is not None else default


def _s(name: str, default: str) -> str:
    raw = os.getenv(name)
    return raw if raw is not None else default


# --- Failure thresholds ----------------------------------------------------
# Two independent counters (event vs periodic) — see bot.types.FailureCounter.
# Five consecutive failures of the same kind flips the bot into
# ``SHUTTING_DOWN``; the main thread drains in-flight work and exits 2.
BOT_MAX_CONSECUTIVE_EVENT_FAILURES: int = _i(
    "BOT_MAX_CONSECUTIVE_EVENT_FAILURES", 5
)
BOT_MAX_CONSECUTIVE_PERIODIC_FAILURES: int = _i(
    "BOT_MAX_CONSECUTIVE_PERIODIC_FAILURES", 5
)


# --- Periodic cadence ------------------------------------------------------
# Inline scheduler — checked on every BAR_CLOSE; no separate timer.
BOT_RECONCILIATION_INTERVAL_MIN: int = _i(
    "BOT_RECONCILIATION_INTERVAL_MIN", 10
)


# --- Graceful shutdown -----------------------------------------------------
# On SIGTERM/SIGINT we set ``_shutdown_requested`` and wait up to this
# many seconds for any in-flight broker call to complete (executor
# open / amend / close) before flushing state and tearing down the
# LS connection. Prevents mid-amend state desync.
BOT_SHUTDOWN_INFLIGHT_TIMEOUT_SEC: float = _f(
    "BOT_SHUTDOWN_INFLIGHT_TIMEOUT_SEC", 5.0
)
# Poll interval for the drain loop. Small enough that the typical
# 200-500ms broker call finishes inside one iteration.
BOT_SHUTDOWN_DRAIN_POLL_SEC: float = _f("BOT_SHUTDOWN_DRAIN_POLL_SEC", 0.1)


# --- Pre-flight ------------------------------------------------------------
# After ``feed_manager.start_live()`` returns, wait up to this many
# seconds for every configured pair to be present in the LS
# subscriber's ``subscribed_pairs`` tuple. Missing pairs at deadline
# = pre-flight failure → exit code 1.
BOT_PREFLIGHT_SUBSCRIPTION_TIMEOUT_SEC: float = _f(
    "BOT_PREFLIGHT_SUBSCRIPTION_TIMEOUT_SEC", 10.0
)
BOT_PREFLIGHT_SUBSCRIPTION_POLL_SEC: float = _f(
    "BOT_PREFLIGHT_SUBSCRIPTION_POLL_SEC", 0.25
)


# --- Logging ---------------------------------------------------------------
BOT_LOG_LEVEL: str = _s("BOT_LOG_LEVEL", "INFO")
BOT_LOG_FILE: str = _s("BOT_LOG_FILE", "")  # empty means stderr only
BOT_LOG_FORMAT: str = _s(
    "BOT_LOG_FORMAT",
    "%(asctime)s %(levelname)s %(name)s: %(message)s",
)
BOT_LOG_DATEFMT: str = _s("BOT_LOG_DATEFMT", "%Y-%m-%dT%H:%M:%S")


__all__ = [
    "BOT_LOG_DATEFMT",
    "BOT_LOG_FILE",
    "BOT_LOG_FORMAT",
    "BOT_LOG_LEVEL",
    "BOT_MAX_CONSECUTIVE_EVENT_FAILURES",
    "BOT_MAX_CONSECUTIVE_PERIODIC_FAILURES",
    "BOT_PREFLIGHT_SUBSCRIPTION_POLL_SEC",
    "BOT_PREFLIGHT_SUBSCRIPTION_TIMEOUT_SEC",
    "BOT_RECONCILIATION_INTERVAL_MIN",
    "BOT_SHUTDOWN_DRAIN_POLL_SEC",
    "BOT_SHUTDOWN_INFLIGHT_TIMEOUT_SEC",
]
