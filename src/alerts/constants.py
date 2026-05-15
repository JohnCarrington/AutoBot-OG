"""Alerts-layer tunables (Phase 9).

Same env-override pattern as the rest of the codebase. Read once at
import time; runtime env changes don't propagate.

Locked decisions (from the Phase 9 plan):

- ``ALERTS_COALESCE_WINDOW_SEC = 30`` — groups of similar events
  (same category, event_subtype, AND pair) sent in this window are
  collapsed into one summary message. CRITICAL severity bypasses
  entirely.
- ``ALERTS_HTTP_TIMEOUT_SEC = 5`` — per-request timeout on the
  Telegram Bot API call. No retry; failures log a WARNING and are
  swallowed.
- ``ALERTS_MAX_BULLETS_IN_SUMMARY = 5`` — coalesced summaries with
  more than this many alerts render the first N bullets plus a
  ``... and (M - N) more`` line. Tuned for typical Telegram client
  rendering width.

Credential discovery:

- ``TELEGRAM_BOT_TOKEN`` — Telegram Bot API token (from BotFather).
- ``TELEGRAM_CHAT_ID`` — numeric chat id the bot posts into.

If either env var is missing, :py:class:`alerts.alerter.TelegramAlerter`
runs in no-op mode (logs a WARNING at construction; every public
method short-circuits without touching the network). This means
local-dev environments without Telegram credentials don't need to
stub anything to run the bot.
"""
from __future__ import annotations

import os


def _i(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw is not None else default


def _f(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw is not None else default


# --- Credential env var names (not values — those resolved at runtime) ----
TELEGRAM_BOT_TOKEN_ENV: str = "TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID_ENV: str = "TELEGRAM_CHAT_ID"

# --- Coalescing ----------------------------------------------------------
ALERTS_COALESCE_WINDOW_SEC: int = _i("ALERTS_COALESCE_WINDOW_SEC", 30)
ALERTS_MAX_BULLETS_IN_SUMMARY: int = _i("ALERTS_MAX_BULLETS_IN_SUMMARY", 5)

# --- HTTP ----------------------------------------------------------------
ALERTS_HTTP_TIMEOUT_SEC: float = _f("ALERTS_HTTP_TIMEOUT_SEC", 5.0)


__all__ = [
    "ALERTS_COALESCE_WINDOW_SEC",
    "ALERTS_HTTP_TIMEOUT_SEC",
    "ALERTS_MAX_BULLETS_IN_SUMMARY",
    "TELEGRAM_BOT_TOKEN_ENV",
    "TELEGRAM_CHAT_ID_ENV",
]
