"""IG REST allowance tracking.

IG publishes per-app rate limits (e.g. 30 requests / minute on the
non-trading endpoints, 60 / minute on trading). Exceeding them returns
HTTP 403 with ``"error.public-api.exceeded-api-key-allowance"`` and
sometimes a temporary ban. The legacy ``ig_auth.py`` carried a
``BACKOFF_SCHEDULE = [60, 90, 120, 180]`` retry table that escalated
on each successive throttle.

This module is the v1 equivalent: a pure trailing-window counter plus
an escalation table. The HTTP layer (``IGClient``) calls
:py:meth:`AllowanceTracker.note_request` before each outbound request
and :py:meth:`AllowanceTracker.note_throttled` if the response carries
a 403 allowance error. ``should_backoff`` returns the recommended
sleep duration (or 0 when the window has cleared).

The tracker is deliberately conservative: it tracks
``ALLOWANCE_REQUESTS_PER_MINUTE`` requests in a rolling 60-second
window and recommends a backoff that decays the throttle counter over
time. The exact IG limits depend on account type; the default below
matches the documented public floor.
"""
from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional


def _i(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw is not None else default


# --- Tunables (env-overridable) ---------------------------------------------
ALLOWANCE_REQUESTS_PER_MINUTE: int = _i("IG_ALLOWANCE_RPM", 30)
ALLOWANCE_WINDOW_SECONDS: int = 60
ALLOWANCE_BACKOFF_SCHEDULE: tuple[int, ...] = (60, 90, 120, 180)


@dataclass
class AllowanceSnapshot:
    """Read-only view of the tracker's current state (diagnostic)."""

    requests_in_window: int
    throttle_count: int
    last_throttle_at: Optional[float]


class AllowanceTracker:
    """Trailing-window REST-request counter with escalating backoff."""

    def __init__(
        self,
        *,
        requests_per_minute: int = ALLOWANCE_REQUESTS_PER_MINUTE,
        window_seconds: int = ALLOWANCE_WINDOW_SECONDS,
        backoff_schedule: tuple[int, ...] = ALLOWANCE_BACKOFF_SCHEDULE,
        clock: Optional[callable] = None,  # type: ignore[type-arg]
    ) -> None:
        if requests_per_minute < 1:
            raise ValueError("requests_per_minute must be >= 1")
        if window_seconds < 1:
            raise ValueError("window_seconds must be >= 1")
        if not backoff_schedule:
            raise ValueError("backoff_schedule must be non-empty")
        self._max_requests = requests_per_minute
        self._window = window_seconds
        self._schedule = backoff_schedule
        self._timestamps: deque[float] = deque()
        self._throttle_count = 0
        self._last_throttle_at: Optional[float] = None
        self._clock = clock or time.time

    # --- Public API ---------------------------------------------------------

    def note_request(self) -> None:
        """Record one outbound REST request at the current clock."""
        now = self._clock()
        self._evict(now)
        self._timestamps.append(now)

    def note_throttled(self) -> None:
        """Record an IG 403 allowance-exceeded response.

        The counter escalates: subsequent throttles read further into
        :data:`ALLOWANCE_BACKOFF_SCHEDULE` (capped at the longest entry).
        """
        self._throttle_count += 1
        self._last_throttle_at = self._clock()

    def should_backoff(self) -> float:
        """Return recommended sleep in seconds (0 if no backoff needed).

        Two reasons to back off:

        1. The trailing window already holds >= ``requests_per_minute``
           records — wait until the oldest expires.
        2. A recent throttle event hasn't elapsed past the current
           backoff-schedule entry.

        The maximum of the two reasons is returned. Zero means the
        caller may proceed immediately.
        """
        now = self._clock()
        self._evict(now)

        window_wait = 0.0
        if len(self._timestamps) >= self._max_requests:
            oldest = self._timestamps[0]
            window_wait = max(0.0, oldest + self._window - now)

        throttle_wait = 0.0
        if self._throttle_count > 0 and self._last_throttle_at is not None:
            idx = min(self._throttle_count - 1, len(self._schedule) - 1)
            cooldown = self._schedule[idx]
            elapsed = now - self._last_throttle_at
            throttle_wait = max(0.0, cooldown - elapsed)

        return max(window_wait, throttle_wait)

    def reset(self) -> None:
        """Forget all history. Used by tests and by manual operator action."""
        self._timestamps.clear()
        self._throttle_count = 0
        self._last_throttle_at = None

    def snapshot(self) -> AllowanceSnapshot:
        """Return a read-only view of the tracker's state (diagnostic)."""
        self._evict(self._clock())
        return AllowanceSnapshot(
            requests_in_window=len(self._timestamps),
            throttle_count=self._throttle_count,
            last_throttle_at=self._last_throttle_at,
        )

    # --- Internals ----------------------------------------------------------

    def _evict(self, now: float) -> None:
        cutoff = now - self._window
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()


__all__ = ["ALLOWANCE_BACKOFF_SCHEDULE", "AllowanceSnapshot", "AllowanceTracker"]
