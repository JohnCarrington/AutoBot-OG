"""Serialisable snapshot of regime-engine state."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

import pandas as pd

if TYPE_CHECKING:  # avoid runtime import cycle
    from .engine import RegimeEngine


class RegimeState(TypedDict):
    """Snapshot of ``RegimeEngine`` state at a point in time.

    Field semantics:

    - ``current_regime`` — the regime label currently in force (post-M5
      confirmation). String form of ``RegimeLabel``.
    - ``current_direction`` — ``"BULLISH"`` / ``"BEARISH"`` for TREND (and
      VOLATILE that inherited a direction), else ``None``.
    - ``pending_regime`` — what H1 most recently emitted that hasn't yet
      been confirmed by 3 agreeing M5 closes. ``None`` when no transition
      is in flight.
    - ``m5_confirmation_count`` — number of consecutive agreeing M5 closes
      observed since the pending regime was emitted. Resets to 0 on
      disagreement or on a fresh pending transition.
    - ``last_regime_change_time`` — ISO-8601 timestamp of the most recent
      *committed* regime change. ``None`` if no commit has happened.
    - ``reason`` — last reason code from the classifier (or
      ``"committed"`` / ``"initial"`` for state-machine events).
    - ``debug`` — free-form dict of the raw signal values that fed the
      latest H1 classification. Diagnostic only; shape is not part of the
      contract.
    """

    current_regime: str
    current_direction: str | None
    pending_regime: str | None
    m5_confirmation_count: int
    last_regime_change_time: str | None
    reason: str
    debug: dict[str, Any]


def _format_time(value: Any) -> str | None:
    """Best-effort conversion of a row index value to ISO-8601 string."""
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    # Integer / RangeIndex case (common in tests).
    return str(value)


def to_dict(engine: "RegimeEngine") -> RegimeState:
    """Serialise a ``RegimeEngine`` to its ``RegimeState`` snapshot.

    Enum values are converted to their string names. Timestamps are
    rendered as ISO-8601. The ``debug`` dict is shallow-copied so callers
    can mutate the snapshot without disturbing engine internals.
    """
    return {
        "current_regime": engine.current_regime.value,
        "current_direction": (
            engine.current_direction.value
            if engine.current_direction is not None
            else None
        ),
        "pending_regime": (
            engine.pending_regime.value
            if engine.pending_regime is not None
            else None
        ),
        "m5_confirmation_count": int(engine.m5_confirmation_count),
        "last_regime_change_time": _format_time(engine.last_regime_change_time),
        "reason": str(engine.reason),
        "debug": dict(engine.debug),
    }
