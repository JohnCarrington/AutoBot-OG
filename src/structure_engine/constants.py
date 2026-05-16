"""Structure Engine tunables (Phase 11).

All values are env-overridable for ops/backtesting parity. Defaults
mirror the Structure Engine spec (sections referenced inline).
Naming convention: ``STRUCTURE_*`` env-var prefix; bare identifier in
the module — same shape as ``strategies/constants.py``.
"""
from __future__ import annotations

import os


def _f(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw is not None else default


def _i(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw is not None else default


# ---------------------------------------------------------------------------
# Minimum candle counts (spec §2).
# ---------------------------------------------------------------------------
MIN_CANDLES_M5: int = _i("STRUCTURE_MIN_CANDLES_M5", 50)
MIN_CANDLES_M15: int = _i("STRUCTURE_MIN_CANDLES_M15", 50)
MIN_CANDLES_H1: int = _i("STRUCTURE_MIN_CANDLES_H1", 50)


# ---------------------------------------------------------------------------
# Swing-detection windows (spec §5).
#
# ``N`` candles left/right that must be strictly lower (HIGH) / higher (LOW).
# ---------------------------------------------------------------------------
SWING_WINDOW_H1: int = _i("STRUCTURE_SWING_WINDOW_H1", 2)
SWING_WINDOW_M15: int = _i("STRUCTURE_SWING_WINDOW_M15", 2)
SWING_WINDOW_M5: int = _i("STRUCTURE_SWING_WINDOW_M5", 3)


# ---------------------------------------------------------------------------
# Zone-width defaults (spec §6).
#
# Per-pair minimum zone half-width in pips. Final half-width =
# ``max(min_pips_in_price, atr_m5 * ZONE_ATR_FRACTION)``.
# ---------------------------------------------------------------------------
PAIR_MIN_ZONE_PIPS: dict[str, float] = {
    "GBPUSD": _f("STRUCTURE_GBPUSD_MIN_ZONE_PIPS", 4.0),
    "EURUSD": _f("STRUCTURE_EURUSD_MIN_ZONE_PIPS", 3.0),
    "EURGBP": _f("STRUCTURE_EURGBP_MIN_ZONE_PIPS", 3.0),
    "AUDUSD": _f("STRUCTURE_AUDUSD_MIN_ZONE_PIPS", 3.0),
    "USDCAD": _f("STRUCTURE_USDCAD_MIN_ZONE_PIPS", 4.0),
    "USDJPY": _f("STRUCTURE_USDJPY_MIN_ZONE_PIPS", 4.0),
    "GBPJPY": _f("STRUCTURE_GBPJPY_MIN_ZONE_PIPS", 4.0),
}
DEFAULT_MIN_ZONE_PIPS: float = _f("STRUCTURE_DEFAULT_MIN_ZONE_PIPS", 4.0)

ZONE_ATR_FRACTION: float = _f("STRUCTURE_ZONE_ATR_FRACTION", 0.25)

# Two zones merge if either overlaps OR sits within ``ZONE_MERGE_MULT`` *
# (zone_a_half_width + zone_b_half_width) of each other.
ZONE_MERGE_MULT: float = _f("STRUCTURE_ZONE_MERGE_MULT", 1.0)


# ---------------------------------------------------------------------------
# Scoring weights (spec §8).
# ---------------------------------------------------------------------------
TIMEFRAME_SCORE: dict[str, float] = {
    "H1": _f("STRUCTURE_TF_SCORE_H1", 3.0),
    "M15": _f("STRUCTURE_TF_SCORE_M15", 2.0),
    "M5": _f("STRUCTURE_TF_SCORE_M5", 1.0),
}

TOUCH_SCORE_1: float = _f("STRUCTURE_TOUCH_SCORE_1", 0.5)
TOUCH_SCORE_2: float = _f("STRUCTURE_TOUCH_SCORE_2", 1.0)
TOUCH_SCORE_3PLUS: float = _f("STRUCTURE_TOUCH_SCORE_3PLUS", 2.0)

REACTION_SCORE_WEAK: float = _f("STRUCTURE_REACTION_SCORE_WEAK", 0.5)
REACTION_SCORE_MEDIUM: float = _f("STRUCTURE_REACTION_SCORE_MEDIUM", 1.0)
REACTION_SCORE_STRONG: float = _f("STRUCTURE_REACTION_SCORE_STRONG", 2.0)
# Reaction strength is measured in ATR multiples: bar displacement
# after the touch divided by ATR.
REACTION_STRONG_ATR_MULT: float = _f("STRUCTURE_REACTION_STRONG_ATR_MULT", 1.5)
REACTION_MEDIUM_ATR_MULT: float = _f("STRUCTURE_REACTION_MEDIUM_ATR_MULT", 0.75)

RECENCY_BARS_RECENT: int = _i("STRUCTURE_RECENCY_BARS_RECENT", 10)
RECENCY_BARS_MEDIUM: int = _i("STRUCTURE_RECENCY_BARS_MEDIUM", 30)
RECENCY_SCORE_RECENT: float = _f("STRUCTURE_RECENCY_SCORE_RECENT", 1.5)
RECENCY_SCORE_MEDIUM: float = _f("STRUCTURE_RECENCY_SCORE_MEDIUM", 1.0)
RECENCY_SCORE_OLD: float = _f("STRUCTURE_RECENCY_SCORE_OLD", 0.5)

LIQUIDITY_SCORE_EQUAL_HL: float = _f("STRUCTURE_LIQUIDITY_SCORE_EQUAL_HL", 1.5)
LIQUIDITY_SCORE_STOP_POOL: float = _f("STRUCTURE_LIQUIDITY_SCORE_STOP_POOL", 1.0)

SESSION_SCORE_PREV_DAY: float = _f("STRUCTURE_SESSION_SCORE_PREV_DAY", 1.5)
SESSION_SCORE_LONDON: float = _f("STRUCTURE_SESSION_SCORE_LONDON", 1.0)
SESSION_SCORE_ASIA: float = _f("STRUCTURE_SESSION_SCORE_ASIA", 0.75)
SESSION_SCORE_NY: float = _f("STRUCTURE_SESSION_SCORE_NY", 1.0)

INVALIDATION_PENALTY: float = _f("STRUCTURE_INVALIDATION_PENALTY", 2.0)

SCORE_CAP: float = _f("STRUCTURE_SCORE_CAP", 10.0)

# Strategies gate on score >= STRONG_LEVEL_THRESHOLD (spec §8 interpretation).
STRONG_LEVEL_THRESHOLD: float = _f("STRUCTURE_STRONG_LEVEL_THRESHOLD", 6.0)


# ---------------------------------------------------------------------------
# Equal-high / equal-low detection.
#
# Two swing extremes count as "equal" if they sit within a zone-tolerance
# of each other. The tolerance reuses the same ATR-fraction logic as the
# zone builder.
# ---------------------------------------------------------------------------
EQUAL_HL_MIN_COUNT: int = _i("STRUCTURE_EQUAL_HL_MIN_COUNT", 2)


# ---------------------------------------------------------------------------
# Bias detection (spec §9).
#
# EMA degradation order: ema_200 → ema_100 → ema_50. The bias detector
# walks this list and picks the longest EMA whose value is not NaN.
# ---------------------------------------------------------------------------
BIAS_EMA_PRIORITY: tuple[str, ...] = ("ema_200", "ema_100", "ema_50")

# MACD-histogram sign agreement with EMA stack and HH/HL structure. The
# threshold is a small dead-zone so a near-zero histogram doesn't flip
# bias.
BIAS_MACD_DEAD_ZONE: float = _f("STRUCTURE_BIAS_MACD_DEAD_ZONE", 1e-6)

# Number of swing points to inspect when judging HH/HL vs LH/LL.
BIAS_SWING_LOOKBACK: int = _i("STRUCTURE_BIAS_SWING_LOOKBACK", 4)


# ---------------------------------------------------------------------------
# Reaction detection (spec §10).
#
# Strict 3-bar lookback. ``REACTION_LOOKBACK_BARS`` is the index window
# (N-2, N-1, N). The reaction detector consumes the last
# ``REACTION_LOOKBACK_BARS`` bars of the M5 DataFrame.
# ---------------------------------------------------------------------------
REACTION_LOOKBACK_BARS: int = 3

# Acceptance break requires this many consecutive closes beyond the zone.
ACCEPTANCE_MIN_CLOSES: int = _i("STRUCTURE_ACCEPTANCE_MIN_CLOSES", 2)


# ---------------------------------------------------------------------------
# Mode classification (spec §11).
# ---------------------------------------------------------------------------
# Range-balance: BB width must be compressed and EMA cluster tight.
MODE_RANGE_BB_WIDTH_MAX: float = _f("STRUCTURE_MODE_RANGE_BB_WIDTH_MAX", 1.5)
# Trend-continuation: directional slope on EMA50 (normalised).
MODE_TREND_SLOPE_MIN: float = _f("STRUCTURE_MODE_TREND_SLOPE_MIN", 0.35)
# Volatile-sweep: elevated ATR relative to its medium-window median.
MODE_VOLATILE_ATR_MULT: float = _f("STRUCTURE_MODE_VOLATILE_ATR_MULT", 1.5)
MODE_VOLATILE_ATR_LOOKBACK: int = _i("STRUCTURE_MODE_VOLATILE_ATR_LOOKBACK", 50)


# ---------------------------------------------------------------------------
# Liquidity detection (spec §12).
# ---------------------------------------------------------------------------
LIQUIDITY_LOOKBACK_BARS_M15: int = _i(
    "STRUCTURE_LIQUIDITY_LOOKBACK_BARS_M15", 50
)
LIQUIDITY_LOOKBACK_BARS_H1: int = _i(
    "STRUCTURE_LIQUIDITY_LOOKBACK_BARS_H1", 50
)


# ---------------------------------------------------------------------------
# Logging (spec §15).
# ---------------------------------------------------------------------------
STRUCTURE_LOG_ENABLED: bool = (
    os.getenv("STRUCTURE_LOG_ENABLED", "0").lower() in ("1", "true", "yes")
)
STRUCTURE_LOG_PATH: str = os.getenv(
    "STRUCTURE_LOG_PATH", "data/structure/structure_state.jsonl"
)


__all__ = [
    "ACCEPTANCE_MIN_CLOSES",
    "BIAS_EMA_PRIORITY",
    "BIAS_MACD_DEAD_ZONE",
    "BIAS_SWING_LOOKBACK",
    "DEFAULT_MIN_ZONE_PIPS",
    "EQUAL_HL_MIN_COUNT",
    "INVALIDATION_PENALTY",
    "LIQUIDITY_LOOKBACK_BARS_H1",
    "LIQUIDITY_LOOKBACK_BARS_M15",
    "LIQUIDITY_SCORE_EQUAL_HL",
    "LIQUIDITY_SCORE_STOP_POOL",
    "MIN_CANDLES_H1",
    "MIN_CANDLES_M15",
    "MIN_CANDLES_M5",
    "MODE_RANGE_BB_WIDTH_MAX",
    "MODE_TREND_SLOPE_MIN",
    "MODE_VOLATILE_ATR_LOOKBACK",
    "MODE_VOLATILE_ATR_MULT",
    "PAIR_MIN_ZONE_PIPS",
    "REACTION_LOOKBACK_BARS",
    "REACTION_MEDIUM_ATR_MULT",
    "REACTION_SCORE_MEDIUM",
    "REACTION_SCORE_STRONG",
    "REACTION_SCORE_WEAK",
    "REACTION_STRONG_ATR_MULT",
    "RECENCY_BARS_MEDIUM",
    "RECENCY_BARS_RECENT",
    "RECENCY_SCORE_MEDIUM",
    "RECENCY_SCORE_OLD",
    "RECENCY_SCORE_RECENT",
    "SCORE_CAP",
    "SESSION_SCORE_ASIA",
    "SESSION_SCORE_LONDON",
    "SESSION_SCORE_NY",
    "SESSION_SCORE_PREV_DAY",
    "STRONG_LEVEL_THRESHOLD",
    "STRUCTURE_LOG_ENABLED",
    "STRUCTURE_LOG_PATH",
    "SWING_WINDOW_H1",
    "SWING_WINDOW_M15",
    "SWING_WINDOW_M5",
    "TIMEFRAME_SCORE",
    "TOUCH_SCORE_1",
    "TOUCH_SCORE_2",
    "TOUCH_SCORE_3PLUS",
    "ZONE_ATR_FRACTION",
    "ZONE_MERGE_MULT",
]
