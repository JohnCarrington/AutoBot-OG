"""Tests for feed.lightstreamer.client — LightstreamerSubscriber.

The real LS SDK is mocked out via the ``ls_client_factory`` test seam.
We exercise the surface the FeedManager actually depends on:

- connect() blocks until status reads CONNECTED:* and reports it back
- connect() raises TimeoutError when status never flips
- subscribe_pair() builds a Subscription with the locked adapter / mode /
  fields and registers it with the client
- subscriber forwards SDK callbacks (item update + status change) to
  the manager-supplied callbacks
- disconnect() unsubscribes and drops the connection cleanly
"""
from __future__ import annotations

from typing import Any, Optional

import pytest

from feed.constants import (
    LIGHTSTREAMER_CANDLE_ITEM_TEMPLATE,
    LIGHTSTREAMER_CANDLE_FIELDS,
    LIGHTSTREAMER_CANDLE_MODE,
)
from feed.lightstreamer.client import (
    LightstreamerSubscriber,
    SubscriptionSpec,
)


# ---------------------------------------------------------------------------
# Fake LS SDK
# ---------------------------------------------------------------------------


class FakeItemUpdate:
    def __init__(self, fields: dict[str, Any]) -> None:
        self._fields = fields

    def getValue(self, name: str) -> Any:  # noqa: N802 — LS API
        return self._fields.get(name)


class FakeSubscription:
    def __init__(self, mode: str, items: list[str], fields: list[str]) -> None:
        self.mode = mode
        self.items = items
        self.fields = fields
        self.listeners: list[Any] = []

    def addListener(self, listener: Any) -> None:  # noqa: N802
        self.listeners.append(listener)


class FakeConnectionDetails:
    def __init__(self) -> None:
        self.user: Optional[str] = None
        self.password: Optional[str] = None

    def setUser(self, user: str) -> None:  # noqa: N802
        self.user = user

    def setPassword(self, password: str) -> None:  # noqa: N802
        self.password = password


class FakeLightstreamerClient:
    """Stands in for ``lightstreamer.client.LightstreamerClient``.

    Holds onto listeners so tests can manually fire ``onStatusChange``
    and inspect subscriptions. ``connect()`` also fires the
    ``onStatusChange`` callback to mimic the real SDK's behaviour —
    the subscriber's status state machine needs that.
    """

    def __init__(self, endpoint: str, adapter_set: str = "DEFAULT") -> None:
        self.endpoint = endpoint
        self.adapter_set = adapter_set
        self.connectionDetails = FakeConnectionDetails()
        self.listeners: list[Any] = []
        self.subscriptions: list[Any] = []
        self.unsubscribed: list[Any] = []
        self.connected = False
        self._status = "DISCONNECTED"
        self.disconnect_calls = 0

    def addListener(self, listener: Any) -> None:  # noqa: N802
        self.listeners.append(listener)

    def connect(self) -> None:
        self.connected = True
        self._status = "CONNECTED:HTTP-STREAMING"
        for l in self.listeners:
            try:
                l.onStatusChange("CONNECTED:HTTP-STREAMING")
            except Exception:
                pass

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._status = "DISCONNECTED"
        self.connected = False

    def getStatus(self) -> str:  # noqa: N802
        return self._status

    def subscribe(self, sub: Any) -> None:
        self.subscriptions.append(sub)

    def unsubscribe(self, sub: Any) -> None:
        self.unsubscribed.append(sub)


@pytest.fixture
def fake_client_factory() -> Any:
    holder: dict[str, FakeLightstreamerClient] = {}

    def factory(endpoint: str, adapter_set: str = "DEFAULT") -> FakeLightstreamerClient:
        c = FakeLightstreamerClient(endpoint, adapter_set)
        holder["c"] = c
        return c

    factory.holder = holder  # type: ignore[attr-defined]
    return factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_connect_blocks_until_connected(fake_client_factory) -> None:
    updates: list = []
    statuses: list = []
    sub = LightstreamerSubscriber(
        acc_type="DEMO",
        account_id="ACC123",
        cst="cst-token",
        xst="xst-token",
        on_update=lambda *a: updates.append(a),
        on_status=lambda new, prev: statuses.append((new, prev)),
        ls_client_factory=fake_client_factory,
    )
    status = sub.connect(timeout_sec=1.0)
    assert status == "CONNECTED:HTTP-STREAMING"
    fake = fake_client_factory.holder["c"]
    assert fake.connectionDetails.user == "ACC123"
    assert fake.connectionDetails.password == "CST-cst-token|XST-xst-token"


def test_connect_times_out_when_status_stuck(fake_client_factory) -> None:
    def factory(endpoint: str, adapter_set: str = "DEFAULT") -> FakeLightstreamerClient:
        c = FakeLightstreamerClient(endpoint, adapter_set)
        # Override connect to leave status DISCONNECTED
        c.connect = lambda: None  # type: ignore[assignment]
        fake_client_factory.holder["c"] = c
        return c

    sub = LightstreamerSubscriber(
        acc_type="DEMO",
        account_id="ACC123",
        cst="x", xst="y",
        on_update=lambda *a: None,
        on_status=lambda *a: None,
        ls_client_factory=factory,
    )
    with pytest.raises(TimeoutError):
        sub.connect(timeout_sec=0.3)


def test_subscribe_pair_uses_locked_adapter_and_fields(fake_client_factory) -> None:
    sub = LightstreamerSubscriber(
        acc_type="DEMO",
        account_id="ACC", cst="c", xst="x",
        on_update=lambda *a: None,
        on_status=lambda *a: None,
        ls_client_factory=fake_client_factory,
    )
    sub.connect(timeout_sec=1.0)
    sub.subscribe_pair(SubscriptionSpec(pair="GBPUSD", epic="CS.D.GBPUSD.TODAY.IP"))
    fake = fake_client_factory.holder["c"]
    assert len(fake.subscriptions) == 1
    # Real LS Subscription object — query via SDK accessors. NB: this
    # test depends on lightstreamer-client-lib==1.0.3 (pinned in
    # pyproject.toml) exposing getMode/getItems/getFields/getListeners.
    # If we ever bump the SDK and the accessor API changes, this is
    # where it shows up. (L2, Phase 7 follow-up.)
    s = fake.subscriptions[0]
    assert s.getMode() == LIGHTSTREAMER_CANDLE_MODE
    assert list(s.getItems()) == [
        LIGHTSTREAMER_CANDLE_ITEM_TEMPLATE.format(epic="CS.D.GBPUSD.TODAY.IP")
    ]
    assert list(s.getFields()) == list(LIGHTSTREAMER_CANDLE_FIELDS)


def test_item_update_routed_to_on_update_with_parsed_candle(fake_client_factory) -> None:
    received: list = []
    sub = LightstreamerSubscriber(
        acc_type="DEMO",
        account_id="ACC", cst="c", xst="x",
        on_update=lambda pair, candle, close, payload: received.append(
            (pair, candle, close)
        ),
        on_status=lambda *a: None,
        ls_client_factory=fake_client_factory,
    )
    sub.connect(timeout_sec=1.0)
    sub.subscribe_pair(SubscriptionSpec(pair="GBPUSD", epic="CS.D.GBPUSD.TODAY.IP"))
    fake = fake_client_factory.holder["c"]
    listener = fake.subscriptions[0].getListeners()[0]
    payload = {
        "UTM": "1778837400000",
        "LTV": "100",
        "CONS_TICK_COUNT": "100",
        "CONS_END": "1",
        "BID_OPEN": "1.30", "BID_HIGH": "1.302", "BID_LOW": "1.299", "BID_CLOSE": "1.301",
        "OFR_OPEN": "1.3002", "OFR_HIGH": "1.3022", "OFR_LOW": "1.2992", "OFR_CLOSE": "1.3012",
    }
    listener.onItemUpdate(FakeItemUpdate(payload))
    assert len(received) == 1
    pair, candle, close = received[0]
    assert pair == "GBPUSD"
    assert close is True


def test_status_change_routed_to_on_status(fake_client_factory) -> None:
    statuses: list = []
    sub = LightstreamerSubscriber(
        acc_type="DEMO",
        account_id="ACC", cst="c", xst="x",
        on_update=lambda *a: None,
        on_status=lambda new, prev: statuses.append((new, prev)),
        ls_client_factory=fake_client_factory,
    )
    sub.connect(timeout_sec=1.0)
    fake = fake_client_factory.holder["c"]
    client_listener = fake.listeners[0]
    # FakeClient.connect() already fired one CONNECTED:HTTP-STREAMING
    # transition via the listener — see the on_status callback's first
    # entry. Push two more transitions.
    client_listener.onStatusChange("DISCONNECTED")
    client_listener.onStatusChange("CONNECTED:HTTP-STREAMING")
    assert statuses == [
        ("CONNECTED:HTTP-STREAMING", None),
        ("DISCONNECTED", "CONNECTED:HTTP-STREAMING"),
        ("CONNECTED:HTTP-STREAMING", "DISCONNECTED"),
    ]


def test_disconnect_cleans_up(fake_client_factory) -> None:
    sub = LightstreamerSubscriber(
        acc_type="DEMO",
        account_id="ACC", cst="c", xst="x",
        on_update=lambda *a: None,
        on_status=lambda *a: None,
        ls_client_factory=fake_client_factory,
    )
    sub.connect(timeout_sec=1.0)
    sub.subscribe_pair(SubscriptionSpec(pair="GBPUSD", epic="CS.D.GBPUSD.TODAY.IP"))
    sub.disconnect()
    fake = fake_client_factory.holder["c"]
    assert fake.disconnect_calls == 1
    assert len(fake.unsubscribed) == 1


def test_bad_acc_type_rejected() -> None:
    with pytest.raises(ValueError):
        LightstreamerSubscriber(
            acc_type="GARBAGE", account_id="ACC", cst="c", xst="x",
            on_update=lambda *a: None,
        )
