"""Day-type enum: the news-calendar spine that replaces ``RegimeLabel``.

A ``DayType`` is a property of the *trading day* (NY session), derived
from the news calendar — it answers "what posture should the dispatcher
take today?" rather than "what regime is the market in right now?".

- ``BIG_NEWS_DAY`` — at least one HIGH-impact release scheduled within
  the current NY session for the queried currencies.
- ``PRE_BIG_NEWS`` — current session is calm, but a HIGH-impact release
  is scheduled within the lookahead horizon (default 24h).
- ``NORMAL`` — neither.

Inherits from ``str`` so ``DayType.NORMAL.value == "NORMAL"`` and
serialisation as a string is trivial (matches the convention
established by :py:class:`regime.labels.RegimeLabel`).
"""
from __future__ import annotations

from enum import Enum


class DayType(str, Enum):
    """News-calendar day classification."""

    BIG_NEWS_DAY = "BIG_NEWS_DAY"
    PRE_BIG_NEWS = "PRE_BIG_NEWS"
    NORMAL = "NORMAL"


__all__ = ["DayType"]
