"""Pure H1 regime classification.

The classifier is a *pure function* of one H1 row plus the previous H1 row.
It does no state-keeping; flapping prevention (hysteresis) is the
``RegimeEngine``'s job. The classifier uses entry thresholds only — once a
regime is entered, the engine applies the asymmetric exit thresholds.

Hierarchical priority (the spec's contract): **Structure > EMA slope > BB
width > MACD**. Each level can either pin the candidate label, override an
upstream candidate (via a conflict), or simply refine the confidence tier.

Inputs required on the H1 row (column names produced by the indicators and
structure modules):

- ``ema_slope_norm_50_10`` — ATR-normalised slope of EMA50 over 10 bars
- ``bb_width_norm_20_2`` — ATR-normalised Bollinger Band width
- ``macd_hist_12_26_9`` — MACD histogram
- ``structural_pattern`` — compound HH/HL/LH/LL label
  (one of ``"HH+HL"``, ``"HH+LL"``, ``"LH+HL"``, ``"LH+LL"``,
  ``"INSUFFICIENT_DATA"``); see :func:`add_structural_pattern_column`

If indicator data is NaN (rows before warmup), the classifier returns
``TRANSITION`` with reason ``"insufficient_indicator_data"`` — callers must
not act on this state.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from .labels import Confidence, Direction, RegimeLabel

# --- Tunables (spec values; documented in module docstring) ------------------

# Slope thresholds (ATR-normalised EMA slope over 10 bars).
SLOPE_TREND_ENTRY = 0.35  # |slope| > this → enter TREND from undecided
SLOPE_FLAT_BAND = 0.15  # |slope| < this → RANGE candidate (and TREND exit)

# Bollinger-width thresholds (ATR-normalised BB width).
BB_WIDTH_RANGE_ENTRY = 1.8  # < this → compressed (RANGE OK)
BB_WIDTH_RANGE_EXIT = 2.5  # > this → expanded (RANGE downgraded)

# Volatility-expansion check (relative bar-on-bar growth in BB width).
BB_WIDTH_EXPANSION_RATIO = 1.20  # 20% growth bar-on-bar triggers VOLATILE

# Structural-pattern lookback for the compound HH/HL/LH/LL label.
DEFAULT_STRUCTURE_LOOKBACK_BARS = 10


# --- Compound structural pattern --------------------------------------------


def compute_structural_pattern(
    high_positions: np.ndarray,
    low_positions: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    current_index: int,
    lookback_bars: int = DEFAULT_STRUCTURE_LOOKBACK_BARS,
) -> str:
    """Return the compound structural label at ``current_index``.

    Considers swing-high and swing-low positions that fall within
    ``[current_index - lookback_bars + 1, current_index]`` (inclusive).
    Requires at least two swing highs **and** two swing lows in that window
    — otherwise returns ``"INSUFFICIENT_DATA"``.

    Returns one of:
        ``"HH+HL"``  ``"HH+LL"``  ``"LH+HL"``  ``"LH+LL"``  ``"INSUFFICIENT_DATA"``
    """
    cutoff = current_index - lookback_bars + 1
    rel_highs = high_positions[
        (high_positions <= current_index) & (high_positions >= cutoff)
    ]
    rel_lows = low_positions[
        (low_positions <= current_index) & (low_positions >= cutoff)
    ]
    if rel_highs.size < 2 or rel_lows.size < 2:
        return "INSUFFICIENT_DATA"
    high_label = "HH" if highs[rel_highs[-1]] > highs[rel_highs[-2]] else "LH"
    low_label = "HL" if lows[rel_lows[-1]] > lows[rel_lows[-2]] else "LL"
    return f"{high_label}+{low_label}"


def add_structural_pattern_column(
    df: pd.DataFrame, lookback_bars: int = DEFAULT_STRUCTURE_LOOKBACK_BARS
) -> pd.DataFrame:
    """Append ``structural_pattern`` to a copy of an H1 DataFrame.

    The input must already carry ``swing_high``, ``swing_low``, ``high``,
    and ``low`` columns (typically from ``add_fractal_swings``).
    """
    required = ("swing_high", "swing_low", "high", "low")
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(
            f"add_structural_pattern_column: input DataFrame missing column(s) "
            f"{missing}. Run add_fractal_swings first."
        )

    out = df.copy()
    high_positions = np.flatnonzero(df["swing_high"].to_numpy())
    low_positions = np.flatnonzero(df["swing_low"].to_numpy())
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    n = len(df)
    patterns = [
        compute_structural_pattern(
            high_positions, low_positions, highs, lows, i, lookback_bars
        )
        for i in range(n)
    ]
    out["structural_pattern"] = patterns
    return out


# --- The classifier itself ---------------------------------------------------


def _row_get(row: pd.Series, key: str, default: float = float("nan")) -> float:
    try:
        v = row[key]
    except KeyError:
        return default
    if v is None:
        return default
    return float(v)


def classify_h1(
    h1_row: pd.Series, prev_h1_row: Optional[pd.Series] = None
) -> tuple[RegimeLabel, Optional[Direction], Confidence, str]:
    """Classify a single H1 close (pure function).

    Parameters
    ----------
    h1_row : pd.Series
        An H1 row that already has indicator and structural columns set.
        See module docstring for the required column names.
    prev_h1_row : pd.Series, optional
        The previous H1 row, used only to detect bar-on-bar BB-width
        expansion (the volatility trigger). If ``None`` or its
        ``bb_width_norm`` is NaN, the expansion check is skipped.

    Returns
    -------
    (RegimeLabel, Optional[Direction], Confidence, str)
        ``Direction`` is ``None`` for direction-less regimes. The trailing
        ``str`` is a reason code suitable for logging / debug.
    """
    structural_pattern = str(
        h1_row.get("structural_pattern", "INSUFFICIENT_DATA")
    )
    slope = _row_get(h1_row, "ema_slope_norm_50_10")
    bb_width = _row_get(h1_row, "bb_width_norm_20_2")
    macd_hist = _row_get(h1_row, "macd_hist_12_26_9")
    prev_bb_width = (
        _row_get(prev_h1_row, "bb_width_norm_20_2")
        if prev_h1_row is not None
        else float("nan")
    )

    # Guard: indicators not yet warmed up.
    if math.isnan(slope) or math.isnan(bb_width):
        return (
            RegimeLabel.TRANSITION,
            None,
            Confidence.LOW,
            "insufficient_indicator_data",
        )

    # Priority 1: Structure.
    candidate_label: Optional[RegimeLabel]
    candidate_dir: Optional[Direction]
    if structural_pattern == "HH+HL":
        candidate_label, candidate_dir = RegimeLabel.TREND, Direction.BULLISH
    elif structural_pattern == "LH+LL":
        candidate_label, candidate_dir = RegimeLabel.TREND, Direction.BEARISH
    elif structural_pattern in ("HH+LL", "LH+HL"):
        return (
            RegimeLabel.VOLATILE,
            None,
            Confidence.LOW,
            "structure_conflict",
        )
    else:
        candidate_label, candidate_dir = None, None

    # Priority 2: EMA slope.
    if candidate_label is None:
        if slope > SLOPE_TREND_ENTRY:
            candidate_label, candidate_dir = RegimeLabel.TREND, Direction.BULLISH
        elif slope < -SLOPE_TREND_ENTRY:
            candidate_label, candidate_dir = RegimeLabel.TREND, Direction.BEARISH
        elif -SLOPE_FLAT_BAND < slope < SLOPE_FLAT_BAND:
            candidate_label, candidate_dir = RegimeLabel.RANGE, None
        else:
            return (
                RegimeLabel.TRANSITION,
                None,
                Confidence.LOW,
                "slope_transitional",
            )
    else:
        # Structure said TREND. Verify slope sign agrees.
        if (candidate_dir == Direction.BULLISH and slope < 0) or (
            candidate_dir == Direction.BEARISH and slope > 0
        ):
            return (
                RegimeLabel.VOLATILE,
                None,
                Confidence.LOW,
                "structure_slope_conflict",
            )

    # Priority 3: BB width.
    if candidate_label == RegimeLabel.TREND and bb_width < BB_WIDTH_RANGE_ENTRY:
        return (
            RegimeLabel.TRANSITION,
            candidate_dir,
            Confidence.LOW,
            "trend_no_expansion",
        )
    if (
        candidate_label == RegimeLabel.RANGE
        and bb_width > BB_WIDTH_RANGE_EXIT
    ):
        return (
            RegimeLabel.VOLATILE,
            None,
            Confidence.LOW,
            "range_with_expansion",
        )

    # Volatility-expansion check: bar-on-bar BB-width growth.
    if (
        not math.isnan(prev_bb_width)
        and prev_bb_width > 0
        and bb_width > prev_bb_width * BB_WIDTH_EXPANSION_RATIO
    ):
        return (
            RegimeLabel.VOLATILE,
            candidate_dir if candidate_label == RegimeLabel.TREND else None,
            Confidence.LOW,
            "volatility_expansion",
        )

    # Priority 4: MACD — affects confidence tier only.
    # N3 design choice: MACD is the lowest-priority signal (confidence-only
    # in the spec hierarchy), so a NaN macd_hist does NOT block TREND
    # classification. It does, however, surface a distinct reason code
    # ("classified_no_macd") so downstream diagnostics can distinguish
    # "MACD evaluated and disagreed" (reason="classified", confidence=
    # MEDIUM) from "MACD unavailable" (reason="classified_no_macd",
    # confidence=MEDIUM). The confidence tier sits at MEDIUM in both cases
    # because HIGH requires evaluable MACD agreement.
    confidence = Confidence.MEDIUM
    reason = "classified"
    if candidate_label == RegimeLabel.TREND:
        if math.isnan(macd_hist):
            reason = "classified_no_macd"
        else:
            macd_agrees = (
                (candidate_dir == Direction.BULLISH and macd_hist > 0)
                or (candidate_dir == Direction.BEARISH and macd_hist < 0)
            )
            confidence = Confidence.HIGH if macd_agrees else Confidence.MEDIUM

    return (candidate_label, candidate_dir, confidence, reason)
