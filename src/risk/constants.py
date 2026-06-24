"""Risk-layer thresholds and tunables.

All values default to the locked v1 spec (``docs/v1_architecture.md`` §6)
and can be overridden via environment variables for ops emergencies
without a code change. The pattern mirrors ``config/pair_config.py``'s
``MIN_SL_PIPS`` env-override scheme.

Phase 4 deliberately ships these as Python constants rather than a YAML
config. Rationale (see ``docs/v1_architecture.md`` §6.10):
- Single source of truth, no parser to debug.
- Refactor-safe and type-checked at import.
- Env overrides cover the "ops needs to bump a threshold without a
  deploy" case until a real ops layer exists in v2.
"""
from __future__ import annotations

import os


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# --- Spread filter (§6.7) ---------------------------------------------------
# Reject when ``current_spread_pips > min(ABS_CAP, ATR_MULT * atr_m5_pips)``.
SPREAD_ABS_CAP_PIPS: float = _env_float("RISK_SPREAD_ABS_CAP_PIPS", 3.0)
SPREAD_ATR_MULT: float = _env_float("RISK_SPREAD_ATR_MULT", 0.3)

# --- Position caps (§6.8) ---------------------------------------------------
MAX_GLOBAL_POSITIONS: int = _env_int("RISK_MAX_GLOBAL_POSITIONS", 2)
MAX_PER_PAIR: int = _env_int("RISK_MAX_PER_PAIR", 1)
# 2c (B-4): per-regime cap replaced with per-strategy cap. No two open
# positions from the same strategy. Not env-overridable in v1.
MAX_PER_STRATEGY: int = 1

# --- Daily drawdown stop (§6.9.1) -------------------------------------------
DAILY_DD_LIMIT_R: float = _env_float("RISK_DAILY_DD_LIMIT_R", -3.0)

# --- Consecutive-loss cooldown (§6.9.2) -------------------------------------
CONSECUTIVE_LOSS_THRESHOLD: int = _env_int("RISK_LOSS_THRESHOLD", 4)
CONSECUTIVE_LOSS_COOLDOWN_HOURS: float = _env_float(
    "RISK_LOSS_COOLDOWN_HOURS", 4.0
)

# 2c (B-2): the regime-instability circuit breaker is gone. Its
# thresholds / pause-hours constants were removed alongside the breaker
# code in risk.rules.circuit_breakers.

# --- End-of-day (§6.5) ------------------------------------------------------
# NY close hour in America/New_York time. Converted to UTC via zoneinfo at
# decision time so DST flips correctly (currently 21:00 UTC in EDT, 22:00 UTC
# in EST).
NY_CLOSE_HOUR_LOCAL: int = _env_int("RISK_NY_CLOSE_HOUR", 17)
NY_TZ_NAME: str = os.getenv("RISK_NY_TZ", "America/New_York")
# Minutes-before-EOD that allow_entry should reject new entries to avoid
# opening a fresh position with minutes to live.
PRE_EOD_NO_ENTRY_MIN: int = _env_int("RISK_PRE_EOD_NO_ENTRY_MIN", 30)

# --- Overnight hold (§6.5) --------------------------------------------------
# Minimum unrealised PnL (in R-multiples) for a position to be eligible for
# overnight hold. 2c (B-1) keeps the +1R floor but the hold decision now
# keys on structure htf_bias alignment, not the position's regime label.
OVERNIGHT_HOLD_MIN_R: float = _env_float(
    "RISK_OVERNIGHT_HOLD_MIN_R", 1.0
)

__all__ = [
    "CONSECUTIVE_LOSS_COOLDOWN_HOURS",
    "CONSECUTIVE_LOSS_THRESHOLD",
    "DAILY_DD_LIMIT_R",
    "MAX_GLOBAL_POSITIONS",
    "MAX_PER_PAIR",
    "MAX_PER_STRATEGY",
    "NY_CLOSE_HOUR_LOCAL",
    "NY_TZ_NAME",
    "OVERNIGHT_HOLD_MIN_R",
    "PRE_EOD_NO_ENTRY_MIN",
    "SPREAD_ABS_CAP_PIPS",
    "SPREAD_ATR_MULT",
]
