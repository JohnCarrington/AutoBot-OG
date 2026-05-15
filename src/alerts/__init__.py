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

Token-scrub filter (N1, Phase 9 review)
---------------------------------------

Importing this package installs a :py:class:`logging.Filter` on the
``alerts``, ``alerts.alerter``, ``alerts.coalescer``,
``alerts.formatter``, and ``alerts.telegram_client`` loggers. The
filter passes every record's formatted message through
:py:func:`alerts.telegram_client._scrub_exception_text` before it
leaves the logging pipeline.

Why scope to ``alerts.*`` and not root: scoping to root would touch
every record in the process and risk surprising third-party loggers.
The token only ever appears in exception strings raised from
``TelegramClient.send`` (a ``requests``-level error that embeds the
URL); those records originate inside ``alerts.*``. Belt-and-braces:
``TelegramClient.send`` already passes exception text through
``_scrub_exception_text`` before logging — this filter catches any
future caller that forgets to.
"""
from __future__ import annotations

import logging

from .alerter import TelegramAlerter
from .telegram_client import _scrub_exception_text
from .types import Alert, AlertCategory, AlertSeverity

__all__ = [
    "Alert",
    "AlertCategory",
    "AlertSeverity",
    "TelegramAlerter",
]


# Sentinel attribute marking that a logger already has the scrub
# filter installed. Idempotent install matters because the package
# may be imported multiple times in a test session (pytest
# re-discovery, sys.modules reload during plugin shuffling).
_FILTER_INSTALLED_ATTR = "_autobot_alerts_token_scrub_installed"

# Scoped to alerts.*. See module docstring for the reasoning.
_SCRUBBED_LOGGER_NAMES = (
    "alerts",
    "alerts.alerter",
    "alerts.coalescer",
    "alerts.formatter",
    "alerts.telegram_client",
)


class _TokenScrubFilter(logging.Filter):
    """Logging filter that redacts ``/bot{token}/`` URL segments.

    Two scrub paths:

    1. **Formatted message** — pre-formats ``record.getMessage()`` so
       a token embedded in a ``%s`` arg is also caught, then writes
       the scrubbed text back into ``record.msg`` and clears
       ``record.args``. Pre-formatting + clearing args is
       intentionally destructive: a filter that rewrote only
       ``record.msg`` would leak the original token via the args at
       the handler.
    2. **Traceback text** — ``logger.exception()`` (and
       ``logger.error(exc_info=True)``) attach an ``exc_info`` tuple;
       the formatter renders the traceback into ``record.exc_text``
       lazily, only when the first handler asks for it. To beat that
       laziness we eagerly render via ``logging.Formatter().formatException``
       here in the filter, scrub the result, and store it back on
       ``record.exc_text`` so every handler sees the redacted form.
       Pre-Phase-9-H1 review: without this path, ``logger.exception``
       on a network error from ``requests`` would leak the token in
       the URL embedded in the traceback frame.

    Always returns ``True`` — the filter never drops records, only
    sanitises them.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Path 1: scrub the formatted message.
        try:
            text = record.getMessage()
        except Exception:
            # If formatting itself fails, leave the record alone so
            # the handler's normal error path can surface it.
            text = None
        if text is not None:
            scrubbed = _scrub_exception_text(text)
            if scrubbed != text:
                record.msg = scrubbed
                record.args = ()

        # Path 2a: scrub already-rendered traceback text.
        if record.exc_text:
            record.exc_text = _scrub_exception_text(record.exc_text)

        # Path 2b: force-render and scrub a not-yet-formatted
        # traceback so handlers never see the unredacted form.
        if record.exc_info and not record.exc_text:
            try:
                rendered = logging.Formatter().formatException(record.exc_info)
            except Exception:
                # If exception formatting fails (rare), leave exc_info
                # in place; the handler will retry rendering and may
                # surface its own error. Don't block the filter.
                return True
            record.exc_text = _scrub_exception_text(rendered)

        return True


def _install_token_scrub_filter() -> None:
    """Install :class:`_TokenScrubFilter` on each ``alerts.*`` logger.

    Idempotent — the sentinel attribute on each logger guards against
    duplicate installs (which would otherwise mean the filter runs
    twice per record).
    """
    for name in _SCRUBBED_LOGGER_NAMES:
        target = logging.getLogger(name)
        if getattr(target, _FILTER_INSTALLED_ATTR, False):
            continue
        target.addFilter(_TokenScrubFilter())
        setattr(target, _FILTER_INSTALLED_ATTR, True)


_install_token_scrub_filter()
