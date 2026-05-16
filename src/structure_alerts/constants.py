"""Phase 12 tunables for the structure-alerting layer.

Same env-override pattern as the rest of the codebase. Read once at
import time; runtime env changes do not propagate.

Locked decisions (from the Phase 12 plan §9):

- ``INFO_COOLDOWN_SEC = 7200`` — NEW_MAJOR_LEVEL / LEVEL_INVALIDATED /
  HOURLY_SUMMARY. Two hours per dedupe key.
- ``WARNING_COOLDOWN_SEC = 3600`` — HTF_BIAS_CHANGE /
  STRUCTURE_MODE_CHANGE / SWEEP_RECLAIM / FAILED_RECLAIM. One hour
  per dedupe key.
- ``CRITICAL_COOLDOWN_SEC = 1800`` — SUPPORT_ACCEPTANCE_BREAK /
  RESISTANCE_ACCEPTANCE_BREAK. Thirty minutes per dedupe key. Even
  CRITICAL events dedupe: the operator does not need to be paged
  three times for the same break of the same level inside half an
  hour. CRITICAL still bypasses the Phase 9 *coalescer* once it
  passes the dedupe gate — those are separate concerns.

The cooldown that applies to a given event is selected from
:data:`COOLDOWN_BY_SEVERITY`. Tests parametrise on the full mapping
so a new severity value cannot silently land without a cooldown row.

Price quantisation
------------------

Dedupe keys for level-anchored events (acceptance break / sweep /
reclaim / new major / invalidated) embed the level price quantised
to integer pip count via :func:`quantise_price`. Two prices that
differ by less than one pip collapse to the same dedupe key, so
spec §9 examples like ``GBPUSD_SUPPORT_ACCEPTANCE_13340`` come out
deterministically — 1.33400 / 1.33405 / 1.33399 all quantise to
``13340``.

The quantisation step is one *pip* (per :func:`config.pair_config.pip_size_for`),
not the engine's per-pair minimum-zone width. Rationale: dedupe key
stability across runs is the goal; the zone-merge width may evolve as
the engine's tunables move, but the pip definition is broker-level
and stable.
"""
from __future__ import annotations

import os

from alerts import AlertSeverity
from config.pair_config import pip_size_for


def _i(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw is not None else default


# --- Cooldowns ----------------------------------------------------------
INFO_COOLDOWN_SEC: int = _i("STRUCTURE_ALERTS_INFO_COOLDOWN_SEC", 2 * 3600)
WARNING_COOLDOWN_SEC: int = _i("STRUCTURE_ALERTS_WARNING_COOLDOWN_SEC", 3600)
CRITICAL_COOLDOWN_SEC: int = _i(
    "STRUCTURE_ALERTS_CRITICAL_COOLDOWN_SEC", 30 * 60
)


COOLDOWN_BY_SEVERITY: dict[AlertSeverity, int] = {
    AlertSeverity.INFO: INFO_COOLDOWN_SEC,
    AlertSeverity.WARNING: WARNING_COOLDOWN_SEC,
    AlertSeverity.CRITICAL: CRITICAL_COOLDOWN_SEC,
}


# M6 (cleanup commit): time-based eviction threshold for DedupeCache.
# An entry older than 2× the longest cooldown can no longer affect any
# dedupe outcome, so it is safe to drop. Sweeping on every should_fire
# call keeps the cache size proportional to recent activity rather
# than to the run's total cardinality of (pair, side, quantised_price)
# tuples — over a multi-month run for four pairs, the upper bound on
# the unbounded variant was tens of thousands of entries (~1.5MB).
DEDUPE_MAX_AGE_SEC: int = max(COOLDOWN_BY_SEVERITY.values()) * 2


# --- Persistence --------------------------------------------------------
STRUCTURE_ALERTS_LOG_PATH: str = os.getenv(
    "STRUCTURE_ALERTS_LOG_PATH", "data/alerts/structure_alerts.jsonl"
)


def structure_alerts_log_path() -> str:
    """Read the audit-log path at call time.

    Mirrors :func:`structure_engine.logging._log_path` (L-2 fix on the
    Phase 11 logger). The module-level :data:`STRUCTURE_ALERTS_LOG_PATH`
    captures the env-or-default value at import; this function returns
    the current env value if set, else falls back to the import-time
    capture. Lets an operator override the path on a running process
    without restarting (rarely useful, but keeps the two persistence
    layers in sync).
    """
    return os.getenv("STRUCTURE_ALERTS_LOG_PATH", STRUCTURE_ALERTS_LOG_PATH)


def quantise_price(pair: str, price: float) -> int:
    """Quantise ``price`` to an integer pip count for dedupe-key use.

    GBPUSD has a pip size of 0.0001 — ``quantise_price("GBPUSD", 1.33405)``
    returns ``13340``. USDJPY has a pip size of 0.01 —
    ``quantise_price("USDJPY", 152.345)`` returns ``15234``. Unknown
    pairs fall through to the four-decimal default (per
    :func:`config.pair_config.pip_size_for`).

    Rounding is banker's rounding (Python's ``round``); the tie case
    is rare in real prices and consistency across runs matters more
    than the choice between half-up and half-even.
    """
    return int(round(price / pip_size_for(pair)))


__all__ = [
    "COOLDOWN_BY_SEVERITY",
    "CRITICAL_COOLDOWN_SEC",
    "DEDUPE_MAX_AGE_SEC",
    "INFO_COOLDOWN_SEC",
    "STRUCTURE_ALERTS_LOG_PATH",
    "WARNING_COOLDOWN_SEC",
    "quantise_price",
    "structure_alerts_log_path",
]
