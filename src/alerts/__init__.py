"""alerts — Phase 9 Telegram notifier.

Public surface:

- :class:`TelegramAlerter` — outbound notifier with coalescing and
  no-op fallback when credentials are missing.
- :class:`Alert` — frozen envelope callers construct and pass to
  ``send()``.
- :class:`AlertCategory`, :class:`AlertSeverity` — closed-set enums
  for tagging.

Integration (Phase 9 commit 2): Phase 6/7/8 callers construct an
``Alert`` and call ``alerter.send(alert)``. Phase 8's BotLoop calls
``alerter.tick()`` on every BAR_CLOSE and on feed state transitions,
and ``alerter.close()`` during shutdown drain.
"""
from .alerter import TelegramAlerter
from .types import Alert, AlertCategory, AlertSeverity

__all__ = [
    "Alert",
    "AlertCategory",
    "AlertSeverity",
    "TelegramAlerter",
]
