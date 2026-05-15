"""Shared types for the Phase 8 bot main loop.

Frozen dataclasses and enums consumed by :py:mod:`bot.loop`,
:py:mod:`bot.preflight`, :py:mod:`bot.main`. Kept here (rather than
inside `loop.py`) so tests can build inputs without depending on the
orchestrator class.

Conventions match the rest of the codebase: all time fields are
timezone-aware ``datetime`` in UTC; dataclasses are frozen unless a
mutable state machine requires otherwise (only
:class:`FailureCounter` mutates).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Lifecycle state machine
# ---------------------------------------------------------------------------


class BotState(Enum):
    """High-level lifecycle state of the bot.

    Transitions live in :class:`bot.loop.BotLoop` —
    see ``_handle_feed_event`` and ``_handle_bar_close``:

    - ``STARTING`` → ``NORMAL`` after :py:meth:`BotLoop.start`
      completes (pre-flight ok, hydration done, ``start_live`` called).
    - ``NORMAL`` → ``STALE`` on :class:`feed.FeedEventKind.FEED_STALE`.
    - ``STALE`` → ``RESUMING`` on :class:`FEED_RESUMED`.
    - ``RESUMING`` → ``NORMAL`` on either
      :class:`GAP_FILLED` or the next live (non-gap-fill) ``BAR_CLOSE``
      — covering the "no gap to fill" / "gap exceeded window" cases
      where ``GAP_FILLED`` never fires.
    - any → ``SHUTTING_DOWN`` on SIGTERM/SIGINT or the consecutive-
      failure threshold trip.

    Signal generation is allowed *only* in ``NORMAL``. Indicator /
    structure / regime updates run on every ``BAR_CLOSE`` regardless
    of state — see :py:meth:`BotLoop._handle_bar_close` for the gate.
    """

    STARTING = "STARTING"
    NORMAL = "NORMAL"
    STALE = "STALE"
    RESUMING = "RESUMING"
    SHUTTING_DOWN = "SHUTTING_DOWN"


# ---------------------------------------------------------------------------
# Runtime configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BotRuntimeConfig:
    """Caller-supplied runtime configuration.

    Phase 6 / Phase 7 module-specific tunables (``FEED_*``, ``RISK_*``,
    ``EXECUTION_*``) are read by their own ``constants.py`` modules at
    import time. The bot only carries top-level switches: which pairs
    to trade, where to log, etc.

    ``pair_to_epic`` defaults to the IG TODAY-spreadbet pattern
    (``"CS.D.{pair}.TODAY.IP"``); override per-pair if a different
    epic is required (CFD, MINI, etc).
    """

    pairs: tuple[str, ...]
    pair_to_epic: dict[str, str]
    log_level: str = "INFO"
    log_file: Optional[str] = None


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """One pre-flight check outcome."""

    name: str
    ok: bool
    message: str


@dataclass(frozen=True)
class PreFlightReport:
    """Aggregate of every pre-flight check that ran.

    ``ok`` is ``True`` iff every check passed. The first failure
    short-circuits the check list (see :py:func:`bot.preflight.run`),
    so the failed check is always the last entry in ``results``.
    """

    results: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    def fail_messages(self) -> list[str]:
        return [f"{r.name}: {r.message}" for r in self.results if not r.ok]


# ---------------------------------------------------------------------------
# Failure tracking — mutable, intentionally
# ---------------------------------------------------------------------------


@dataclass
class FailureCounter:
    """Tracks consecutive failures for a single kind of work.

    The bot keeps two of these: one for feed-event handling (every
    ``BAR_CLOSE`` / ``BAR_UPDATE``) and one for periodic tasks
    (reconciliation, force-close). Each is reset by its own success
    path and trips independently — a flaky 10-min reconciliation
    must not kill the bot on the next live bar.

    ``threshold`` is the consecutive-failure count that flips
    :py:meth:`should_shutdown` to ``True``.
    """

    name: str
    threshold: int
    consecutive: int = 0
    total: int = 0
    last_exception_summary: Optional[str] = None
    last_failure_at_utc: Optional[datetime] = None

    def record_success(self) -> None:
        self.consecutive = 0

    def record_failure(
        self,
        exc: BaseException,
        *,
        now_utc: datetime,
    ) -> None:
        self.consecutive += 1
        self.total += 1
        self.last_exception_summary = f"{type(exc).__name__}: {exc}"
        self.last_failure_at_utc = now_utc

    def should_shutdown(self) -> bool:
        return self.consecutive >= self.threshold


__all__ = [
    "BotRuntimeConfig",
    "BotState",
    "CheckResult",
    "FailureCounter",
    "PreFlightReport",
]
