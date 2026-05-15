"""Time-windowed alert grouping (Phase 9).

The coalescer's job is to collapse bursts of similar events into one
Telegram message instead of N individual ones. The grouping key is
``(category, event_subtype, pair, severity)`` (locked in the Phase 9
plan, with severity added in commit 2a per M1 from the adversarial
review) — so a single GBPUSD TRADE_OPENED followed 25 seconds later
by a single EURUSD TRADE_OPENED produces *two* messages (different
keys), but three BROKER_ORPHAN findings for the same pair in the
same 30-second window become *one* summary.

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


CoalesceKey = tuple[AlertCategory, str, Optional[str], AlertSeverity]
"""``(category, event_subtype, pair, severity)``.

Locked in the Phase 9 plan; severity added in commit 2a per M1 from
the Phase 9 adversarial review. In practice the event-subtype
catalogue assigns severity deterministically, so adding severity
rarely changes runtime grouping — but it makes the "alerts of
different severity never merge" invariant explicit at the type level
instead of implicit in the catalogue.
"""


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
        self._closed: bool = False

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

    @property
    def closed(self) -> bool:
        """True after :py:meth:`close` has been called.

        L5 (Phase 9 review): the alerter checks this so a late
        ``send()`` arriving after shutdown drain logs a WARNING and
        drops the alert instead of silently buffering it into a
        ``_pending`` group that will never flush.
        """
        return self._closed

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
            # CRITICAL bypasses coalescing. Flush any pending groups
            # for the SAME (category, event_subtype, pair) prefix
            # ahead of the CRITICAL so the operator's screen reads in
            # arrival order. Severity is part of the full coalesce key
            # (M1), but for the timeline-preservation guarantee we
            # match on the prefix — otherwise a contrived burst of
            # WARNING + CRITICAL of the same subtype would ship the
            # CRITICAL first and leave the WARNING pending.
            prefix = (alert.category, alert.event_subtype, alert.pair)
            matching_keys = [
                k for k in self._pending if k[:3] == prefix
            ]
            for k in matching_keys:
                group = self._pending.pop(k)
                if group.alerts:
                    batches.append(group.alerts)
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

    def close(self) -> None:
        """Mark the coalescer closed. Idempotent.

        L5 (Phase 9 review): pair with :py:meth:`drain_all` during the
        shutdown sequence. After this returns, :py:attr:`closed` is
        ``True`` and the alerter will refuse subsequent ``send()``
        calls instead of buffering alerts that would never flush.
        """
        self._closed = True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _flush_elapsed(self, *, now: datetime) -> list[list[Alert]]:
        """Pop and return every pending group whose window has elapsed.

        M2 (Phase 9 review): a backwards clock jump (NTP correction,
        DST-confused naive clock injected in tests) counts as
        "elapsed". The alternative — silently holding the group until
        the wall clock catches up — would mean an alert disappears for
        the duration of the jump. Treating ``now < first_arrival_utc``
        as a flush trigger is the conservative choice: at worst it
        ships a same-key alert as two messages instead of one (the
        coalescing was opportunistic anyway), but it never silently
        buries an alert behind a clock anomaly.
        """
        ready: list[list[Alert]] = []
        expired_keys: list[CoalesceKey] = []
        for key, group in self._pending.items():
            elapsed = now - group.first_arrival_utc
            if elapsed.total_seconds() < 0 or elapsed >= self._window:
                if group.alerts:
                    ready.append(list(group.alerts))
                expired_keys.append(key)
        for key in expired_keys:
            self._pending.pop(key, None)
        return ready


__all__ = ["AlertCoalescer", "CoalesceKey"]
