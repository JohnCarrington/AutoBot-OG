"""Time-windowed alert grouping (Phase 9).

The coalescer's job is to collapse bursts of similar events into one
Telegram message instead of N individual ones. The grouping key is
``(category, event_subtype, pair)`` (locked in the Phase 9 plan
refinement) — so a single GBPUSD TRADE_OPENED followed 25 seconds
later by a single EURUSD TRADE_OPENED produces *two* messages
(different keys), but three BROKER_ORPHAN findings for the same pair
in the same 30-second window become *one* summary.

Semantics:

- **First alert of a key** — held in a pending group; not sent.
- **Subsequent non-CRITICAL alert of the same key, within window** —
  appended to the pending group; not sent.
- **Subsequent non-CRITICAL alert of the same key, after window** —
  flushes the existing group (returned to caller for delivery) and
  starts a new pending group with the incoming alert.
- **CRITICAL alert (any key)** — bypasses coalescing entirely. Any
  pending non-CRITICAL alerts for the same key flush first
  (preserving timeline ordering), then the CRITICAL is delivered
  on its own.
- **Side effect on every call** — every ``add()`` and every
  ``tick()`` also flushes any *other* pending groups whose windows
  have elapsed. This is what lets the single-threaded design work:
  callers don't have to poll; activity on any key opportunistically
  drains stale groups for every other key.
- **Shutdown drain** — :py:meth:`drain_all` flushes every pending
  group regardless of window age. Called from
  :py:meth:`TelegramAlerter.close` so pending alerts go out before
  the process exits.

Threading: no internal locks. Every public method runs on the
caller's thread; the alerter is expected to call from the LS reader
thread (Phase 8's single-event-loop model). Multi-thread callers
would need to wrap in their own lock — out of scope for v1.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from .types import Alert, AlertCategory, AlertSeverity


CoalesceKey = tuple[AlertCategory, str, Optional[str]]
"""``(category, event_subtype, pair)`` — locked in the Phase 9 plan."""


@dataclass
class _PendingGroup:
    """A single pending coalesce bucket."""

    key: CoalesceKey
    first_arrival_utc: datetime
    alerts: list[Alert] = field(default_factory=list)


class AlertCoalescer:
    """Time-windowed grouping for non-CRITICAL alerts.

    Parameters
    ----------
    window_seconds : int
        Coalesce window. Alerts of the same key arriving within
        ``window_seconds`` of the group's first arrival collapse into
        one batch.
    clock : callable, optional
        Test seam — defaults to :py:func:`datetime.now(timezone.utc)`.
    """

    def __init__(
        self,
        *,
        window_seconds: int,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError(
                f"window_seconds must be positive, got {window_seconds}"
            )
        self._window = timedelta(seconds=window_seconds)
        self._window_seconds = window_seconds
        self._clock: Callable[[], datetime] = clock or (
            lambda: datetime.now(timezone.utc)
        )
        self._pending: dict[CoalesceKey, _PendingGroup] = {}

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def window_seconds(self) -> int:
        return self._window_seconds

    @property
    def pending_count(self) -> int:
        """Total alerts currently held across all pending groups."""
        return sum(len(g.alerts) for g in self._pending.values())

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def add(self, alert: Alert) -> list[list[Alert]]:
        """Register ``alert`` and return any batches that became ready.

        Returns a list of batches (each a list of alerts). Each batch
        becomes one Telegram message. A common return is an empty
        list (alert added to a still-open pending group). Possible
        non-empty returns:

        - One batch — a different key's group window had elapsed, OR
          this alert is CRITICAL and the same-key pending group was
          flushed ahead of it.
        - Two batches — both of the above happened in one ``add()``.
        - Larger — multiple other-key groups had elapsed in parallel.

        Callers should iterate the returned batches and deliver each
        as one message, in order.
        """
        now = self._clock()
        batches: list[list[Alert]] = self._flush_elapsed(now=now)

        key = alert.coalesce_key()

        if alert.severity is AlertSeverity.CRITICAL:
            # CRITICAL bypasses coalescing. If a same-key non-CRITICAL
            # group is pending, flush it FIRST so the ordering on the
            # operator's screen reads as it happened.
            same_key_pending = self._pending.pop(key, None)
            if same_key_pending is not None and same_key_pending.alerts:
                batches.append(same_key_pending.alerts)
            batches.append([alert])
            return batches

        # Non-CRITICAL: append to existing group or start a new one.
        existing = self._pending.get(key)
        if existing is None:
            self._pending[key] = _PendingGroup(
                key=key, first_arrival_utc=now, alerts=[alert],
            )
            return batches

        # The elapsed check at the top already flushed expired groups
        # — ``existing`` is by construction still within its window.
        existing.alerts.append(alert)
        return batches

    def tick(self) -> list[list[Alert]]:
        """Flush every pending group whose window has elapsed.

        Returns the list of newly-ready batches in arbitrary order
        (dict iteration order, which is insertion order). Called
        opportunistically by the alerter — typically from
        :py:meth:`BotLoop._handle_bar_close` and on feed state
        transitions — so pending groups don't sit forever during
        quiet periods.
        """
        return self._flush_elapsed(now=self._clock())

    def drain_all(self) -> list[list[Alert]]:
        """Flush every pending group regardless of window age.

        Called by :py:meth:`TelegramAlerter.close` during the shutdown
        drain so no buffered alert is silently dropped. Returns
        batches in insertion order.
        """
        out = [g.alerts for g in self._pending.values() if g.alerts]
        self._pending.clear()
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _flush_elapsed(self, *, now: datetime) -> list[list[Alert]]:
        """Pop and return every pending group whose window has elapsed."""
        ready: list[list[Alert]] = []
        expired_keys: list[CoalesceKey] = []
        for key, group in self._pending.items():
            if now - group.first_arrival_utc >= self._window:
                if group.alerts:
                    ready.append(list(group.alerts))
                expired_keys.append(key)
        for key in expired_keys:
            self._pending.pop(key, None)
        return ready


__all__ = ["AlertCoalescer", "CoalesceKey"]
