"""Phase 12 dedupe cache — severity-aware cooldown gate.

The cache is a thin layer between the trigger output and the alerter
hand-off. Its single job: stop the same operator-visible event from
paging twice within its severity-specific cooldown window.

Cooldowns (locked in :mod:`structure_alerts.constants`):

- INFO     — 2 hours
- WARNING  — 1 hour
- CRITICAL — 30 minutes

CRITICAL still dedupes (30m) — the operator does not need to be
paged three times for the same break of the same level inside half
an hour. CRITICAL bypassing the Phase 9 *coalescer* is a separate
concern handled downstream in C-5.

Key construction is the trigger layer's responsibility
(:mod:`structure_alerts.triggers`). The cache treats keys as opaque
strings and indexes them in a plain ``dict``. The locked dedupe-key
shapes from the spec §9 table are documented in MODULE.md and tested
in C-2's ``test_triggers``.

Ask-and-record contract
-----------------------

:py:meth:`should_fire` is a single-call gate: it both decides whether
the event is eligible AND records the firing time on a True return.
This matters for the failure-isolation contract: if the rest of the
pipeline fails downstream (persistence error, alerter exception),
the cache has still recorded "we tried to fire this" — the operator
doesn't get a retry storm on the next bar. The cache is "have we
attempted to fire this recently", not "have we successfully
delivered".

Threading
---------

No internal locks. The structure-alerts pipeline runs inside
:meth:`BotLoop._handle_bar_close` on the LS reader thread (Phase 8's
single-event-loop model — same threading constraint Phase 9's
:class:`AlertCoalescer` is documented under). A future v2 with
concurrent BAR_CLOSE handling would need an external lock wrapping
``should_fire``.

Clock skew
----------

A backwards clock jump (NTP correction, naive clock in tests) makes
elapsed negative. We treat ``elapsed < 0`` as "eligible to fire" —
the conservative direction matching Phase 9's
:class:`AlertCoalescer` (M2 from the Phase 9 review). The
alternative — silently blocking until the wall clock catches up —
would mean alerts disappear for the duration of the jump.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from alerts import AlertSeverity

from .constants import COOLDOWN_BY_SEVERITY


logger = logging.getLogger(__name__)


class DedupeCache:
    """In-memory severity-aware dedupe cache.

    One instance is constructed by :class:`BotLoop` at startup and
    shared across all pairs. Dedupe keys are pair-prefixed by the
    trigger layer, so there is no cross-pair interference; the cache
    itself is pair-agnostic.

    Restart resets the cache. The C-4 hydration of
    ``_previous_structure`` from the structure jsonl prevents the
    most common post-restart re-fire (a bias change that was already
    alerted before the restart will not re-trigger because the
    diff layer sees the same prev as it did pre-restart).
    """

    def __init__(self) -> None:
        self._last_fired: dict[str, datetime] = {}

    @property
    def size(self) -> int:
        """Number of keys currently tracked. Diagnostic; tests assert."""
        return len(self._last_fired)

    def should_fire(
        self,
        key: str,
        severity: AlertSeverity,
        *,
        now: datetime,
    ) -> bool:
        """Return ``True`` if ``key`` is eligible to fire; record on True.

        Side effect: when this returns ``True``, ``now`` is stored as
        the new last-fired time for ``key``. A ``False`` return does
        NOT update the timer — a blocked attempt does not reset the
        cooldown clock, so a key fired at t=0 and re-attempted at
        t=15min (blocked) is still eligible at t=30min, not t=45min.

        Parameters
        ----------
        key
            Opaque dedupe key (see :class:`structure_alerts.triggers`
            for the locked format per event kind).
        severity
            Selects the cooldown window via
            :data:`structure_alerts.constants.COOLDOWN_BY_SEVERITY`.
        now
            Current time. Caller (typically the C-5 processor)
            passes ``self._clock()`` from BotLoop.
        """
        cooldown_seconds = COOLDOWN_BY_SEVERITY[severity]
        last = self._last_fired.get(key)
        if last is not None:
            elapsed = (now - last).total_seconds()
            # Block iff inside positive cooldown window. Negative
            # elapsed (clock jumped back) falls through to fire —
            # matches Phase 9 AlertCoalescer's clock-skew handling.
            if 0 <= elapsed < cooldown_seconds:
                return False
        self._last_fired[key] = now
        return True

    def last_fired(self, key: str) -> Optional[datetime]:
        """Return the last firing time for ``key`` or ``None``.

        Read-only — does not mutate state. Used in tests and for
        diagnostic logging.
        """
        return self._last_fired.get(key)

    def clear(self) -> None:
        """Drop every recorded firing time.

        Idempotent. Used by tests; production code does not call this
        — a restart resets the cache by reconstruction.
        """
        self._last_fired.clear()


__all__ = ["DedupeCache"]
