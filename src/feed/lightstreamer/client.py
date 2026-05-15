"""Lightstreamer subscription wrapper (Phase 7).

:class:`LightstreamerSubscriber` is the Lightstreamer SDK adapter
sitting between the broker SDK (``lightstreamer.client``) and the
:class:`feed.feed_manager.FeedManager`. It owns:

- Construction of the ``LightstreamerClient`` with the right
  endpoint and IG-issued ``CST-…|XST-…`` password format.
- One :class:`lightstreamer.client.Subscription` per pair, all on
  the locked ``CHART:{epic}:5MINUTE`` / ``MERGE`` adapter.
- Routing of every update through stateless parsing into a
  :class:`feed.types.Candle`, then handing the candle (plus the
  ``CONS_END`` flag) to a callback supplied by the feed manager.
- Status-change notifications: every transition into / out of
  ``CONNECTED:*`` is forwarded to a status callback so the feed
  manager can emit ``FEED_STALE`` / ``FEED_RESUMED`` events.

What this module does NOT own:

- Dedup or BAR_UPDATE / BAR_CLOSE selection — the manager decides.
- Gap-fill on reconnect — also the manager.
- IG REST credentials or session refresh — those live in
  :py:mod:`feed.ig_rest.auth`. The subscriber takes the already-
  extracted CST + XST tokens.

Threading model:

The LS SDK fires every callback on its internal reader thread. The
manager's callbacks are *not* re-dispatched onto a queue here — they
run on the LS thread. The two consumers we hand the candles to
(:class:`feed.rolling_buffer.RollingBuffer` and
:class:`feed.archive.CandleArchive`) are both thread-safe, and the
event callbacks the manager forwards to strategy code are documented
as "may run on the LS thread".
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..constants import (
    LIGHTSTREAMER_CANDLE_ADAPTER,
    LIGHTSTREAMER_CANDLE_FIELDS,
    LIGHTSTREAMER_CANDLE_MODE,
    LIGHTSTREAMER_ENDPOINT_BY_ACC,
)
from ..types import Candle
from .parsers import PartialPayloadError, is_bar_close, parse_chart_payload

logger = logging.getLogger(__name__)


# Default deadline for a fresh ``connect()`` to reach a CONNECTED:* state.
# The probe used 10 s and connected in <2 s — 15 s is the same headroom
# with a safety margin.
LS_CONNECT_TIMEOUT_SEC = 15.0


# ---------------------------------------------------------------------------
# Public callback signatures
# ---------------------------------------------------------------------------


# (pair, candle, is_close, raw_payload) -> None
UpdateCallback = Callable[[str, Candle, bool, dict[str, Any]], None]

# (new_status, previous_status) -> None
StatusCallback = Callable[[str, Optional[str]], None]


@dataclass(frozen=True)
class SubscriptionSpec:
    """One pair's Lightstreamer subscription parameters."""

    pair: str
    epic: str


# ---------------------------------------------------------------------------
# Subscriber
# ---------------------------------------------------------------------------


class LightstreamerSubscriber:
    """Wraps ``LightstreamerClient`` + one Subscription per pair.

    Parameters
    ----------
    acc_type : str
        ``"LIVE"`` or ``"DEMO"`` — picks the LS endpoint.
    account_id : str
        IG sub-account id used as the LS username.
    cst, xst : str
        Tokens extracted from a logged-in :py:class:`IGSession` via
        :py:meth:`feed.ig_rest.auth.IGSession.service.session.headers`.
    on_update : UpdateCallback
        Called for every payload, on the LS reader thread.
    on_status : StatusCallback, optional
        Called on every LS status transition. Defaults to a no-op.
    ls_client_factory : callable, optional
        Test seam: replaces the real ``LightstreamerClient`` with
        a fake. Production code lets this default to ``None`` and
        the lightstreamer SDK is imported lazily.
    """

    def __init__(
        self,
        *,
        acc_type: str,
        account_id: str,
        cst: str,
        xst: str,
        on_update: UpdateCallback,
        on_status: Optional[StatusCallback] = None,
        ls_client_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        if acc_type not in LIGHTSTREAMER_ENDPOINT_BY_ACC:
            raise ValueError(
                f"acc_type must be 'LIVE' or 'DEMO', got {acc_type!r}"
            )
        self._acc_type = acc_type
        self._endpoint = LIGHTSTREAMER_ENDPOINT_BY_ACC[acc_type]
        self._account_id = account_id
        self._cst = cst
        self._xst = xst
        self._on_update = on_update
        self._on_status: StatusCallback = on_status or (lambda *_: None)
        self._ls_client_factory = ls_client_factory

        self._client: Any = None
        self._subscriptions: dict[str, Any] = {}
        self._previous_status: Optional[str] = None
        self._status_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self, timeout_sec: float = LS_CONNECT_TIMEOUT_SEC) -> str:
        """Open the LS connection and block until it reaches CONNECTED:*.

        Raises
        ------
        TimeoutError
            If the SDK does not report a connected status within
            ``timeout_sec``.
        """
        if self._client is not None:
            return self._safe_status()
        client = self._build_client()
        client.addListener(_ClientStatusForwarder(self))
        client.connectionDetails.setUser(self._account_id)
        client.connectionDetails.setPassword(f"CST-{self._cst}|XST-{self._xst}")
        client.connect()
        self._client = client
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            status = self._safe_status()
            # Match "CONNECTED:*" prefix — "DISCONNECTED" contains
            # "CONNECTED" as a substring, so a naive `in` check would
            # accept the wrong status.
            if status.upper().startswith("CONNECTED:") or status.upper() == "CONNECTED":
                logger.info("Lightstreamer connected (status=%s)", status)
                return status
            time.sleep(0.1)
        raise TimeoutError(
            f"LS failed to reach CONNECTED within {timeout_sec:.1f}s "
            f"(last status={self._safe_status()!r})"
        )

    def subscribe_pair(self, spec: SubscriptionSpec) -> None:
        """Subscribe to one pair's CHART:5MINUTE feed.

        Subsequent calls for the same pair are no-ops.
        """
        if spec.pair in self._subscriptions:
            return
        if self._client is None:
            raise RuntimeError(
                "subscribe_pair called before connect — call connect() first"
            )
        sub = self._build_subscription(spec)
        listener = _SubscriptionForwarder(self, spec.pair)
        sub.addListener(listener)
        self._client.subscribe(sub)
        self._subscriptions[spec.pair] = sub
        logger.info(
            "Lightstreamer subscribed: pair=%s item=%s",
            spec.pair,
            LIGHTSTREAMER_CANDLE_ADAPTER.format(epic=spec.epic),
        )

    def disconnect(self) -> None:
        """Drop all subscriptions and close the LS session."""
        for pair, sub in list(self._subscriptions.items()):
            try:
                if self._client is not None:
                    self._client.unsubscribe(sub)
            except Exception as exc:
                logger.warning(
                    "unsubscribe(%s) failed (ignored): %s", pair, exc,
                )
        self._subscriptions.clear()
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception as exc:
                logger.warning("LS client disconnect failed (ignored): %s", exc)
        self._client = None

    # ------------------------------------------------------------------
    # Properties / introspection
    # ------------------------------------------------------------------

    @property
    def status(self) -> str:
        return self._safe_status()

    @property
    def subscribed_pairs(self) -> tuple[str, ...]:
        return tuple(sorted(self._subscriptions))

    # ------------------------------------------------------------------
    # SDK callback receivers (called from LS thread, public for forwarders)
    # ------------------------------------------------------------------

    def _handle_status_change(self, new_status: str) -> None:
        with self._status_lock:
            prev = self._previous_status
            self._previous_status = new_status
        logger.debug(
            "LS status change: %s -> %s", prev or "<initial>", new_status,
        )
        try:
            self._on_status(new_status, prev)
        except Exception:
            # A misbehaving status callback must never tear down the LS
            # reader thread — log and continue. Same posture as the
            # SubscriptionListener path below.
            logger.exception("on_status callback raised")

    def _handle_item_update(
        self, pair: str, payload: dict[str, Any]
    ) -> None:
        try:
            candle = parse_chart_payload(pair, payload)
        except PartialPayloadError as exc:
            logger.debug("LS %s: partial payload skipped (%s)", pair, exc)
            return
        except Exception:
            logger.exception("LS %s: unexpected parse failure", pair)
            return
        close_flag = is_bar_close(payload)
        try:
            self._on_update(pair, candle, close_flag, payload)
        except Exception:
            logger.exception("on_update callback raised for %s", pair)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_client(self) -> Any:
        if self._ls_client_factory is not None:
            return self._ls_client_factory(self._endpoint, "DEFAULT")
        # Lazy import keeps test envs without the LS SDK happy.
        from lightstreamer.client import LightstreamerClient  # type: ignore

        return LightstreamerClient(self._endpoint, "DEFAULT")

    def _build_subscription(self, spec: SubscriptionSpec) -> Any:
        item = LIGHTSTREAMER_CANDLE_ADAPTER.format(epic=spec.epic)
        if self._ls_client_factory is not None:
            # In tests, the factory exposes a callable on the client for
            # building subscriptions. Falling back to the real SDK is
            # safe — the LS SDK only complains at .subscribe() time, not
            # construction, if it isn't installed.
            try:
                from lightstreamer.client import Subscription  # type: ignore
            except Exception:
                sub_factory = getattr(
                    self._client, "_test_make_subscription", None
                )
                if sub_factory is None:
                    raise RuntimeError(
                        "Lightstreamer SDK not importable and no test "
                        "_test_make_subscription on the fake client"
                    )
                return sub_factory(
                    mode=LIGHTSTREAMER_CANDLE_MODE,
                    items=[item],
                    fields=list(LIGHTSTREAMER_CANDLE_FIELDS),
                )
            return Subscription(
                mode=LIGHTSTREAMER_CANDLE_MODE,
                items=[item],
                fields=list(LIGHTSTREAMER_CANDLE_FIELDS),
            )
        from lightstreamer.client import Subscription  # type: ignore

        return Subscription(
            mode=LIGHTSTREAMER_CANDLE_MODE,
            items=[item],
            fields=list(LIGHTSTREAMER_CANDLE_FIELDS),
        )

    def _safe_status(self) -> str:
        if self._client is None:
            return "DISCONNECTED"
        try:
            return str(self._client.getStatus() or "DISCONNECTED")
        except Exception:
            return "DISCONNECTED"


# ---------------------------------------------------------------------------
# LS SDK forwarders
# ---------------------------------------------------------------------------
#
# The LS SDK's listener classes are abstract bases the user subclasses.
# We use them as thin adapters: every method that the SDK calls back
# into is delegated to the LightstreamerSubscriber so the test path
# (which bypasses the SDK entirely) doesn't need to subclass anything.


def _try_import_subscription_listener() -> Any:
    try:
        from lightstreamer.client import SubscriptionListener  # type: ignore

        return SubscriptionListener
    except Exception:
        return object


def _try_import_client_listener() -> Any:
    try:
        from lightstreamer.client import ClientListener  # type: ignore

        return ClientListener
    except Exception:
        return object


_SubscriptionListenerBase = _try_import_subscription_listener()
_ClientListenerBase = _try_import_client_listener()


class _SubscriptionForwarder(_SubscriptionListenerBase):  # type: ignore[misc]
    """Adapter from ``SubscriptionListener`` to the subscriber."""

    def __init__(self, subscriber: LightstreamerSubscriber, pair: str) -> None:
        # Avoid super().__init__() — the LS SDK's base class doesn't
        # define one and the stub fallback is ``object`` which doesn't
        # need it either.
        self._subscriber = subscriber
        self._pair = pair

    def onItemUpdate(self, item_update: Any) -> None:  # noqa: N802 — LS API
        payload: dict[str, Any] = {}
        for field_name in LIGHTSTREAMER_CANDLE_FIELDS:
            try:
                payload[field_name] = item_update.getValue(field_name)
            except Exception:
                payload[field_name] = None
        self._subscriber._handle_item_update(self._pair, payload)

    def onSubscriptionError(  # noqa: N802 — LS API
        self, code: Any, message: Any
    ) -> None:
        logger.error(
            "LS subscription error for %s: code=%s message=%s",
            self._pair, code, message,
        )

    # Other SDK callbacks (onSubscription, onUnsubscription, onEndOfSnapshot,
    # onItemLostUpdates, onClearSnapshot) are intentionally inherited
    # as no-ops — the manager does not consume them in Phase 7.


class _ClientStatusForwarder(_ClientListenerBase):  # type: ignore[misc]
    """Adapter from ``ClientListener`` to the subscriber."""

    def __init__(self, subscriber: LightstreamerSubscriber) -> None:
        self._subscriber = subscriber

    def onStatusChange(self, new_status: Any) -> None:  # noqa: N802 — LS API
        self._subscriber._handle_status_change(str(new_status))

    def onServerError(self, code: Any, message: Any) -> None:  # noqa: N802 — LS API
        logger.error("LS server error: code=%s message=%s", code, message)


__all__ = [
    "LightstreamerSubscriber",
    "StatusCallback",
    "SubscriptionSpec",
    "UpdateCallback",
]
