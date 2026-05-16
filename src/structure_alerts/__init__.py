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

from .alert_translator import translate_to_phase9_alert
from .constants import (
    CRITICAL_COOLDOWN_SEC,
    INFO_COOLDOWN_SEC,
    STRUCTURE_ALERTS_LOG_PATH,
    WARNING_COOLDOWN_SEC,
    quantise_price,
    structure_alerts_log_path,
)
from .dedupe import DedupeCache
from .hydration import load_latest_structure_state_per_pair
from .persistence import append_event_to_jsonl
from .processor import process_structure_alerts
from .summary import build_hourly_summary
from .types import AlertEvent, AlertEventKind, severity_for

__all__ = [
    "AlertEvent",
    "AlertEventKind",
    "CRITICAL_COOLDOWN_SEC",
    "DedupeCache",
    "INFO_COOLDOWN_SEC",
    "STRUCTURE_ALERTS_LOG_PATH",
    "WARNING_COOLDOWN_SEC",
    "append_event_to_jsonl",
    "build_hourly_summary",
    "load_latest_structure_state_per_pair",
    "process_structure_alerts",
    "quantise_price",
    "severity_for",
    "structure_alerts_log_path",
    "translate_to_phase9_alert",
]
