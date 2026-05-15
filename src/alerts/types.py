"""Shared types for the Phase 9 alerts layer.

Frozen :class:`Alert` envelope plus the two closed-set enums callers
use to tag it. The alerter / coalescer / formatter all consume these
types — they're defined here once so the rest of the module can stay
focused on behaviour.

Severity (locked in the Phase 9 plan):

- ``INFO`` — normal lifecycle: TRADE_OPENED, TRADE_CLOSED, STARTUP,
  SHUTDOWN (clean), FEED_RESUMED.
- ``WARNING`` — operator should look but the bot keeps running:
  AMEND_FAILED, BROKER_ORPHAN, MISSING_LOCAL_KEPT, MANUAL_SL_MOVE,
  FEED_STALE.
- ``CRITICAL`` — bot-stopping conditions only:
  FAILURE_THRESHOLD_TRIPPED, SHUTDOWN (after crashed=True).

CRITICAL is the only severity that bypasses coalescing — see
:py:mod:`alerts.coalescer`. Reserving the immediate-send channel for
genuine bot-stopping conditions keeps the signal-to-noise ratio of
the Telegram chat usable in production.

Coalesce key: ``(category, event_subtype, pair)``. Two trade-opens
on different pairs 25 seconds apart produce two separate messages
(different keys) — coalescing only fires for genuinely similar
bursts (e.g. several BROKER_ORPHAN findings for the same pair from a
single reconcile pass).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class AlertSeverity(Enum):
    """Three-level severity used by the formatter (emoji) and the
    coalescer (bypass logic)."""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class AlertCategory(Enum):
    """Top-level grouping. Phase 9 ships three categories; v2 may add
    more (e.g. ``RISK`` for circuit-breaker alerts that are currently
    out of scope)."""

    TRADE = "TRADE"
    RECONCILIATION = "RECONCILIATION"
    SYSTEM = "SYSTEM"


# The closed set of event_subtype values Phase 9 emits. Kept as a
# tuple (not a Literal) so the alerter / tests can iterate the names
# without import gymnastics. Callers are free to pass any string —
# this tuple just documents what production uses.
EVENT_SUBTYPES: tuple[str, ...] = (
    # TRADE
    "TRADE_OPENED",
    "TRADE_CLOSED",
    "AMEND_FAILED",
    # RECONCILIATION
    "BROKER_ORPHAN",
    "MISSING_LOCAL_KEPT",
    "MANUAL_SL_MOVE",
    # SYSTEM
    "STARTUP",
    "SHUTDOWN",
    "FEED_STALE",
    "FEED_RESUMED",
    "FAILURE_THRESHOLD_TRIPPED",
)


@dataclass(frozen=True)
class Alert:
    """A single alert payload handed to :py:meth:`TelegramAlerter.send`.

    ``full_text`` is the body of a single-alert message (used when
    the coalescer flushes a group of one). ``short_text`` is what
    appears as a bullet inside a coalesced summary of N>1 — typically
    drops the pair (since the pair is in the header) and any
    repeated header info, leaving just the per-event payload.

    ``timestamp`` is set by the caller for testability — the alerter
    falls back to its injected clock if the caller passes ``None``,
    but the recommended pattern is to stamp at the point of dispatch
    (e.g., ``self._clock()`` inside the BotLoop handler) so the
    alert order matches the bot's view of time.

    ``debug`` carries diagnostic context (deal IDs, broker error
    codes, regime snapshots). It is logged at WARNING when Telegram
    delivery fails — operator can reconstruct context from logs
    alone if the chat is unreachable.
    """

    category: AlertCategory
    event_subtype: str
    severity: AlertSeverity
    pair: Optional[str]
    full_text: str
    short_text: str
    timestamp: Optional[datetime] = None
    debug: dict[str, Any] = field(default_factory=dict)

    def coalesce_key(
        self,
    ) -> tuple[AlertCategory, str, Optional[str], AlertSeverity]:
        """Return the tuple used by :py:class:`AlertCoalescer` to group alerts.

        M1 (Phase 9 review): severity is part of the key. Without it,
        two alerts with the same ``(category, event_subtype, pair)``
        but different severities would coalesce into a single bullet
        list, hiding a severity escalation from the operator. In
        practice the existing event-subtype catalogue assigns severity
        deterministically per subtype, so this is mostly belt-and-
        braces — but it makes the invariant explicit at the type
        level rather than implicit in the subtype catalogue.
        """
        return (self.category, self.event_subtype, self.pair, self.severity)


__all__ = [
    "Alert",
    "AlertCategory",
    "AlertSeverity",
    "EVENT_SUBTYPES",
]
