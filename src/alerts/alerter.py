"""TelegramAlerter — the public surface of the Phase 9 alerts layer.

Composition:

- :py:class:`AlertCoalescer` — windowed grouping by
  ``(category, event_subtype, pair, severity)``.
- :py:class:`AlertFormatter` — plain-text rendering with severity
  emoji + truncated bullet lists for big batches.
- :py:class:`TelegramClient` — synchronous best-effort POST to the
  Bot API with a short timeout.

The class exposes four methods:

- :py:meth:`send` — caller registers an alert. The coalescer may
  emit zero, one, or more ready batches; each is delivered as one
  Telegram message.
- :py:meth:`tick` — called by the BotLoop (per BAR_CLOSE and on
  feed state transitions) to drain pending groups whose windows
  have elapsed. Belt-and-braces against quiet periods where no
  ``send()`` runs.
- :py:meth:`close` — called from :py:meth:`BotLoop.stop`. Drains
  every pending group regardless of window age so no buffered
  alert is silently lost on shutdown.
- :py:meth:`enabled` — read-only property indicating whether
  credentials were found at construction.

No-op mode:

If either :data:`TELEGRAM_BOT_TOKEN_ENV` or :data:`TELEGRAM_CHAT_ID_ENV`
is missing at construction, a single WARNING is logged and every
public method short-circuits. The coalescer is not constructed in
this mode — pending state has nowhere useful to go.

Exception isolation:

Telegram delivery cannot affect bot correctness. Every public method
guards every delivery call with try/except — any unexpected error
inside the alerter logs at ERROR and returns control to the caller.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Callable, Optional

from .coalescer import AlertCoalescer
from .constants import (
    ALERTS_COALESCE_WINDOW_SEC,
    ALERTS_HTTP_TIMEOUT_SEC,
    TELEGRAM_BOT_TOKEN_ENV,
    TELEGRAM_CHAT_ID_ENV,
)
from .formatter import AlertFormatter
from .telegram_client import TelegramClient
from .types import Alert

logger = logging.getLogger(__name__)


_DELIVERY_LOG_MAX_LEN = 160  # truncate alert text in DEBUG success log


def _truncate_for_log(text: str, max_len: int = _DELIVERY_LOG_MAX_LEN) -> str:
    """Single-line, length-capped form of an alert body for log output.

    Newlines collapse to ``\\n`` literal so a multi-bullet batch
    summary still occupies one log line. Keeps grep across log
    aggregators from breaking across records.
    """
    one_line = text.replace("\n", "\\n")
    if len(one_line) <= max_len:
        return one_line
    return one_line[: max_len - 3] + "..."


class TelegramAlerter:
    """Outbound Telegram notifier with coalescing.

    Parameters
    ----------
    bot_token, chat_id : str, optional
        Override env-discovered credentials (used in tests). When
        ``None``, falls back to the ``TELEGRAM_BOT_TOKEN`` /
        ``TELEGRAM_CHAT_ID`` env vars. If either is empty after
        resolution, the alerter runs in no-op mode.
    coalesce_window_seconds : int, optional
        Window passed to :class:`AlertCoalescer`. Defaults to
        :data:`ALERTS_COALESCE_WINDOW_SEC`.
    http_timeout_sec : float, optional
        Per-request HTTP timeout. Defaults to
        :data:`ALERTS_HTTP_TIMEOUT_SEC`.
    client : TelegramClient, optional
        Pre-built client (test seam). When ``None`` and credentials
        are present, one is constructed here.
    clock : callable, optional
        Test seam for the coalescer's timing.
    """

    def __init__(
        self,
        *,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        coalesce_window_seconds: int = ALERTS_COALESCE_WINDOW_SEC,
        http_timeout_sec: float = ALERTS_HTTP_TIMEOUT_SEC,
        client: Optional[TelegramClient] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        token = (
            bot_token
            if bot_token is not None
            else os.getenv(TELEGRAM_BOT_TOKEN_ENV, "")
        )
        chat = (
            chat_id
            if chat_id is not None
            else os.getenv(TELEGRAM_CHAT_ID_ENV, "")
        )
        self._enabled = bool(token and chat)
        self._window_seconds = coalesce_window_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        if not self._enabled:
            logger.warning(
                "TelegramAlerter: %s and/or %s missing - alerts will be "
                "no-op. Set both env vars to enable Telegram delivery.",
                TELEGRAM_BOT_TOKEN_ENV,
                TELEGRAM_CHAT_ID_ENV,
            )
            self._client: Optional[TelegramClient] = None
            self._coalescer: Optional[AlertCoalescer] = None
            return

        self._client = client or TelegramClient(
            bot_token=token, chat_id=chat, timeout_sec=http_timeout_sec,
        )
        self._coalescer = AlertCoalescer(
            window_seconds=coalesce_window_seconds, clock=self._clock,
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """``True`` iff credentials were found at construction."""
        return self._enabled

    @property
    def pending_count(self) -> int:
        """Alerts currently buffered in the coalescer (0 when disabled)."""
        if self._coalescer is None:
            return 0
        return self._coalescer.pending_count

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def send(self, alert: Alert) -> None:
        """Register ``alert``. Delivery is best-effort; never raises.

        L5 (Phase 9 review): if the alerter has already been
        :py:meth:`close`-d, a late ``send()`` logs a WARNING and drops
        the alert. The coalescer would otherwise buffer it into a
        pending group that nothing ever flushes (BotLoop.stop has
        already returned), and the alert would die silently with the
        process.
        """
        if not self._enabled or self._coalescer is None:
            return
        if self._coalescer.closed:
            logger.warning(
                "TelegramAlerter.send called after close - alert dropped "
                "(kind=%s pair=%s severity=%s)",
                alert.event_subtype,
                alert.pair,
                alert.severity.value,
            )
            return
        try:
            batches = self._coalescer.add(alert)
        except Exception:
            # The coalescer is pure-Python in-memory state - an
            # exception here is a programming bug, not a transient
            # condition. Log loudly and continue; do not propagate.
            logger.exception(
                "AlertCoalescer.add raised - alert dropped (kind=%s pair=%s)",
                alert.event_subtype,
                alert.pair,
            )
            return
        for batch in batches:
            self._deliver(batch)

    def tick(self) -> None:
        """Drain any pending groups whose windows have elapsed.

        Called by :py:meth:`BotLoop._handle_bar_close` (per M5 close)
        and on feed state transitions (FEED_STALE / FEED_RESUMED /
        GAP_FILLED) so quiet periods don't leave pending alerts in
        limbo for hours.
        """
        if not self._enabled or self._coalescer is None:
            return
        try:
            batches = self._coalescer.tick()
        except Exception:
            logger.exception("AlertCoalescer.tick raised - pending state may be stale")
            return
        for batch in batches:
            self._deliver(batch)

    def close(self) -> None:
        """Drain every pending group and send. Idempotent.

        Called by :py:meth:`BotLoop.stop`. Telegram failures during
        the close drain are logged but never block shutdown.

        L5 (Phase 9 review): also marks the coalescer closed so any
        late :py:meth:`send` calls (e.g. from a worker that didn't
        observe the shutdown signal in time) log a WARNING and drop
        the alert instead of buffering into pending state that
        nothing will flush.
        """
        if not self._enabled or self._coalescer is None:
            return
        try:
            batches = self._coalescer.drain_all()
        except Exception:
            logger.exception(
                "AlertCoalescer.drain_all raised - pending alerts may be lost"
            )
            # Still mark closed so subsequent send() calls are
            # rejected rather than silently buffered.
            self._coalescer.close()
            return
        for batch in batches:
            self._deliver(batch)
        self._coalescer.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _deliver(self, batch: list[Alert]) -> None:
        if not batch or self._client is None:
            return
        try:
            text = AlertFormatter.format_batch(
                batch, window_seconds=self._window_seconds,
            )
        except Exception:
            logger.exception(
                "AlertFormatter.format_batch raised - batch dropped (n=%d)",
                len(batch),
            )
            return
        try:
            ok = self._client.send(text)
        except Exception:
            # TelegramClient.send already swallows; this is defense in
            # depth in case a future change makes it raise.
            logger.exception("TelegramClient.send raised unexpectedly")
            return
        if ok:
            # L3 (Phase 9 review): operators reading DEBUG logs during
            # incident response can correlate "did this alert ship?"
            # without grep'ing for the absence of a WARNING. Truncated
            # for log volume; the alerter's failure path already logs
            # at WARNING when delivery fails, so the asymmetric levels
            # match observability needs.
            logger.debug(
                "Telegram alert delivered (n=%d): %s",
                len(batch),
                _truncate_for_log(text),
            )


__all__ = ["TelegramAlerter"]
