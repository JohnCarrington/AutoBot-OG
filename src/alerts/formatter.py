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

Severity → emoji mapping (locked):

- INFO    → ℹ️
- WARNING → ⚠️
- CRITICAL → 🚨

Plain text only — no Markdown / HTML escaping. The Telegram Bot API
call omits ``parse_mode`` so message bodies pass through unchanged.
Strategy and pair names contain ``_`` and other Markdown-meaningful
characters; plain text avoids the escaping burden entirely.
"""
from __future__ import annotations

from typing import Iterable

from .constants import ALERTS_MAX_BULLETS_IN_SUMMARY
from .types import Alert, AlertSeverity


_SEVERITY_EMOJI: dict[AlertSeverity, str] = {
    AlertSeverity.INFO: "ℹ️",       # ℹ️
    AlertSeverity.WARNING: "⚠️",    # ⚠️
    AlertSeverity.CRITICAL: "\U0001f6a8",     # 🚨
}


class AlertFormatter:
    """Stateless formatter — every method is ``@staticmethod``."""

    @staticmethod
    def format_single(alert: Alert) -> str:
        """Render a single alert as one line.

        Format: ``{emoji} {event_subtype} — {full_text}``. The
        full_text is the caller's responsibility — for trade alerts
        it typically reads like ``GBPUSD bullish @ 1.30050 SL=1.29900``.
        """
        emoji = _SEVERITY_EMOJI[alert.severity]
        return f"{emoji} {alert.event_subtype} — {alert.full_text}"

    @staticmethod
    def format_batch(alerts: list[Alert], *, window_seconds: int) -> str:
        """Render N alerts that share a coalesce key.

        For N == 1, equivalent to :py:meth:`format_single`. For N > 1,
        emits a header line plus bullet list. The coalesce key
        guarantees every alert in the batch shares ``category``,
        ``event_subtype``, and ``pair`` — so the header carries those
        once, and the bullets only need to convey the per-event
        payload via ``short_text``.

        Pair appears in the header iff non-``None`` (system events like
        STARTUP/SHUTDOWN don't have a pair).
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
        header = " ".join(header_parts)

        visible = alerts[:ALERTS_MAX_BULLETS_IN_SUMMARY]
        bullets = [f"• {a.short_text}" for a in visible]
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
