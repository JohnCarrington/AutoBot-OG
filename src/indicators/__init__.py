"""Pure-pandas technical indicators.

Each function takes a DataFrame of OHLC candles plus parameters and returns
a NEW DataFrame (a copy of the input) with one or more indicator columns
appended. The original columns are preserved unchanged, and leading rows
that cannot be computed are left as NaN (no silent forward-fill).

All functions are pair-agnostic — they operate on whatever ``open``,
``high``, ``low``, ``close`` columns are present and embed no knowledge of
any specific instrument.

Public API:
    add_ema(df, period)
    add_atr(df, period=14)
    add_bollinger(df, period=20, std_mult=2.0)
    add_macd(df, fast=12, slow=26, signal=9)
    add_ema_slope_normalised(df, period=50, lookback=10, atr_period=14)
    add_bb_width_normalised(df, bb_period=20, bb_std=2.0, atr_period=14)
"""
from .atr import add_atr
from .bollinger import add_bollinger
from .ema import add_ema
from .macd import add_macd
from .normalised import add_bb_width_normalised, add_ema_slope_normalised

__all__ = [
    "add_atr",
    "add_bb_width_normalised",
    "add_bollinger",
    "add_ema",
    "add_ema_slope_normalised",
    "add_macd",
]
