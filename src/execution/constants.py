"""Execution-layer tunables (Phase 6).

All values are read once at import time from environment variables
matching the constant name (``EXECUTION_*`` prefix). Defaults match
``docs/v1_architecture.md`` §6.11 (the Phase 6 module structure
section). Pattern mirrors :py:mod:`risk.constants` and
:py:mod:`strategies.constants`.

Categories:

- **Order sizing** — fixed unit size in v1; sizing-by-balance is
  Phase 7+.
- **Reconciliation** — how often to reconcile, what constitutes a
  "large" SL drift, what counts as a "stale" position.
- **SL management** — BE-move buffer (small offset above entry on
  longs to absorb spread oscillation at exactly +1R), minimum
  delta before an amend is sent.
- **State paths** — where the JSON files live (``data/execution/``
  subtree, gitignored).
- **Retry** — single retry with 2 s delay on amend failure.
"""
from __future__ import annotations

import os


def _f(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw is not None else default


def _i(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw is not None else default


def _b(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _s(name: str, default: str) -> str:
    raw = os.getenv(name)
    return raw if raw is not None else default


# --- Sizing -----------------------------------------------------------------
EXECUTION_DEFAULT_SIZE_UNITS: float = _f("EXECUTION_DEFAULT_SIZE_UNITS", 1.0)


# --- Reconciliation ---------------------------------------------------------
# 10 minutes is the v1 default. The conservative alternative is 5
# minutes — pick that if API allowance budget permits. Slower (e.g.
# 30 min) leaves a window where a manually-closed position keeps
# trailing on phantom local state.
EXECUTION_RECONCILIATION_INTERVAL_MIN: int = _i(
    "EXECUTION_RECONCILIATION_INTERVAL_MIN", 10
)
# Pip distance beyond which a broker-vs-local SL drift escalates from
# INFO (silent update) to WARNING (logged + Telegram on Phase 7).
EXECUTION_SL_DRIFT_WARN_PIPS: float = _f("EXECUTION_SL_DRIFT_WARN_PIPS", 5.0)
# Positions older than this without a fresh confirmation count as
# stale — the operator should investigate (likely a stuck IG ticket).
EXECUTION_STALE_POSITION_HOURS: float = _f(
    "EXECUTION_STALE_POSITION_HOURS", 8.0
)
EXECUTION_BROKER_ORPHAN_ALERT: bool = _b(
    "EXECUTION_BROKER_ORPHAN_ALERT", True
)


# --- SL management ----------------------------------------------------------
# Spec §6.2 reads "move to break-even"; literal interpretation places
# the new SL exactly at entry, which can immediately re-trigger on
# spread oscillation at +1R. Add a small buffer above entry (longs;
# below for shorts) to absorb spread. Default 1 pip — small enough
# to keep the move "free-roll", large enough to clip typical major-FX
# spread oscillation.
EXECUTION_BE_MOVE_BUFFER_PIPS: float = _f("EXECUTION_BE_MOVE_BUFFER_PIPS", 1.0)
# Skip an amend whose movement would be smaller than this — avoids
# pinging the broker on every M5 close when the trail candidate
# drifted < 1 pip.
EXECUTION_SL_AMEND_MIN_DELTA_PIPS: float = _f(
    "EXECUTION_SL_AMEND_MIN_DELTA_PIPS", 1.0
)


# --- Retry ------------------------------------------------------------------
EXECUTION_AMEND_RETRY_COUNT: int = _i("EXECUTION_AMEND_RETRY_COUNT", 1)
EXECUTION_AMEND_RETRY_DELAY_S: float = _f("EXECUTION_AMEND_RETRY_DELAY_S", 2.0)


# --- State paths ------------------------------------------------------------
EXECUTION_STATE_PATH: str = _s(
    "EXECUTION_STATE_PATH", "data/execution/positions.json"
)
EXECUTION_RECONCILIATION_LOG_PATH: str = _s(
    "EXECUTION_RECONCILIATION_LOG_PATH",
    "data/execution/reconciliation_events.jsonl",
)


__all__ = [
    "EXECUTION_AMEND_RETRY_COUNT",
    "EXECUTION_AMEND_RETRY_DELAY_S",
    "EXECUTION_BE_MOVE_BUFFER_PIPS",
    "EXECUTION_BROKER_ORPHAN_ALERT",
    "EXECUTION_DEFAULT_SIZE_UNITS",
    "EXECUTION_RECONCILIATION_INTERVAL_MIN",
    "EXECUTION_RECONCILIATION_LOG_PATH",
    "EXECUTION_SL_AMEND_MIN_DELTA_PIPS",
    "EXECUTION_SL_DRIFT_WARN_PIPS",
    "EXECUTION_STALE_POSITION_HOURS",
    "EXECUTION_STATE_PATH",
]
