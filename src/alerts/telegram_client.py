"""Thin Telegram Bot API client used by :py:mod:`alerts.alerter`.

Wraps a single :py:func:`requests.post` to ``/bot{token}/sendMessage``.
Designed to:

- **Never raise to the caller.** Every exception (connection error,
  timeout, non-2xx response, malformed JSON) is caught and logged at
  WARNING; :py:meth:`send` returns ``bool``. Alerts are observability,
  not control-flow — a flaky Telegram must not propagate up into the
  trading loop.
- **Have no retry policy.** v1 is best-effort. Operator sees failures
  via the WARNING log and via the absence of the expected alert.
  Retry / queue persistence is Phase 10+.
- **Cap latency.** Default 5-second timeout (``ALERTS_HTTP_TIMEOUT_SEC``)
  on the underlying ``requests.post`` so a hung API doesn't block the
  LS reader thread for long. v1's send-on-LS-thread model accepts the
  bounded blocking; v2 may move to a queue + worker thread.

Test seam: a ``post_fn`` callable can be injected to replace
``requests.post``. Tests construct a fake that records calls and/or
raises specific exception classes.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

from .constants import ALERTS_HTTP_TIMEOUT_SEC

logger = logging.getLogger(__name__)


_BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"
_MAX_TEXT_LOG_LEN = 200  # truncate alert text in failure logs

# H1 (adversarial review 2026-05-15): ``requests.exceptions.ConnectionError``
# and similar lower-level exceptions stringify with the full request URL
# embedded — and the Telegram Bot API path ``/bot{token}/sendMessage``
# means the bot token lives inside the URL. Logging the exception
# verbatim would surface the token in centralised log aggregators
# (Datadog/Splunk/ELK). We scrub the ``/bot{token}/`` segment before
# any exception text reaches a log call. Telegram bot tokens are of
# the form ``{numeric_id}:{auth_string}`` — no slashes — so the
# negated character class ``[^/]+`` is a tight match.
_TOKEN_PATTERN = re.compile(r"/bot[^/]+/")
_REDACTED_TOKEN_REPLACEMENT = "/bot<redacted>/"


PostFn = Callable[..., Any]
"""Signature: same as ``requests.post`` for the kwargs we use
(``url``, ``data=...``, ``timeout=...``)."""


class TelegramClient:
    """Thin wrapper around the Telegram Bot API ``sendMessage`` endpoint.

    Parameters
    ----------
    bot_token, chat_id : str
        Credentials. The constructor does NOT validate them — passing
        an empty string here produces an object whose :py:meth:`send`
        will POST to ``/bot/sendMessage`` and get a 404. The
        no-credentials path is :py:class:`TelegramAlerter`'s
        responsibility: it checks the env vars before constructing
        this client and short-circuits to no-op mode if either is
        missing. L2 (Phase 9 review): the prior docstring read as if
        empty strings were a supported call shape — they're not.
    timeout_sec : float, optional
        HTTP request timeout. Defaults to
        :data:`ALERTS_HTTP_TIMEOUT_SEC`.
    post_fn : callable, optional
        Test seam. Defaults to :py:func:`requests.post` (lazily
        imported inside :py:meth:`send` so tests without ``requests``
        installed don't break module import — though in this project
        ``requests`` is a transitive dep of ``trading_ig``, so it's
        always present in production).
    """

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        timeout_sec: float = ALERTS_HTTP_TIMEOUT_SEC,
        post_fn: Optional[PostFn] = None,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._timeout_sec = timeout_sec
        self._post_fn = post_fn

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def send(self, text: str) -> bool:
        """POST ``text`` to the configured Telegram chat.

        Returns ``True`` iff the API returned a 2xx status. All other
        outcomes (HTTP errors, connection failures, timeouts) return
        ``False`` and log a WARNING — never raises.
        """
        url = _BASE_URL.format(token=self._bot_token)
        payload = {
            "chat_id": self._chat_id,
            "text": text,
        }
        post = self._post_fn or _default_post()
        try:
            response = post(
                url,
                data=payload,
                timeout=self._timeout_sec,
            )
        except Exception as exc:
            # Scrub the URL-embedded bot token before logging — see
            # ``_TOKEN_PATTERN`` comment at module top for the why.
            scrubbed = _scrub_exception_text(str(exc))
            logger.warning(
                "Telegram delivery failed (%s: %s) - alert text: %s",
                type(exc).__name__,
                scrubbed,
                _truncate(text),
            )
            return False

        status = getattr(response, "status_code", None)
        if status is None or not (200 <= status < 300):
            logger.warning(
                "Telegram API returned non-2xx (status=%s) - alert text: %s",
                status,
                _truncate(text),
            )
            return False
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_post() -> PostFn:
    """Lazily resolve :py:func:`requests.post`.

    Lazy because the module's import surface should not require
    ``requests`` at collection time — tests inject their own
    ``post_fn``, and module import would otherwise drag a network lib
    into every static-analysis pass.
    """
    import requests  # local import — see docstring

    return requests.post


def _truncate(text: str, max_len: int = _MAX_TEXT_LOG_LEN) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _scrub_exception_text(text: str) -> str:
    """Replace ``/bot{token}/`` URL segments with ``/bot<redacted>/``.

    H1 (adversarial review 2026-05-15): exception strings from
    ``requests`` (notably ``ConnectionError`` and ``ReadTimeout``)
    embed the full request URL — and our URL path carries the bot
    token. Without scrubbing, every WARNING-level delivery failure
    would leak the token into log aggregators. The regex anchors
    on the literal ``/bot`` + non-slash run + trailing ``/`` so it
    cannot match anything except the URL token segment.
    """
    return _TOKEN_PATTERN.sub(_REDACTED_TOKEN_REPLACEMENT, text)


__all__ = ["PostFn", "TelegramClient"]
