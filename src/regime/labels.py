"""Regime-classification enums.

These three enums form the alphabet the rest of the regime module speaks:

- ``RegimeLabel`` — what kind of market we think this is.
- ``Direction`` — when applicable, which way the regime is biased.
- ``Confidence`` — how strongly the H1 signals agreed when the label was emitted.

``Direction.NEUTRAL`` exists for completeness; in v1, direction-less regimes
(RANGE, VOLATILE, TRANSITION) carry ``None`` rather than ``NEUTRAL`` so that
``Optional[Direction]`` cleanly distinguishes "no direction" from "explicit
bullish/bearish bias".
"""
from __future__ import annotations

from enum import Enum


class RegimeLabel(str, Enum):
    """Top-level regime classification.

    Inherits from ``str`` so that ``RegimeLabel.TREND.value == "TREND"`` and
    serialisation as a string is trivial (``label.name`` and ``label.value``
    are both ``"TREND"``).
    """

    TREND = "TREND"
    RANGE = "RANGE"
    VOLATILE = "VOLATILE"
    TRANSITION = "TRANSITION"


class Direction(str, Enum):
    """Directional bias inside a regime.

    Used with ``RegimeLabel.TREND`` (and inherited by ``VOLATILE`` when the
    expansion happens out of a trending state). ``NEUTRAL`` is defined for
    completeness but v1 callers should prefer ``Optional[Direction] = None``
    for direction-less regimes (RANGE, VOLATILE expansions out of
    no-direction context, TRANSITION).
    """

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class Confidence(str, Enum):
    """Confidence tier emitted alongside a classification.

    - ``HIGH``: structure or slope decisively places the regime AND MACD
      histogram agrees (for trending regimes).
    - ``MEDIUM``: regime detected but a secondary signal disagrees (e.g.
      MACD contradicts a trending slope).
    - ``LOW``: degraded states — VOLATILE, TRANSITION, structure conflicts,
      indicator-data not yet warmed up.
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
