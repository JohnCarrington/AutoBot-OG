"""Phase 12 → Phase 9 translator.

Converts :class:`structure_alerts.types.AlertEvent` into the Phase 9
:class:`alerts.Alert` envelope that :class:`alerts.TelegramAlerter`
accepts. The translation is intentionally thin — Phase 12 and Phase 9
catalogues are aligned (one STRUCTURE category, ``event_subtype`` ==
``AlertEventKind.value``), so the translator's job is field-mapping
plus the ``dedupe_key`` debug-payload injection.

``dedupe_key`` lands in :attr:`Alert.debug` so the Phase 9
persistence (and any future debug-stream consumer) can reconstruct
the Phase 12 dedupe gate's view of the event. Without it, a jsonl
record post-translation would lose the structure-alerts-layer
identity of the event and an operator trying to correlate
"this alert / that dedupe miss" would have to re-derive the key
from the body.

Timestamp policy
----------------

``Alert.timestamp`` defaults to ``event.timestamp`` (set by the
producer at bar-close time). A ``clock`` callable can be passed to
override — typically the BotLoop's ``self._clock()`` — to match the
Phase 9 dispatch-time-stamping convention used by ``_send_alert``.
The flexibility lets C-6 pick either:

- ``Alert.timestamp = event.timestamp`` — what the operator
  sees in the rendered Telegram footer is the bar-close time.
- ``Alert.timestamp = clock()`` — dispatch-time stamping, matches
  what every other Phase 9/10 alert in the codebase does.

Both are valid; the C-6 wiring will pick one and document the
rationale.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from alerts import Alert, AlertCategory

from .types import AlertEvent


def translate_to_phase9_alert(
    event: AlertEvent,
    *,
    clock: Optional[Callable[[], datetime]] = None,
) -> Alert:
    """Translate a Phase 12 :class:`AlertEvent` to a Phase 9 :class:`Alert`.

    Parameters
    ----------
    event
        The Phase 12 event surviving the dedupe gate.
    clock
        Optional callable returning current UTC datetime. When
        supplied, its return becomes :attr:`Alert.timestamp`; when
        ``None``, :attr:`event.timestamp` is used verbatim.
    """
    timestamp = clock() if clock is not None else event.timestamp
    return Alert(
        category=AlertCategory.STRUCTURE,
        event_subtype=event.kind.value,
        severity=event.severity,
        pair=event.pair,
        full_text=event.full_text,
        short_text=event.short_text,
        timestamp=timestamp,
        # Merge the dedupe_key in alongside the event's own debug
        # payload. We construct a new dict so the original
        # event.debug (frozen-ish — it's a mutable dict on a frozen
        # dataclass) is not mutated as a side effect of translation.
        debug={**event.debug, "dedupe_key": event.dedupe_key},
    )


__all__ = ["translate_to_phase9_alert"]
