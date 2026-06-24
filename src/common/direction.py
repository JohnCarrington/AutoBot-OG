"""Direction enum — the trade-side primitive shared across layers.

2d: relocated from ``regime.labels`` so the regime spine can be deleted.
``Direction`` is not regime-specific — strategies, risk, execution, and
the broker adapter all need to talk about BULLISH vs BEARISH trades —
so it now lives in a small ``common`` package that has no upstream
dependencies of its own.

``NEUTRAL`` is preserved for completeness; ``Optional[Direction] = None``
remains the convention for "no direction".
"""
from __future__ import annotations

from enum import Enum


class Direction(str, Enum):
    """Directional bias of a trade or signal.

    Inherits from ``str`` so ``Direction.BULLISH.value == "BULLISH"`` and
    serialisation as a string is trivial.
    """

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


__all__ = ["Direction"]
