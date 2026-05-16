"""Shared fixture builders for Structure Engine tests.

Two layers:

1. Low-level helpers (``make_ohlc``, ``warmup_rows``) that build OHLC
   DataFrames with all the columns the engine reads.
2. Per-reaction-type builders (``build_support_rejection`` and friends)
   that append a 3-bar reaction onto a warmup window.

All fixtures use 5-minute spacing and a tz-aware ``DatetimeIndex`` so
``isinstance(idx[-1], datetime)`` is true (the strategy session gate
relies on this).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

import pandas as pd
import pytest


_BASE_TS = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)


def make_index(n: int, *, start: datetime = _BASE_TS, step_minutes: int = 5) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(
        [start + timedelta(minutes=step_minutes * i) for i in range(n)]
    )


def make_ohlc(
    rows: list[dict],
    *,
    start: datetime = _BASE_TS,
    step_minutes: int = 5,
) -> pd.DataFrame:
    """Build an enriched OHLC DataFrame.

    ``rows`` is a list of dicts. Missing OHLC columns default to a
    flat candle around ``close``; missing indicator columns default
    to ``nan`` (engine treats them as "unavailable").
    """
    index = make_index(len(rows), start=start, step_minutes=step_minutes)
    normalised = [_normalise(r) for r in rows]
    df = pd.DataFrame(normalised, index=index)
    return df


def _normalise(row: dict) -> dict:
    close = row.get("close")
    if close is None:
        close = row.get("high", row.get("low", 1.30000))
    base = {
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 0.0,
        "atr_14": 0.0010,
        "ema_8": float("nan"),
        "ema_13": float("nan"),
        "ema_21": float("nan"),
        "ema_50": close,
        "ema_100": float("nan"),
        "ema_200": float("nan"),
        "bb_upper_20_2": close + 0.0020,
        "bb_mid_20_2": close,
        "bb_lower_20_2": close - 0.0020,
        "bb_width_norm_20_2": 1.0,
        "macd_hist_12_26_9": 0.0,
        "ema_slope_norm_50_10": 0.0,
    }
    base.update(row)
    return base


def warmup_rows(n: int, *, close: float = 1.30000, atr: float = 0.0010) -> list[dict]:
    """Return ``n`` flat rows centred on ``close``.

    Used to satisfy the engine's MIN_CANDLES_M5 threshold without
    contaminating the test's signal pattern.
    """
    return [
        {
            "open": close,
            "high": close + 0.0003,
            "low": close - 0.0003,
            "close": close,
            "atr_14": atr,
        }
        for _ in range(n)
    ]


# ---------------------------------------------------------------------------
# Reaction-builder helpers — produce 3 bars that should trigger the named
# reaction when analysed against a known support / resistance zone.
#
# Convention: support zone centred at ``s``, resistance at ``r``. ATR is
# small enough that zone_half_width ≈ 4 pips (pip floor for GBPUSD).
# ---------------------------------------------------------------------------


def build_support_rejection(support: float, *, atr: float = 0.0010) -> list[dict]:
    """Bars N-2/N-1/N forming a SUPPORT_REJECTION at ``support``."""
    midpoint = support
    return [
        # Setup — clean bar above support.
        {"open": support + 0.0010, "high": support + 0.0015,
         "low": support + 0.0005, "close": support + 0.0010, "atr_14": atr},
        # Rejection — wicks into support zone, closes back above midpoint.
        {"open": support + 0.0008, "high": support + 0.0012,
         "low": support - 0.0003, "close": support + 0.0009, "atr_14": atr},
        # Confirmation — holds above midpoint.
        {"open": support + 0.0009, "high": support + 0.0015,
         "low": support + 0.0006, "close": support + 0.0012, "atr_14": atr},
    ]


def build_resistance_rejection(resistance: float, *, atr: float = 0.0010) -> list[dict]:
    return [
        {"open": resistance - 0.0010, "high": resistance - 0.0005,
         "low": resistance - 0.0015, "close": resistance - 0.0010, "atr_14": atr},
        {"open": resistance - 0.0008, "high": resistance + 0.0003,
         "low": resistance - 0.0012, "close": resistance - 0.0009, "atr_14": atr},
        {"open": resistance - 0.0009, "high": resistance - 0.0005,
         "low": resistance - 0.0015, "close": resistance - 0.0012, "atr_14": atr},
    ]


def build_support_sweep_reclaim(support: float, *, atr: float = 0.0010) -> list[dict]:
    return [
        # Setup — sweep below zone_low (zone_low ≈ support - 0.0004 for 4-pip half).
        {"open": support, "high": support + 0.0005,
         "low": support - 0.0020, "close": support - 0.0010, "atr_14": atr},
        # Reclaim — close back above midpoint.
        {"open": support - 0.0010, "high": support + 0.0008,
         "low": support - 0.0012, "close": support + 0.0005, "atr_14": atr},
        # Confirmation — bullish bar that holds above support.
        {"open": support + 0.0005, "high": support + 0.0015,
         "low": support + 0.0002, "close": support + 0.0012, "atr_14": atr},
    ]


def build_resistance_sweep_reclaim(resistance: float, *, atr: float = 0.0010) -> list[dict]:
    return [
        {"open": resistance, "high": resistance + 0.0020,
         "low": resistance - 0.0005, "close": resistance + 0.0010, "atr_14": atr},
        {"open": resistance + 0.0010, "high": resistance + 0.0012,
         "low": resistance - 0.0008, "close": resistance - 0.0005, "atr_14": atr},
        {"open": resistance - 0.0005, "high": resistance - 0.0002,
         "low": resistance - 0.0015, "close": resistance - 0.0012, "atr_14": atr},
    ]


def build_support_acceptance_break(support: float, *, atr: float = 0.0010) -> list[dict]:
    """Two consecutive closes below ``zone_low``."""
    zone_low = support - 0.0004  # 4-pip half-width on GBPUSD
    return [
        # Setup — bar still inside (close above zone_low).
        {"open": support, "high": support + 0.0003,
         "low": support - 0.0002, "close": support, "atr_14": atr},
        # Rejection — first close below zone_low.
        {"open": support - 0.0001, "high": support + 0.0001,
         "low": zone_low - 0.0015, "close": zone_low - 0.0010, "atr_14": atr},
        # Confirmation — second consecutive close below; bearish-bodied.
        {"open": zone_low - 0.0005, "high": zone_low - 0.0003,
         "low": zone_low - 0.0025, "close": zone_low - 0.0020, "atr_14": atr},
    ]


def build_resistance_acceptance_break(resistance: float, *, atr: float = 0.0010) -> list[dict]:
    zone_high = resistance + 0.0004
    return [
        {"open": resistance, "high": resistance + 0.0002,
         "low": resistance - 0.0003, "close": resistance, "atr_14": atr},
        {"open": resistance + 0.0001, "high": zone_high + 0.0015,
         "low": resistance - 0.0001, "close": zone_high + 0.0010, "atr_14": atr},
        {"open": zone_high + 0.0005, "high": zone_high + 0.0025,
         "low": zone_high + 0.0003, "close": zone_high + 0.0020, "atr_14": atr},
    ]


def build_failed_reclaim_below_support(support: float, *, atr: float = 0.0010) -> list[dict]:
    zone_low = support - 0.0004
    return [
        # Setup — clear break: closes well below zone_low.
        {"open": zone_low - 0.0005, "high": zone_low - 0.0002,
         "low": zone_low - 0.0020, "close": zone_low - 0.0015, "atr_14": atr},
        # Rejection — retests from underneath. High enters zone but close
        # remains below zone_low (failed reclaim).
        {"open": zone_low - 0.0010, "high": zone_low + 0.0002,
         "low": zone_low - 0.0015, "close": zone_low - 0.0005, "atr_14": atr},
        # Confirmation — bearish continuation, closes further below rejection.
        {"open": zone_low - 0.0005, "high": zone_low - 0.0003,
         "low": zone_low - 0.0025, "close": zone_low - 0.0020, "atr_14": atr},
    ]


def build_failed_reclaim_above_resistance(resistance: float, *, atr: float = 0.0010) -> list[dict]:
    zone_high = resistance + 0.0004
    return [
        {"open": zone_high + 0.0005, "high": zone_high + 0.0020,
         "low": zone_high + 0.0002, "close": zone_high + 0.0015, "atr_14": atr},
        {"open": zone_high + 0.0010, "high": zone_high + 0.0015,
         "low": zone_high - 0.0002, "close": zone_high + 0.0005, "atr_14": atr},
        {"open": zone_high + 0.0005, "high": zone_high + 0.0025,
         "low": zone_high + 0.0003, "close": zone_high + 0.0020, "atr_14": atr},
    ]


# ---------------------------------------------------------------------------
# Lightweight fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def base_ts() -> datetime:
    return _BASE_TS


@pytest.fixture
def gbpusd_pair() -> str:
    return "GBPUSD"
