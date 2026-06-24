"""Strategy emission contract: :py:class:`Signal` and helpers.

Every strategy returns ``Optional[Signal]`` from its public ``detect_*``
function. A non-``None`` return means "a setup is present at the latest
M5 close"; ``None`` means "no valid setup".

The fields below capture *everything* the downstream risk and execution
layers need to gate, size, and place an order — strategies do not hold
state between calls, so each Signal is self-contained.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Mapping, Optional

from day_type import DayType
from regime.labels import Direction

from .constants import M5_BAR_MINUTES


StrategyName = Literal["bb_reclaim", "ema_continuation", "liquidity_sweep"]
"""Closed set of strategy names. Downstream consumers can switch on these
values with static-type guarantees."""


@dataclass(frozen=True)
class Signal:
    """A confirmed trade setup at the latest M5 close.

    A ``Signal`` is intentionally minimal: it carries *suggested* price
    levels, not orders. The risk layer (:py:class:`risk.RiskGuard`)
    gates whether the signal becomes an order; the execution layer
    translates the price levels into broker-specific instructions.

    Fields
    ------
    pair : str
        Pair symbol (e.g. ``"GBPUSD"``). Always upper-case.
    direction : Direction
        ``Direction.BULLISH`` for longs, ``Direction.BEARISH`` for
        shorts. ``Direction.NEUTRAL`` is **not** valid here — a Signal
        always has a side.
    day_type : DayType
        The day-type under which this setup was detected. Used by
        downstream consumers to disambiguate strategy intent
        (TREND positions can hold overnight Mon-Thu; RANGE/VOLATILE
        cannot — see ``risk/rules/eod_enforcement.py``). (2a: field
        renamed from ``regime: RegimeLabel`` to ``day_type: DayType``.
        Strategies currently populate ``DayType.NORMAL`` as a placeholder
        — 2b wires the real day-type via the dispatcher. The legacy
        regime-based EOD carve-out remains in eod_enforcement.py until
        2c — strategy-pipeline tests that exercise EOD still construct
        ``ExecutionPosition`` with explicit regime values to drive that
        carve-out.)
    strategy_name : StrategyName
        Identifier for the strategy that produced the signal.
    suggested_entry_price : float
        The price the strategy thinks the position should open at —
        typically the confirmation candle's close. The execution layer
        may translate this to a market order or a limit-on-touch at
        the next bar open.
    suggested_sl_price : float
        Initial stop-loss price. Anchored to the structural
        invalidation (pierce wick / pullback low / sweep wick), padded
        by ``max(MIN_SL_PIPS[pair], multiplier × ATR_M5)``.
    suggested_tp_price : Optional[float]
        Fixed take-profit price, or ``None`` when the strategy uses a
        structure-based trailing exit (TREND and VOLATILE in v1).
    confidence_score : float
        ``0.0 <= score <= 1.0``. Currently a binary HIGH/LOW split
        per strategy (see ``strategies/constants.py``); v2 may
        introduce finer tiers.
    source_candle_ts : datetime
        Timestamp of the confirmation candle (the M5 bar whose close
        completed the 3-bar pattern). Always the ``df_m5.iloc[-1].name``
        at the moment of detection.
    invalid_after_candle_ts : datetime
        Strict cutoff: if the risk / execution layer wakes up *after*
        this timestamp, the signal is stale and should be discarded.
        Computed as ``source_candle_ts + M5_BAR_MINUTES`` minutes — the
        signal is valid until the next M5 close.
    debug : Mapping[str, Any]
        Free-form strategy-specific diagnostic payload (gate values,
        chosen multiplier, MACD alignment, etc.). Shape is **not** part
        of the contract — add fields freely without touching consumers.
    """

    pair: str
    direction: Direction
    day_type: DayType
    strategy_name: StrategyName
    suggested_entry_price: float
    suggested_sl_price: float
    suggested_tp_price: Optional[float]
    confidence_score: float
    source_candle_ts: datetime
    invalid_after_candle_ts: datetime
    debug: Mapping[str, Any]


def compute_invalid_after(source_candle_ts: datetime) -> datetime:
    """Return ``source_candle_ts + M5_BAR_MINUTES`` minutes.

    Centralised so every strategy uses the same cadence and a future
    change to ``M5_BAR_MINUTES`` propagates everywhere.
    """
    return source_candle_ts + timedelta(minutes=M5_BAR_MINUTES)


__all__ = ["Signal", "StrategyName", "compute_invalid_after"]
