"""Replay H1 + M5 events through a RegimeEngine and annotate the M5 frame.

The applier is the "batch" front-end to the engine. Given an enriched H1
DataFrame and an M5 DataFrame, it interleaves the two streams in
chronological order (with H1 closes processed before M5 closes at the same
timestamp, so the new regime is in force for any aligned M5 bar) and emits
a copy of the M5 frame with five columns appended:

- ``regime`` — string form of the current ``RegimeLabel`` at that bar.
- ``regime_direction`` — ``"BULLISH"`` / ``"BEARISH"`` / ``None``.
- ``regime_confidence`` — ``"HIGH"`` / ``"MEDIUM"`` / ``"LOW"``.
- ``regime_reason`` — most recent reason code from the engine.
- ``regime_live`` — ``True`` if the regime is executable on this bar.

The applier is *cold-start friendly*: given any window of H1 + M5 history,
it replays from the beginning of that window. M5 bars that occur before
any H1 event has been processed receive a ``TRANSITION`` / not-live row.

H1 input requirements
---------------------
The H1 frame must already carry the indicator columns the classifier reads
(``ema_slope_norm_50_10``, ``bb_width_norm_20_2``, ``macd_hist_12_26_9``)
plus ``swing_high`` / ``swing_low`` / ``high`` / ``low`` for the
structural pattern. The applier itself appends ``structural_pattern`` to a
copy of the H1 frame before iteration.

M5 input requirements
---------------------
The M5 frame must carry whatever columns the engine's M5 validator needs
for the regime being confirmed (``close``, ``ema_50``,
``ema_slope_norm_50_10`` for TREND; ``close``, ``bb_upper_20_2``,
``bb_lower_20_2``, ``bb_width_norm_20_2`` for RANGE). Rows missing a
required value will simply fail validation (counter does not advance).
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from .classifier import add_structural_pattern_column
from .engine import RegimeEngine


_OUTPUT_COLUMNS = (
    "regime",
    "regime_direction",
    "regime_confidence",
    "regime_reason",
    "regime_live",
)


def apply_regime_to_candles(
    df_h1: pd.DataFrame,
    df_m5: pd.DataFrame,
    engine: RegimeEngine,
) -> pd.DataFrame:
    """Replay events through ``engine`` and annotate a copy of ``df_m5``.

    Parameters
    ----------
    df_h1 : DataFrame
        Enriched H1 candles (indicators + fractal swings already applied).
    df_m5 : DataFrame
        Enriched M5 candles. Indexed by timestamp (DatetimeIndex) for the
        chronological replay to be meaningful; integer indices work for
        tests but mix poorly with H1 timestamps if both are integer.
    engine : RegimeEngine
        The engine instance to replay through. Caller-supplied so that
        warm-started engines can be passed in (e.g. resumed from a saved
        state in a future iteration).

    Returns
    -------
    DataFrame
        Copy of ``df_m5`` with the five regime columns appended.

    Raises
    ------
    ValueError
        If ``df_h1`` lacks the columns required to compute the structural
        pattern (``swing_high`` / ``swing_low`` / ``high`` / ``low``).
    """
    out = df_m5.copy()
    if df_m5.empty:
        for col in _OUTPUT_COLUMNS:
            out[col] = pd.Series([], dtype=_dtype_for(col))
        return out

    # If no H1 history is provided, every M5 bar is pre-classification.
    if df_h1.empty:
        return _emit_initial(out)

    df_h1_enriched = add_structural_pattern_column(df_h1)

    # Build an event list: (timestamp, kind, positional_index). H1 events
    # sort before M5 events at the same timestamp so an H1 close in force
    # for the aligned M5 bar is processed first.
    events: list[tuple[Any, str, int]] = []
    events.extend(
        (t, "H1", i) for i, t in enumerate(df_h1_enriched.index)
    )
    events.extend((t, "M5", i) for i, t in enumerate(df_m5.index))
    events.sort(key=lambda e: (e[0], 0 if e[1] == "H1" else 1))

    n_m5 = len(df_m5)
    regime_col: list[str] = [""] * n_m5
    direction_col: list[str | None] = [None] * n_m5
    confidence_col: list[str] = [""] * n_m5
    reason_col: list[str] = [""] * n_m5
    live_col: list[bool] = [False] * n_m5
    written = [False] * n_m5

    prev_h1_row: pd.Series | None = None
    for _ts, kind, idx in events:
        if kind == "H1":
            row = df_h1_enriched.iloc[idx]
            engine.process_h1_close(row, prev_h1_row)
            prev_h1_row = row
        else:
            row = df_m5.iloc[idx]
            engine.process_m5_close(row)
            regime_col[idx] = engine.current_regime.value
            direction_col[idx] = (
                engine.current_direction.value
                if engine.current_direction is not None
                else None
            )
            confidence_col[idx] = engine.current_confidence.value
            reason_col[idx] = engine.reason
            live_col[idx] = engine.is_live()
            written[idx] = True

    # Any M5 rows that occurred before the very first H1 event won't have
    # been touched above; fill them with the engine's initial state.
    if not all(written):
        for idx in range(n_m5):
            if written[idx]:
                continue
            regime_col[idx] = "TRANSITION"
            direction_col[idx] = None
            confidence_col[idx] = "LOW"
            reason_col[idx] = "initial"
            live_col[idx] = False

    out["regime"] = pd.Series(regime_col, index=df_m5.index, dtype="string")
    out["regime_direction"] = pd.Series(
        direction_col, index=df_m5.index, dtype="string"
    )
    out["regime_confidence"] = pd.Series(
        confidence_col, index=df_m5.index, dtype="string"
    )
    out["regime_reason"] = pd.Series(
        reason_col, index=df_m5.index, dtype="string"
    )
    out["regime_live"] = pd.Series(
        live_col, index=df_m5.index, dtype="bool"
    )
    return out


def _emit_initial(df_m5_copy: pd.DataFrame) -> pd.DataFrame:
    n = len(df_m5_copy)
    df_m5_copy["regime"] = pd.Series(
        ["TRANSITION"] * n, index=df_m5_copy.index, dtype="string"
    )
    df_m5_copy["regime_direction"] = pd.Series(
        [None] * n, index=df_m5_copy.index, dtype="string"
    )
    df_m5_copy["regime_confidence"] = pd.Series(
        ["LOW"] * n, index=df_m5_copy.index, dtype="string"
    )
    df_m5_copy["regime_reason"] = pd.Series(
        ["initial"] * n, index=df_m5_copy.index, dtype="string"
    )
    df_m5_copy["regime_live"] = pd.Series(
        [False] * n, index=df_m5_copy.index, dtype="bool"
    )
    return df_m5_copy


def _dtype_for(col: str) -> str:
    return "bool" if col == "regime_live" else "string"
