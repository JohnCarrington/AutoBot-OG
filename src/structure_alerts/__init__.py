"""structure_alerts — Phase 12 structure-transition alerting layer.

C-1 ships only the closed-set types and tunables; the diff /
triggers / dedupe / persistence / hydration / summary / processor /
translator modules land in C-2 through C-6.

Public surface today:

- :class:`AlertEvent`, :class:`AlertEventKind` — the closed-set
  envelope produced by ``triggers.changes_to_events`` (C-2) and
  consumed by ``alert_translator.translate_to_phase9_alert`` (C-5).
- :func:`severity_for` — locked kind→severity mapping per
  Phase 12 spec §7. Used at every event construction site so the
  severity field cannot drift from the kind.
- Cooldown / path / quantisation tunables from :mod:`structure_alerts.constants`.
"""
from __future__ import annotations

from .constants import (
    CRITICAL_COOLDOWN_SEC,
    INFO_COOLDOWN_SEC,
    STRUCTURE_ALERTS_LOG_PATH,
    WARNING_COOLDOWN_SEC,
    quantise_price,
)
from .types import AlertEvent, AlertEventKind, severity_for

__all__ = [
    "AlertEvent",
    "AlertEventKind",
    "CRITICAL_COOLDOWN_SEC",
    "INFO_COOLDOWN_SEC",
    "STRUCTURE_ALERTS_LOG_PATH",
    "WARNING_COOLDOWN_SEC",
    "quantise_price",
    "severity_for",
]
