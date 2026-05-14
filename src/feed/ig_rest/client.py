"""IGClient — the integration surface for the execution layer.

Composes :py:class:`IGSession`, :py:class:`AllowanceTracker`, and the
free functions in :py:mod:`feed.ig_rest.positions` into a single
typed object. Execution code never imports ``trading_ig`` directly —
it depends on ``IGClient``, which can be substituted for a fake in
tests.

The class exposes the *minimum* surface Phase 6 needs:

- :py:meth:`open_position`
- :py:meth:`amend_position`
- :py:meth:`close_position`
- :py:meth:`fetch_open_positions`
- :py:meth:`fetch_open_position_by_deal_id`
- :py:meth:`fetch_deal_confirmation`

Markets / history are exposed by separate top-level modules
(:py:mod:`feed.ig_rest.markets`, :py:mod:`feed.ig_rest.history`) and
do not pass through ``IGClient`` — they have no interaction with the
execution layer.

Allowance-tracking is integrated: every call to a method below first
checks :py:meth:`AllowanceTracker.should_backoff`. If a non-zero
backoff is needed, the call **raises** :py:class:`AllowanceExceeded`
rather than sleeping — the caller decides whether to retry, queue,
or abort. This keeps the client non-blocking and testable.
"""
from __future__ import annotations

import logging
from typing import Optional

from . import positions as _positions_module
from .allowance import AllowanceTracker
from .auth import IGSession, create_ig_service
from .types import (
    AmendRequest,
    BrokerPosition,
    CloseRequest,
    DealConfirmation,
    OrderRequest,
)

logger = logging.getLogger(__name__)


class AllowanceExceeded(RuntimeError):
    """Raised when a call would exceed the configured REST allowance.

    Carries ``recommended_sleep_seconds`` so the caller can schedule a
    retry without re-querying the tracker.
    """

    def __init__(self, recommended_sleep_seconds: float) -> None:
        super().__init__(
            f"IG REST allowance exhausted; recommend sleeping "
            f"{recommended_sleep_seconds:.1f}s before retry"
        )
        self.recommended_sleep_seconds = recommended_sleep_seconds


class IGClient:
    """High-level IG REST client used by the execution layer.

    Constructed with an :py:class:`IGSession` and (optionally) an
    :py:class:`AllowanceTracker`. The default tracker uses the
    library defaults in :py:mod:`feed.ig_rest.allowance`. Tests
    inject a fake session + fake tracker to drive specific scenarios
    without touching the real library or wall-clock.
    """

    def __init__(
        self,
        session: IGSession,
        *,
        allowance: Optional[AllowanceTracker] = None,
    ) -> None:
        self._session = session
        self._allowance = allowance or AllowanceTracker()

    # --- Factory ------------------------------------------------------------

    @classmethod
    def create_from_env(cls, *, service_factory=None) -> "IGClient":
        """Build an :py:class:`IGClient` from env-supplied credentials.

        ``service_factory`` is a test seam — see
        :func:`feed.ig_rest.auth.create_ig_service`.
        """
        session = create_ig_service(service_factory=service_factory)
        return cls(session)

    # --- Read API -----------------------------------------------------------

    def fetch_open_positions(self) -> list[BrokerPosition]:
        """Return every open position IG reports for the account."""
        self._gate()
        try:
            return _positions_module.fetch_open_positions(self._session)
        except Exception as exc:
            self._maybe_note_throttle(exc)
            raise

    def fetch_open_position_by_deal_id(
        self, deal_id: str
    ) -> Optional[BrokerPosition]:
        """Look up a single position by deal id; ``None`` on 404."""
        self._gate()
        try:
            return _positions_module.fetch_open_position_by_deal_id(
                self._session, deal_id
            )
        except Exception as exc:
            self._maybe_note_throttle(exc)
            raise

    def fetch_deal_confirmation(self, deal_reference: str) -> DealConfirmation:
        """Look up a deal-confirmation record by reference."""
        self._gate()
        try:
            return _positions_module.fetch_deal_confirmation(
                self._session, deal_reference
            )
        except Exception as exc:
            self._maybe_note_throttle(exc)
            raise

    # --- Write API ----------------------------------------------------------

    def open_position(self, order: OrderRequest) -> DealConfirmation:
        """Submit a market / limit order."""
        self._gate()
        try:
            return _positions_module.open_position(self._session, order)
        except Exception as exc:
            self._maybe_note_throttle(exc)
            raise

    def amend_position(self, amend: AmendRequest) -> DealConfirmation:
        """Update an existing position's stop / limit levels."""
        self._gate()
        try:
            return _positions_module.amend_position(self._session, amend)
        except Exception as exc:
            self._maybe_note_throttle(exc)
            raise

    def close_position(self, close: CloseRequest) -> DealConfirmation:
        """Close an open position at market or limit."""
        self._gate()
        try:
            return _positions_module.close_position(self._session, close)
        except Exception as exc:
            self._maybe_note_throttle(exc)
            raise

    # --- Internals ----------------------------------------------------------

    def _gate(self) -> None:
        wait = self._allowance.should_backoff()
        if wait > 0:
            raise AllowanceExceeded(wait)
        self._allowance.note_request()

    def _maybe_note_throttle(self, exc: BaseException) -> None:
        """Heuristic: if IG returned an allowance error, escalate backoff.

        ``trading_ig`` doesn't expose a typed exception class for
        allowance violations; the message is the only signal. We match
        on the IG canonical error string conservatively — false
        positives just sleep for the next backoff window.
        """
        text = str(exc).lower()
        if "allowance" in text or "exceeded-api-key" in text:
            self._allowance.note_throttled()
            logger.warning(
                "IG REST allowance throttled (raw=%s); next backoff scheduled",
                exc,
            )

    @property
    def session(self) -> IGSession:
        """Underlying :py:class:`IGSession` — exposed for diagnostic use."""
        return self._session

    @property
    def allowance(self) -> AllowanceTracker:
        """Underlying :py:class:`AllowanceTracker` — exposed for diagnostic use."""
        return self._allowance


__all__ = ["AllowanceExceeded", "IGClient"]
