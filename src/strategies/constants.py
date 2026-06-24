"""Strategy-layer tunables (Phase 5).

Every value is read once at import time from an environment variable so
ops can adjust without code changes; defaults match
``docs/v1_architecture.md`` §5. The naming convention mirrors
``src/risk/constants.py`` (``STRATEGY_*`` prefix on the env var, bare
identifier in the module) and ``config/pair_config.py`` (per-pair floors).

Categories:

- **ATR multipliers** — how many ATRs the strategy expects to give up
  before its invalidation. The actual SL distance is
  ``max(MIN_SL_PIPS[pair], multiplier × ATR_M5_pips)``.
- **Confidence bands** — the HIGH-vs-LOW split each strategy uses when
  MACD-H1 agrees (or doesn't). v1 is a binary split; v2 may introduce
  finer tiers.
- **EMA pullback close tolerance** — how far the close may sit below
  EMA50 (in pips) on a bullish-TREND pullback bar. Spec §5.2 reads
  "pullback either touches the EMA50 or closes one bar past it on the
  wrong side"; the close-past case is gated by this tolerance so the
  pullback bar can still wick-penetrate and close *slightly* past
  without disqualifying the setup. Mirror for bearish TREND.
- **Sweep swing age** — maximum age (in M5 bars) of the structural
  swing whose liquidity the sweep candle is fading. Older swings are
  considered stale.
- **Sweep strength threshold** — how far beyond the swept level the
  sweep wick must extend (in ATR units) to earn the high-confidence
  rating.
"""
from __future__ import annotations

import os


def _f(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw is not None else default


def _i(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw is not None else default


# --- ATR multipliers --------------------------------------------------------
BB_RECLAIM_ATR_MULT: float = _f("STRATEGY_BB_RECLAIM_ATR_MULT", 0.8)
EMA_CONT_ATR_MULT: float = _f("STRATEGY_EMA_CONT_ATR_MULT", 1.2)
LIQ_SWEEP_ATR_MULT: float = _f("STRATEGY_LIQ_SWEEP_ATR_MULT", 1.0)
# Structure-break continuation: same SL philosophy as EMA continuation —
# the SL sits past the broken-and-accepted level by `mult × ATR_M5` pips
# (floor: per-pair MIN_SL_PIPS). Tighter than EMA_CONT by default because
# the level was *just* broken — the accepted close is the proof; a clean
# re-claim of the zone invalidates the thesis quickly.
STRUCT_BREAK_ATR_MULT: float = _f("STRATEGY_STRUCT_BREAK_ATR_MULT", 1.0)


# --- Confidence bands -------------------------------------------------------
BB_RECLAIM_CONF_HIGH: float = _f("STRATEGY_BB_RECLAIM_CONF_HIGH", 0.85)
BB_RECLAIM_CONF_LOW: float = _f("STRATEGY_BB_RECLAIM_CONF_LOW", 0.65)
EMA_CONT_CONF_HIGH: float = _f("STRATEGY_EMA_CONT_CONF_HIGH", 0.80)
EMA_CONT_CONF_LOW: float = _f("STRATEGY_EMA_CONT_CONF_LOW", 0.60)
LIQ_SWEEP_CONF_HIGH: float = _f("STRATEGY_LIQ_SWEEP_CONF_HIGH", 0.75)
LIQ_SWEEP_CONF_LOW: float = _f("STRATEGY_LIQ_SWEEP_CONF_LOW", 0.55)
STRUCT_BREAK_CONF_HIGH: float = _f("STRATEGY_STRUCT_BREAK_CONF_HIGH", 0.80)
STRUCT_BREAK_CONF_LOW: float = _f("STRATEGY_STRUCT_BREAK_CONF_LOW", 0.60)


# --- EMA Continuation pullback tolerance ------------------------------------
# How many pips the pullback bar's *close* may sit on the wrong side of
# EMA50 and still qualify (the bar must also wick-penetrate the EMA).
EMA_PULLBACK_CLOSE_TOLERANCE_PIPS: float = _f(
    "STRATEGY_EMA_PULLBACK_CLOSE_TOLERANCE_PIPS", 5.0
)


# --- Liquidity Sweep gates --------------------------------------------------
SWEEP_SWING_MAX_AGE_BARS: int = _i("STRATEGY_SWEEP_SWING_MAX_AGE_BARS", 24)
# How far the sweep wick must extend beyond the swept level, measured
# in ATR units, to earn the high-confidence rating.
LIQ_SWEEP_STRONG_ATR_FRACTION: float = _f(
    "STRATEGY_LIQ_SWEEP_STRONG_ATR_FRACTION", 0.5
)


# --- M5 bar cadence ---------------------------------------------------------
# Used by Signal.invalid_after_candle_ts to compute the next M5 close.
M5_BAR_MINUTES: int = _i("STRATEGY_M5_BAR_MINUTES", 5)


__all__ = [
    "BB_RECLAIM_ATR_MULT",
    "BB_RECLAIM_CONF_HIGH",
    "BB_RECLAIM_CONF_LOW",
    "EMA_CONT_ATR_MULT",
    "EMA_CONT_CONF_HIGH",
    "EMA_CONT_CONF_LOW",
    "EMA_PULLBACK_CLOSE_TOLERANCE_PIPS",
    "LIQ_SWEEP_ATR_MULT",
    "LIQ_SWEEP_CONF_HIGH",
    "LIQ_SWEEP_CONF_LOW",
    "LIQ_SWEEP_STRONG_ATR_FRACTION",
    "M5_BAR_MINUTES",
    "STRUCT_BREAK_ATR_MULT",
    "STRUCT_BREAK_CONF_HIGH",
    "STRUCT_BREAK_CONF_LOW",
    "SWEEP_SWING_MAX_AGE_BARS",
]
