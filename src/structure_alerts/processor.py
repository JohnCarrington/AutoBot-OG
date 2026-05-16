"""Phase 12 processor — orchestrates diff → triggers → dedupe.

The processor is the single entry point BotLoop (C-6) calls per
BAR_CLOSE per pair. It's deliberately small: each upstream layer
(:mod:`structure_alerts.diff`,
:mod:`structure_alerts.triggers`,
:mod:`structure_alerts.dedupe`) is independently tested; the
processor just wires them in order and returns the surviving events.

Side-effect surface
-------------------

- Calls :meth:`DedupeCache.should_fire` — mutates the cache for
  events that pass the gate (the cache's "ask-and-record" contract,
  C-3).
- Does NOT persist to jsonl. The C-6 BotLoop wiring is responsible
  for calling :func:`structure_alerts.persistence.append_event_to_jsonl`
  for each returned event. Keeping persistence out of the processor
  lets tests run without filesystem setup and gives BotLoop the
  flexibility to skip persistence in edge cases (e.g., shadow-mode
  no-op alerter runs).
- Does NOT call the alerter or build the hourly summary. Those are
  also BotLoop concerns — the processor's job is "diff what changed
  this bar and tell me which events survive dedupe".

Cold-start contract
-------------------

When ``prev is None``, :func:`compute_structure_diff` already returns
``[]`` (per C-2's locked decision). The processor inherits this:
``prev=None`` means an empty event list, no dedupe-cache mutation,
no alerts. The hourly summary builder
(:func:`structure_alerts.summary.build_hourly_summary`) is a
separate code path with no ``prev`` dependency — BotLoop calls it
when ``candle.close_time.minute == 0`` regardless of cold-start
status.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from structure_engine.types import StructureState

from .dedupe import DedupeCache
from .diff import compute_structure_diff
from .triggers import changes_to_events
from .types import AlertEvent


def process_structure_alerts(
    *,
    prev: Optional[StructureState],
    curr: StructureState,
    dedupe: DedupeCache,
    now: datetime,
) -> list[AlertEvent]:
    """Run one bar's structure-alert pipeline.

    Parameters
    ----------
    prev
        Previous-bar :class:`StructureState` for the same pair, or
        ``None`` on cold start. After hydration (C-4), this is
        non-``None`` for any pair with history in
        ``data/structure/structure_state.jsonl``.
    curr
        Current-bar :class:`StructureState`. Always present.
    dedupe
        BotLoop-owned :class:`DedupeCache`. The processor mutates
        this for events that pass the gate.
    now
        Wall-clock UTC datetime, passed to both the trigger layer
        (becomes :attr:`AlertEvent.timestamp`) and the dedupe gate.
        BotLoop passes ``self._clock()``.

    Returns
    -------
    list[AlertEvent]
        Events that survived the dedupe gate, in diff/trigger emit
        order (bias → mode → reaction → new-level → invalidated).
        Empty list for cold start, invalid state, or no transitions.
    """
    changes = compute_structure_diff(prev, curr)
    if not changes:
        return []
    candidate_events = changes_to_events(changes, curr, now=now)
    surviving: list[AlertEvent] = []
    for event in candidate_events:
        if dedupe.should_fire(event.dedupe_key, event.severity, now=now):
            surviving.append(event)
    return surviving


__all__ = ["process_structure_alerts"]
