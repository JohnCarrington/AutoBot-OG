"""Shared types for the Phase 9 alerts layer.

Frozen :class:`Alert` envelope plus the two closed-set enums callers
use to tag it. The alerter / coalescer / formatter all consume these
types — they're defined here once so the rest of the module can stay
focused on behaviour.

Severity (locked in the Phase 9 plan; Phase 10 added SHADOW_TRADE +
HEALTHCHECK_FAILED for shadow-mode and pre-market healthcheck):

- ``INFO`` — normal lifecycle: TRADE_OPENED, TRADE_CLOSED, STARTUP,
  SHUTDOWN (clean), FEED_RESUMED, SHADOW_TRADE (Phase 10 — would-be
  trade caught by SHADOW_MODE intercept, no broker call).
- ``WARNING`` — operator should look but the bot keeps running:
  AMEND_FAILED, BROKER_ORPHAN, MISSING_LOCAL_KEPT, MANUAL_SL_MOVE,
  FEED_STALE, SHADOW_GUARD_BLOCKED (Phase 10 H1 layer 2 —
  defense-in-depth: shadow_mode is true but a position-manage
  broker call was about to fire; layer 1 should have refused
  startup so this firing means a code path bypassed layer 1).
- ``CRITICAL`` — bot-stopping conditions and state-divergence
  emergencies: FAILURE_THRESHOLD_TRIPPED, SHUTDOWN (after
  crashed=True), AMEND_PERSIST_FAILED (H1, Session-3 commit-2b
  review — broker accepted amend, local persist failed; operator
  must reconcile manually), HEALTHCHECK_FAILED (Phase 10 —
  pre-market healthcheck reported one or more hard failures; bot
  should not start trading until resolved), STARTUP_ABORTED
  (Phase 10 H1 layer 1 — refused to start because the runtime
  state would have made shadow_mode unsafe; e.g.,
  shadow_mode=true with non-empty positions.json),
  SUPPORT_ACCEPTANCE_BREAK / RESISTANCE_ACCEPTANCE_BREAK (Phase 12
  STRUCTURE category — price has accepted beyond a major level;
  the structure-alerting dedupe cache still gates these so the
  operator sees at most one CRITICAL per level per 30 minutes).

Phase 12 (STRUCTURE category) adds nine event subtypes that surface
structure-engine transitions to the operator. Severity assignment is
locked in :mod:`structure_alerts.types` — every Phase 12 construction
site reads through ``severity_for(kind)``. The two ACCEPTANCE_BREAK
events are CRITICAL (locked level lost — material change to the
trade thesis); HTF_BIAS_CHANGE / STRUCTURE_MODE_CHANGE / SWEEP_RECLAIM
/ FAILED_RECLAIM are WARNING (operator-watchable transitions);
NEW_MAJOR_LEVEL / LEVEL_INVALIDATED / HOURLY_SUMMARY are INFO
(observability + passive heartbeat).

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
    """Top-level grouping. Phase 9 shipped three categories; Phase 12
    adds STRUCTURE for the structure-alerting layer. v2 may add more
    (e.g. ``RISK`` for circuit-breaker alerts that are currently out
    of scope)."""

    TRADE = "TRADE"
    RECONCILIATION = "RECONCILIATION"
    SYSTEM = "SYSTEM"
    STRUCTURE = "STRUCTURE"


# The closed set of event_subtype values Phase 9 emits. Kept as a
# tuple (not a Literal) so the alerter / tests can iterate the names
# without import gymnastics. Callers are free to pass any string —
# this tuple just documents what production uses.
EVENT_SUBTYPES: tuple[str, ...] = (
    # TRADE
    "TRADE_OPENED",
    "TRADE_CLOSED",
    "AMEND_FAILED",
    "AMEND_PERSIST_FAILED",
    "SHADOW_TRADE",
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
    "HEALTHCHECK_FAILED",
    "STARTUP_ABORTED",
    "SHADOW_GUARD_BLOCKED",
    # STRUCTURE (Phase 12) — kind→severity mapping is locked in
    # :mod:`structure_alerts.types.severity_for`. Listed here in
    # producer-order (spec §7 A–H + §11 heartbeat) so a future
    # reader can scan the catalogue without cross-referencing.
    "HTF_BIAS_CHANGE",
    "STRUCTURE_MODE_CHANGE",
    "SUPPORT_ACCEPTANCE_BREAK",
    "RESISTANCE_ACCEPTANCE_BREAK",
    "SWEEP_RECLAIM",
    "FAILED_RECLAIM",
    "NEW_MAJOR_LEVEL",
    "LEVEL_INVALIDATED",
    "HOURLY_SUMMARY",
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
