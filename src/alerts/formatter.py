"""Plain-text rendering for outbound Telegram messages (Phase 9).

Two rendering paths, both static methods on :class:`AlertFormatter`:

- ``format_single`` — one alert, full body. Used when the coalescer
  flushes a group of exactly one (the common case for STARTUP,
  TRADE_OPENED, etc.).
- ``format_batch`` — N alerts that share a coalesce key. Renders a
  header (emoji, event_subtype, pair, count, window) and a bulleted
  list of ``short_text`` lines. Truncates above
  :data:`ALERTS_MAX_BULLETS_IN_SUMMARY` with a ``... and (M-N) more``
  trailer.

Both paths surface :py:attr:`Alert.timestamp` (L1, Phase 9 review)
as a trailing ``(HH:MM:SS UTC)`` — for single-alert messages on the
same line as the body, and for batches once per header (using the
first alert in the batch). Stamping the alert at dispatch time gives
the operator an unambiguous "this is when the bot saw it", which
matters when Telegram delivery is lagging or coalescing held the
group across a window.

Severity → emoji mapping (locked):

- INFO    → ℹ️
- WARNING → ⚠️
- CRITICAL → 🚨

Plain text only — no Markdown / HTML escaping. The Telegram Bot API
call omits ``parse_mode`` so message bodies pass through unchanged.
Strategy and pair names contain ``_`` and other Markdown-meaningful
characters; plain text avoids the escaping burden entirely. The text
*is* sanitised against ASCII control characters (M5, Phase 9 review)
so a stray ``\\x07`` or ``\\x1b[`` in upstream debug text can't ring
a Telegram client's bell or inject an ANSI escape into the chat
transcript.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable, Optional

from .constants import ALERTS_MAX_BULLETS_IN_SUMMARY
from .types import Alert, AlertSeverity


_SEVERITY_EMOJI: dict[AlertSeverity, str] = {
    AlertSeverity.INFO: "ℹ️",       # ℹ️
    AlertSeverity.WARNING: "⚠️",    # ⚠️
    AlertSeverity.CRITICAL: "\U0001f6a8",     # 🚨
}


# M5 (Phase 9 review): strip ASCII control chars except \t \n \r from
# every user-visible text field before it reaches the Telegram payload.
# Tab/newline/CR are preserved because alert bodies legitimately use
# them (multi-line full_text, indented short_text). Everything else in
# the 0x00-0x1F range is non-printable junk that can confuse Telegram
# clients or smuggle ANSI escapes if the alerter ever logs the text
# back to a terminal.
_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sanitise(text: str) -> str:
    """Drop ASCII control chars except tab/newline/carriage-return."""
    return _CONTROL_CHAR_PATTERN.sub("", text)


def _format_timestamp_suffix(timestamp: Optional[datetime]) -> str:
    """Render the trailing ``(HH:MM:SS UTC)`` if a timestamp is set.

    L1 (Phase 9 review): Alert.timestamp is set by the caller at
    dispatch time. Surface it in the rendered text so the chat reader
    can spot delivery lag or stale coalesced batches at a glance.

    M1 (Session-3 re-review): tz-aware non-UTC timestamps are
    converted to UTC before formatting. The trailer always reads
    "UTC", so a tz-aware timestamp in another offset must be shifted
    rather than rendered with its local clock-face but the wrong
    label. Naive timestamps are assumed already-UTC (the same
    contract used everywhere else in the codebase — bot.clocks emits
    UTC datetimes and the few naive ones in fixtures are dispatch-
    time stamps).
    """
    if timestamp is None:
        return ""
    if timestamp.tzinfo is not None:
        utc_ts = timestamp.astimezone(timezone.utc)
    else:
        utc_ts = timestamp
    return f" ({utc_ts.strftime('%H:%M:%S')} UTC)"


class AlertFormatter:
    """Stateless formatter — every method is ``@staticmethod``."""

    @staticmethod
    def format_single(alert: Alert) -> str:
        """Render a single alert as one line.

        Format: ``{emoji} {event_subtype} — {full_text}{ts?}``. The
        full_text is the caller's responsibility — for trade alerts
        it typically reads like ``GBPUSD bullish @ 1.30050 SL=1.29900``.
        ``{ts?}`` is the trailing ``(HH:MM:SS UTC)`` from L1, emitted
        only when :py:attr:`Alert.timestamp` is set.
        """
        emoji = _SEVERITY_EMOJI[alert.severity]
        body = _sanitise(alert.full_text)
        return (
            f"{emoji} {alert.event_subtype} — {body}"
            f"{_format_timestamp_suffix(alert.timestamp)}"
        )

    @staticmethod
    def format_batch(alerts: list[Alert], *, window_seconds: int) -> str:
        """Render N alerts that share a coalesce key.

        For N == 1, equivalent to :py:meth:`format_single`. For N > 1,
        emits a header line plus bullet list. The coalesce key
        guarantees every alert in the batch shares ``category``,
        ``event_subtype``, ``pair``, and ``severity`` (M1, Phase 9
        review) — so the header carries those once, and the bullets
        only need to convey the per-event payload via ``short_text``.

        Pair appears in the header iff non-``None`` (system events
        like STARTUP/SHUTDOWN don't have a pair). The timestamp on the
        *first* alert in the batch is surfaced on the header (L1);
        per-bullet timestamps would clutter the typical 2-5 alert
        summary, and "when the burst started" is the operator-relevant
        anchor.
        """
        if not alerts:
            raise ValueError("format_batch called with empty alerts list")
        if len(alerts) == 1:
            return AlertFormatter.format_single(alerts[0])

        first = alerts[0]
        emoji = _SEVERITY_EMOJI[first.severity]
        n = len(alerts)
        header_parts = [f"{emoji} {first.event_subtype}", f"×{n}"]
        if first.pair:
            header_parts.append(first.pair)
        header_parts.append(f"in {window_seconds}s")
        header = " ".join(header_parts) + _format_timestamp_suffix(first.timestamp)

        visible = alerts[:ALERTS_MAX_BULLETS_IN_SUMMARY]
        bullets = [f"• {_sanitise(a.short_text)}" for a in visible]
        body_lines = [header, *bullets]
        if n > ALERTS_MAX_BULLETS_IN_SUMMARY:
            remaining = n - ALERTS_MAX_BULLETS_IN_SUMMARY
            body_lines.append(f"... and {remaining} more")
        return "\n".join(body_lines)

    @staticmethod
    def emoji_for(severity: AlertSeverity) -> str:
        """Public access to the locked emoji mapping (used by tests)."""
        return _SEVERITY_EMOJI[severity]


__all__ = ["AlertFormatter"]
