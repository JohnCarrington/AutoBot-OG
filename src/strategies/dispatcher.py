"""Regime → strategy dispatcher.

A thin selector that calls the single strategy whose regime gate
matches the engine's current regime. Returns a list (length 0 or 1
in v1) so future multi-pair / multi-signal extensions can add entries
without breaking callers.

Phase 11 update — the dispatcher signature now threads a
:class:`StructureState` through to each strategy. Strategies read it
in place of doing their own pattern detection (spec §13 gates).
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from regime.labels import RegimeLabel
from regime.state import RegimeState
from structure_engine import StructureState

from .bb_reclaim import detect_bb_reclaim
from .ema_continuation import detect_ema_continuation
from .liquidity_sweep import detect_liquidity_sweep
from .signal import Signal


def detect_all_setups(
    df_m5: pd.DataFrame,
    df_h1: pd.DataFrame,
    regime_state: RegimeState,
    structure_state: StructureState,
    pair: str,
    current_time: datetime,
) -> list[Signal]:
    """Return signals from the strategy matching ``regime_state``.

    Routing (unchanged):

    - ``RANGE``      → :py:func:`detect_bb_reclaim`
    - ``TREND``      → :py:func:`detect_ema_continuation`
    - ``VOLATILE``   → :py:func:`detect_liquidity_sweep`
    - ``TRANSITION`` → ``[]`` (nothing trades during a transition)

    Each strategy applies its spec §13 gates against ``structure_state``;
    the matrix is total and unambiguous — there is never more than one
    eligible strategy in v1.
    """
    current = regime_state.get("current_regime")
    if current == RegimeLabel.RANGE.value:
        result = detect_bb_reclaim(
            df_m5, df_h1, regime_state, structure_state, pair, current_time
        )
    elif current == RegimeLabel.TREND.value:
        result = detect_ema_continuation(
            df_m5, df_h1, regime_state, structure_state, pair, current_time
        )
    elif current == RegimeLabel.VOLATILE.value:
        result = detect_liquidity_sweep(
            df_m5, df_h1, regime_state, structure_state, pair, current_time
        )
    else:
        return []

    return [result] if result is not None else []


__all__ = ["detect_all_setups"]
