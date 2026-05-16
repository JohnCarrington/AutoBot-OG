"""Phase 12 jsonl persistence — best-effort append of AlertEvents.

One line per event into ``data/alerts/structure_alerts.jsonl``
(configurable via :data:`STRUCTURE_ALERTS_LOG_PATH`). Mirrors the
Phase 11 :mod:`structure_engine.logging` shape: module-level
:class:`threading.Lock`, ``os.makedirs(parent, exist_ok=True)``
before write, OSError logged + swallowed.

Always-on (no env toggle, unlike the engine's
``STRUCTURE_LOG_ENABLED``). The structure-alerts jsonl is the audit
log for what was sent to Telegram (or would have been, in no-op
mode); audit logs are always-on by convention. The volume is bounded
by the dedupe cooldowns and the operator-visible event rate — not by
per-bar engine output — so it stays small in production.

Failure isolation
-----------------

The structure-alerts pipeline cannot crash :meth:`BotLoop._handle_bar_close`
on a persistence error. The writer catches :class:`OSError` (disk
full, permission denied, parent-mkdir races) and logs a WARNING.
The caller (C-5 processor) treats the call as fire-and-forget.

JSON edge cases in :attr:`AlertEvent.debug`
-------------------------------------------

The debug dict is free-form and may contain non-JSON-trivial values
(NumPy scalars, datetimes, enum values that escaped translation).
We serialise with ``default=str`` so anything ``json`` can't natively
encode round-trips through ``str()`` rather than crashing. This is
the same trade-off Phase 11 logging took implicitly (its payload was
type-controlled; ours isn't) — a stray weird value lands in the
jsonl as its ``repr``, but the record still writes.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Union

from .types import AlertEvent


logger = logging.getLogger(__name__)
_lock = threading.Lock()


def append_event_to_jsonl(
    event: AlertEvent,
    path: Union[str, os.PathLike[str]],
) -> None:
    """Append ``event`` as one JSON line to ``path``. Never raises.

    Creates the parent directory if missing (``parents=True``,
    ``exist_ok=True``). On any :class:`OSError` (filesystem failure,
    permission denied, race against another process), logs at
    WARNING and returns. The structure-alerts pipeline keeps running.
    """
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(_to_payload(event), separators=(",", ":"), default=str)
        with _lock:
            with target.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError as exc:
        # Matches structure_engine.logging.log_structure_state failure
        # shape — operator can grep the journalctl stream for the
        # "structure_alerts:" prefix if the jsonl turns up missing
        # records during incident review.
        logger.warning("structure_alerts: failed to write %s: %s", target, exc)


def _to_payload(event: AlertEvent) -> dict[str, Any]:
    """Render :class:`AlertEvent` into a JSON-encodable dict.

    Hand-written rather than via :func:`dataclasses.asdict` because
    asdict does not unwrap enums or datetimes. Field order matches
    the most-useful grep order for incident triage: timestamp first,
    then kind / pair / severity, then dedupe_key, then bodies and
    debug.
    """
    return {
        "timestamp": event.timestamp.isoformat(),
        "kind": event.kind.value,
        "pair": event.pair,
        "severity": event.severity.value,
        "dedupe_key": event.dedupe_key,
        "full_text": event.full_text,
        "short_text": event.short_text,
        "debug": event.debug,
    }


__all__ = ["append_event_to_jsonl"]
