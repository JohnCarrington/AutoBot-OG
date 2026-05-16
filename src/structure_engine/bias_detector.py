"""HTF / local bias detection per spec §9.

Bias is computed from EMAs, MACD-histogram sign, and HH/HL swing
structure. The interesting part is **EMA warm-up degradation**
(refinement A): until ~200 H1 bars accumulate, ``ema_200`` is NaN,
so the detector falls back to ``ema_100`` and then ``ema_50``. The
spec's rule "price below EMA50/EMA100" is interpreted as "price below
the longest *available* EMA".

The function records which EMA was used in
``StructureState.debug.htf_ema_used`` so operators can correlate weak
bias signals with warm-up state in the first ~17 hours of live operation.
"""
from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from .constants import BIAS_EMA_PRIORITY, BIAS_MACD_DEAD_ZONE, BIAS_SWING_LOOKBACK
from .swing_detector import detect_swings
from .types import Direction, Swing, Timeframe


def detect_htf_bias(df_h1: pd.DataFrame) -> tuple[Direction, dict]:
    """Return ``(htf_bias, debug)`` derived from H1.

    The debug dict carries the EMA the detector landed on, the close
    used, and the structural signal flavour ("HH_HL" / "LH_LL" /
    "MIXED"). Strategies don't read the dict; it is included so logs
    and the adversarial review can explain why a bias call landed
    where it did.
    """
    if df_h1 is None or df_h1.empty:
        return "NEUTRAL", {"htf_ema_used": None, "reason": "no_h1_data"}

    latest = df_h1.iloc[-1]
    ema_used, ema_value = _select_ema(latest)
    debug: dict = {"htf_ema_used": ema_used}

    if ema_used is None or ema_value is None:
        debug["reason"] = "insufficient_ema_data"
        return "NEUTRAL", debug

    close = _safe_float(latest.get("close"))
    if math.isnan(close):
        debug["reason"] = "missing_close"
        return "NEUTRAL", debug
    debug["close"] = close
    debug["ema_value"] = ema_value

    # 1. Long-EMA position
    price_signal: Direction
    if close > ema_value:
        price_signal = "BULLISH"
    elif close < ema_value:
        price_signal = "BEARISH"
    else:
        price_signal = "NEUTRAL"

    # 2. EMA-stack ordering (8 < 13 < 21 for bearish, > for bullish).
    stack_signal = _ema_stack_signal(latest)
    debug["ema_stack"] = stack_signal

    # 3. MACD-histogram dead-zone check.
    macd_signal = _macd_signal(latest)
    debug["macd_signal"] = macd_signal

    # 4. Structural pattern (HH/HL vs LH/LL) over recent H1 swings.
    structural_signal = _structural_signal(df_h1)
    debug["structural_signal"] = structural_signal

    bias = _combine_signals(
        price_signal, stack_signal, macd_signal, structural_signal
    )
    debug["reason"] = "combined"
    return bias, debug


def detect_local_bias(
    df_m5: pd.DataFrame, df_m15: pd.DataFrame | None
) -> tuple[Direction, dict]:
    """Return ``(local_bias, debug)`` derived from M5/M15.

    M15 takes precedence when available — M5 is noise-prone at small
    samples. M15 may be empty during warm-up (Phase 8 derives it from
    M5 by resample; the first M5 bars produce no M15 yet); in that
    case we fall back to M5 EMA-stack alone.
    """
    df = df_m15 if df_m15 is not None and not df_m15.empty else df_m5
    if df is None or df.empty:
        return "NEUTRAL", {"local_source": None, "reason": "no_lower_tf_data"}

    latest = df.iloc[-1]
    debug: dict = {"local_source": "M15" if df is df_m15 else "M5"}

    ema_used, ema_value = _select_ema(latest)
    if ema_used is None or ema_value is None:
        debug["reason"] = "insufficient_ema_data"
        return "NEUTRAL", debug
    debug["local_ema_used"] = ema_used

    close = _safe_float(latest.get("close"))
    if math.isnan(close):
        debug["reason"] = "missing_close"
        return "NEUTRAL", debug

    price_signal: Direction
    if close > ema_value:
        price_signal = "BULLISH"
    elif close < ema_value:
        price_signal = "BEARISH"
    else:
        price_signal = "NEUTRAL"

    stack_signal = _ema_stack_signal(latest)
    if stack_signal != "NEUTRAL" and stack_signal != price_signal:
        debug["reason"] = "stack_disagree"
        return "NEUTRAL", debug

    debug["reason"] = "price_vs_ema"
    return price_signal, debug


def _select_ema(row) -> tuple[Optional[str], Optional[float]]:
    """Walk ``BIAS_EMA_PRIORITY`` and return the first non-NaN EMA.

    Refinement A: graceful degradation through EMA200 → EMA100 → EMA50.
    """
    for col in BIAS_EMA_PRIORITY:
        value = _safe_float(row.get(col)) if hasattr(row, "get") else float("nan")
        if not math.isnan(value):
            return col.upper().replace("EMA_", "EMA"), value
    return None, None


def _ema_stack_signal(row) -> Direction:
    """Return ``BULLISH`` if EMA8 > EMA13 > EMA21, ``BEARISH`` if mirrored.

    A missing EMA in the trio short-circuits to NEUTRAL — the warm-up
    chain falls through to the price-vs-longer-EMA signal instead.
    """
    ema8 = _safe_float(row.get("ema_8")) if hasattr(row, "get") else float("nan")
    ema13 = _safe_float(row.get("ema_13")) if hasattr(row, "get") else float("nan")
    ema21 = _safe_float(row.get("ema_21")) if hasattr(row, "get") else float("nan")
    if any(math.isnan(v) for v in (ema8, ema13, ema21)):
        return "NEUTRAL"
    if ema8 > ema13 > ema21:
        return "BULLISH"
    if ema8 < ema13 < ema21:
        return "BEARISH"
    return "NEUTRAL"


def _macd_signal(row) -> Direction:
    hist = (
        _safe_float(row.get("macd_hist_12_26_9"))
        if hasattr(row, "get")
        else float("nan")
    )
    if math.isnan(hist):
        return "NEUTRAL"
    if hist > BIAS_MACD_DEAD_ZONE:
        return "BULLISH"
    if hist < -BIAS_MACD_DEAD_ZONE:
        return "BEARISH"
    return "NEUTRAL"


def _structural_signal(df_h1: pd.DataFrame) -> Direction:
    """HH/HL → BULLISH; LH/LL → BEARISH; otherwise NEUTRAL."""
    swings = detect_swings(df_h1, "H1")
    if len(swings) < BIAS_SWING_LOOKBACK:
        return "NEUTRAL"
    recent = swings[-BIAS_SWING_LOOKBACK:]
    highs = [s for s in recent if s.type == "HIGH"]
    lows = [s for s in recent if s.type == "LOW"]
    if len(highs) >= 2 and len(lows) >= 2:
        higher_highs = highs[-1].price > highs[-2].price
        higher_lows = lows[-1].price > lows[-2].price
        lower_highs = highs[-1].price < highs[-2].price
        lower_lows = lows[-1].price < lows[-2].price
        if higher_highs and higher_lows:
            return "BULLISH"
        if lower_highs and lower_lows:
            return "BEARISH"
    return "NEUTRAL"


def _combine_signals(
    price: Direction,
    stack: Direction,
    macd: Direction,
    structural: Direction,
) -> Direction:
    """Majority vote with price as the tie-breaker.

    NEUTRAL signals don't count toward the vote. A 1-to-1 disagreement
    (e.g. price BULLISH, structural BEARISH, others NEUTRAL) returns
    NEUTRAL rather than letting price win — the engine prefers to say
    "I don't know" over a low-confidence call.
    """
    votes = [s for s in (price, stack, macd, structural) if s != "NEUTRAL"]
    if not votes:
        return "NEUTRAL"
    bulls = votes.count("BULLISH")
    bears = votes.count("BEARISH")
    if bulls > bears:
        return "BULLISH"
    if bears > bulls:
        return "BEARISH"
    return "NEUTRAL"


def _safe_float(value) -> float:
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


__all__ = ["detect_htf_bias", "detect_local_bias"]
