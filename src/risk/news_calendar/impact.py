"""News-event impact classification and actual-vs-forecast deviation.

The risk layer keys off ``Impact`` to apply the v1 spec blackout rules
(``docs/v1_architecture.md`` §6.6)::

    HIGH    ±15 min hard blackout
    MEDIUM  soft block (skip new entries; existing trades unchanged)
    LOW     ignored

``compute_deviation`` and ``compute_surprise`` reproduce the legacy
AutoBot ``_calc_deviation`` / ``_compute_surprise`` helpers verbatim
(modulo type-hint cleanup). They are exposed so the calendar lookup
in ``calendar.py`` can attach BEAT/MISS/IN_LINE + direction-hint to
every released event.
"""

from __future__ import annotations

import enum
import os
from typing import Any


# 5% surprise is the default threshold above which a release is treated as
# CONTINUATION rather than REVERSAL. The legacy env var is honoured so the
# .env files from AutoBot continue to apply.
DEVIATION_THRESHOLD: float = float(
    os.getenv(
        "NEWS_DEVIATION_THRESHOLD_PCT",
        os.getenv("TE_DEVIATION_THRESHOLD_PCT", "5"),
    )
) / 100.0


class Impact(enum.Enum):
    """News event severity, in v1-spec order (HIGH most blocking)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


def parse_impact(impact_str: str | None) -> Impact:
    """Parse a Finnhub-style impact string.

    Finnhub returns lowercase strings (``"high"``, ``"medium"``, ``"low"``).
    Anything unrecognised (including ``None``) maps to ``LOW`` so the risk
    layer's failure mode is "don't block", which is the correct behaviour
    when the upstream payload is malformed — the rest of the system will
    still see the event and can decide whether to skip on other signals.
    """
    if impact_str is None:
        return Impact.LOW
    s = str(impact_str).strip().lower()
    if s == "high":
        return Impact.HIGH
    if s == "medium":
        return Impact.MEDIUM
    return Impact.LOW


def classify_beat_miss(deviation: float) -> str:
    """Threshold-gated BEAT/MISS/IN_LINE label for a fractional deviation.

    Single source of truth for the ``beat_miss`` taxonomy (review H5).
    Used by both ``compute_deviation`` and ``calendar.get_actual_for_event``'s
    Finnhub-surprise branch, so the same input always produces the same
    label regardless of which deviation source the result was derived from.

    A surprise larger than ``+DEVIATION_THRESHOLD`` is BEAT; smaller than
    ``-DEVIATION_THRESHOLD`` is MISS; everything between is IN_LINE.
    """
    if deviation > DEVIATION_THRESHOLD:
        return "BEAT"
    if deviation < -DEVIATION_THRESHOLD:
        return "MISS"
    return "IN_LINE"


def classify_direction(deviation: float) -> str:
    """Threshold-gated CONTINUATION/REVERSAL hint for a fractional deviation."""
    return "CONTINUATION" if abs(deviation) > DEVIATION_THRESHOLD else "REVERSAL"


def compute_deviation(actual: float, forecast: float) -> dict[str, Any]:
    """BEAT/MISS/IN_LINE classification with CONTINUATION/REVERSAL hint.

    Returns a dict with keys ``deviation`` (signed fraction), ``beat_miss``
    (BEAT/MISS/IN_LINE), and ``direction_hint`` (CONTINUATION/REVERSAL).
    ``deviation = (actual - forecast) / abs(forecast)``. A move that
    exceeds ``DEVIATION_THRESHOLD`` (default 5%) gets CONTINUATION;
    smaller surprises get REVERSAL (the legacy convention).
    """
    if forecast == 0:
        return {"deviation": None, "direction_hint": None, "beat_miss": None}
    deviation = (actual - forecast) / abs(forecast)
    return {
        "deviation": deviation,
        "direction_hint": classify_direction(deviation),
        "beat_miss": classify_beat_miss(deviation),
    }


def compute_surprise(actual: Any, estimate: Any) -> tuple[float | None, str | None]:
    """Defensive coarse-surprise computation.

    Used when the Finnhub event payload does not include the Enterprise-tier
    ``surprise`` field (Economic-1 plans don't get it). Returns
    ``(surprise_pct, beat_miss)`` where ``surprise_pct = (actual / estimate) - 1``.
    Returns ``(None, None)`` if either value is missing or estimate is zero.

    Note: no ``DEVIATION_THRESHOLD`` gating. This is the COARSE
    classification only — ``compute_deviation`` is the threshold-aware
    direction_hint source.
    """
    try:
        a = float(actual) if actual is not None else None
        e = float(estimate) if estimate is not None else None
        if a is None or e is None or e == 0:
            return None, None
        surprise_pct = (a / e) - 1.0
        if a > e:
            beat_miss = "BEAT"
        elif a < e:
            beat_miss = "MISS"
        else:
            beat_miss = "IN_LINE"
        return surprise_pct, beat_miss
    except (TypeError, ValueError):
        return None, None


__all__ = [
    "DEVIATION_THRESHOLD",
    "Impact",
    "parse_impact",
    "classify_beat_miss",
    "classify_direction",
    "compute_deviation",
    "compute_surprise",
]
