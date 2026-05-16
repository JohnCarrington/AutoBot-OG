"""Phase 12 closed-set types for the structure-alerting layer.

Two shapes:

- :class:`AlertEventKind` — the closed catalogue of nine event kinds
  emitted by the diff/triggers pipeline (C-2). Mirrors the Phase 12
  spec §7 A–H plus :py:attr:`HOURLY_SUMMARY`.
- :class:`AlertEvent` — the frozen envelope handed to the dedupe
  gate (C-3), to the persistence layer (C-3), and finally to the
  Phase 9 :class:`alerts.Alert` translator (C-5).

The severity for each kind is locked at the spec level — operators
must not see a SUPPORT_ACCEPTANCE_BREAK with WARNING severity because
some trigger callsite forgot to pass CRITICAL. :func:`severity_for`
is the single source of truth; every callsite reads through it rather
than passing severity directly.

Coalescing parity with Phase 9
------------------------------

After translation (C-5) each :class:`AlertEvent` becomes an
:class:`alerts.Alert` with :class:`alerts.AlertCategory.STRUCTURE`
(added to the Phase 9 catalogue in C-1) and ``event_subtype`` set to
``kind.value``. The Phase 9 coalescer keys on
``(category, event_subtype, pair, severity)`` — so two
STRUCTURE_MODE_CHANGE events on different pairs ship as separate
messages, but two STRUCTURE_MODE_CHANGE events on the same pair
within the 30s window collapse. CRITICAL events
(SUPPORT_ACCEPTANCE_BREAK, RESISTANCE_ACCEPTANCE_BREAK) bypass
coalescing entirely — same rule as Phase 9.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Final, Mapping

from alerts import AlertSeverity


class AlertEventKind(Enum):
    """Closed catalogue of structure-alert events.

    Names mirror the spec §7 labels (A–H) plus the :py:attr:`HOURLY_SUMMARY`
    heartbeat. Tests assert the full set is exactly nine entries so a
    silent rename or addition surfaces in CI.
    """

    HTF_BIAS_CHANGE = "HTF_BIAS_CHANGE"
    STRUCTURE_MODE_CHANGE = "STRUCTURE_MODE_CHANGE"
    SUPPORT_ACCEPTANCE_BREAK = "SUPPORT_ACCEPTANCE_BREAK"
    RESISTANCE_ACCEPTANCE_BREAK = "RESISTANCE_ACCEPTANCE_BREAK"
    SWEEP_RECLAIM = "SWEEP_RECLAIM"
    FAILED_RECLAIM = "FAILED_RECLAIM"
    NEW_MAJOR_LEVEL = "NEW_MAJOR_LEVEL"
    LEVEL_INVALIDATED = "LEVEL_INVALIDATED"
    HOURLY_SUMMARY = "HOURLY_SUMMARY"


# Locked kind→severity mapping per Phase 12 spec §7. CRITICAL is
# reserved for the two acceptance-break events; everything else is
# INFO or WARNING per the plan. Operators rely on this mapping being
# stable — a CRITICAL bypasses coalescing and pages immediately, an
# INFO does not.
_SEVERITY_BY_KIND: Final[Mapping[AlertEventKind, AlertSeverity]] = {
    AlertEventKind.HTF_BIAS_CHANGE: AlertSeverity.WARNING,
    AlertEventKind.STRUCTURE_MODE_CHANGE: AlertSeverity.WARNING,
    AlertEventKind.SUPPORT_ACCEPTANCE_BREAK: AlertSeverity.CRITICAL,
    AlertEventKind.RESISTANCE_ACCEPTANCE_BREAK: AlertSeverity.CRITICAL,
    AlertEventKind.SWEEP_RECLAIM: AlertSeverity.WARNING,
    AlertEventKind.FAILED_RECLAIM: AlertSeverity.WARNING,
    AlertEventKind.NEW_MAJOR_LEVEL: AlertSeverity.INFO,
    AlertEventKind.LEVEL_INVALIDATED: AlertSeverity.INFO,
    AlertEventKind.HOURLY_SUMMARY: AlertSeverity.INFO,
}


def severity_for(kind: AlertEventKind) -> AlertSeverity:
    """Return the locked :class:`AlertSeverity` for ``kind``.

    Single source of truth — every event construction site reads
    through this rather than passing severity directly, so the
    severity field on :class:`AlertEvent` cannot drift from the kind.
    Raises :class:`KeyError` for any kind missing from the mapping —
    that's a programming bug (a new enum value added without a
    severity row) and should surface loudly in tests.
    """
    return _SEVERITY_BY_KIND[kind]


@dataclass(frozen=True)
class AlertEvent:
    """A single structure-alert payload.

    Produced by :func:`structure_alerts.triggers.changes_to_events`
    (C-2), gated by :class:`structure_alerts.dedupe.DedupeCache` (C-3),
    persisted to ``data/alerts/structure_alerts.jsonl`` (C-3), and
    translated to :class:`alerts.Alert` for Telegram delivery (C-5).

    Fields
    ------
    kind
        One of the nine catalogued :class:`AlertEventKind` values.
    pair
        Always a concrete pair — there are no pair-less structure
        events in v1 (unlike Phase 9's SYSTEM events). Tests assert
        non-empty.
    severity
        Required, and expected to equal ``severity_for(kind)``. Kept
        as an explicit field rather than a property so the dataclass
        carries its full state through serialisation / dedupe / log
        rendering without re-deriving anything.
    timestamp
        Bar-close UTC datetime — what the operator sees in the
        Telegram rendering's trailing ``(HH:MM:SS UTC)``. Set by the
        producer (typically ``self._clock()`` inside BotLoop).
    dedupe_key
        The string key consulted by
        :class:`structure_alerts.dedupe.DedupeCache`. Construction
        rules live in :mod:`structure_alerts.triggers` (C-2); the
        format is documented in MODULE.md per kind.
    full_text
        Body of a single-event Telegram message.
    short_text
        Bullet body when the coalescer flushes a group of >1.
    debug
        Arbitrary diagnostic payload — prev/curr snapshot diff data,
        component scores, etc. Carried through to the Phase 9
        :py:attr:`Alert.debug` so the persisted jsonl record matches
        what the alerter saw.
    """

    kind: AlertEventKind
    pair: str
    severity: AlertSeverity
    timestamp: datetime
    dedupe_key: str
    full_text: str
    short_text: str
    debug: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "AlertEvent",
    "AlertEventKind",
    "severity_for",
]
