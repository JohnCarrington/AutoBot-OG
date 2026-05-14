"""Tests for feed.ig_rest.client.IGClient.

Tests inject a fake :py:class:`AllowanceTracker` and a fake
``IGSession`` so allowance gating + error escalation are exercised
without a real broker.
"""
from __future__ import annotations

from typing import Any

import pytest

from feed.ig_rest.allowance import AllowanceTracker
from feed.ig_rest.auth import IGSession
from feed.ig_rest.client import AllowanceExceeded, IGClient
from feed.ig_rest.types import AmendRequest, CloseRequest, OrderRequest


class _StubService:
    """Minimal IGService stand-in that returns canned dicts."""

    ACC_NUMBER = None

    def __init__(self) -> None:
        self.exceptions: dict[str, BaseException] = {}
        self.returns: dict[str, Any] = {
            "create_open_position": {
                "dealReference": "REF",
                "dealId": "D1",
                "status": "OPEN",
                "dealStatus": "ACCEPTED",
                "level": 1.30,
            },
            "update_open_position": {
                "dealReference": "REF",
                "dealId": "D1",
                "status": "AMENDED",
                "dealStatus": "ACCEPTED",
            },
            "close_open_position": {
                "dealReference": "REF",
                "dealId": "D1",
                "status": "CLOSED",
                "dealStatus": "ACCEPTED",
            },
            "fetch_open_positions": {"positions": []},
            "fetch_open_position_by_deal_id": None,
            "fetch_deal_by_deal_reference": {
                "dealReference": "REF",
                "dealId": "D1",
                "status": "OPEN",
                "dealStatus": "ACCEPTED",
            },
        }

    def _check(self, name: str) -> None:
        exc = self.exceptions.get(name)
        if exc is not None:
            raise exc

    def create_open_position(self, **_kwargs):
        self._check("create_open_position")
        return self.returns["create_open_position"]

    def update_open_position(self, **_kwargs):
        self._check("update_open_position")
        return self.returns["update_open_position"]

    def close_open_position(self, **_kwargs):
        self._check("close_open_position")
        return self.returns["close_open_position"]

    def fetch_open_positions(self):
        self._check("fetch_open_positions")
        return self.returns["fetch_open_positions"]

    def fetch_open_position_by_deal_id(self, deal_id):  # noqa: ARG002
        self._check("fetch_open_position_by_deal_id")
        return self.returns["fetch_open_position_by_deal_id"]

    def fetch_deal_by_deal_reference(self, deal_reference):  # noqa: ARG002
        self._check("fetch_deal_by_deal_reference")
        return self.returns["fetch_deal_by_deal_reference"]


def _client(*, rpm: int = 5) -> tuple[IGClient, _StubService, list[float], AllowanceTracker]:
    """Build an IGClient driven by an injectable clock."""
    clock_state = [0.0]
    tracker = AllowanceTracker(
        requests_per_minute=rpm,
        window_seconds=60,
        backoff_schedule=(10, 20),
        clock=lambda: clock_state[0],
    )
    svc = _StubService()
    session = IGSession(service=svc, account_id=None, acc_type="DEMO")  # type: ignore[arg-type]
    return IGClient(session, allowance=tracker), svc, clock_state, tracker


# --- Pass-through dispatch -------------------------------------------------


def test_open_position_proxies_to_session() -> None:
    client, svc, _, _ = _client()
    result = client.open_position(
        OrderRequest(
            epic="X",
            direction="BUY",
            size=1.0,
            stop_level=1.29,
        )
    )
    assert result.deal_id == "D1"


def test_amend_position_proxies_to_session() -> None:
    client, _, _, _ = _client()
    result = client.amend_position(
        AmendRequest(deal_id="D1", stop_level=1.30010)
    )
    assert result.status == "ACCEPTED"


def test_close_position_proxies_to_session() -> None:
    client, _, _, _ = _client()
    result = client.close_position(
        CloseRequest(
            deal_id="D1",
            epic="X",
            position_direction="BUY",
            size=1.0,
        )
    )
    assert result.status == "ACCEPTED"


def test_fetch_open_positions_empty() -> None:
    client, _, _, _ = _client()
    assert client.fetch_open_positions() == []


# --- Allowance gating ------------------------------------------------------


def test_allowance_exceeded_raises_before_calling_session() -> None:
    client, svc, clock, _ = _client(rpm=2)
    # Burn the allowance.
    client.fetch_open_positions()
    client.fetch_open_positions()
    # Third call should raise immediately without touching the service.
    pre_calls = svc.exceptions.get("fetch_open_positions") is None
    # Set sentinel: if a third call sneaks through, the service raises.
    svc.exceptions["fetch_open_positions"] = RuntimeError(
        "service should not be touched while allowance is exhausted"
    )
    with pytest.raises(AllowanceExceeded) as exc_info:
        client.fetch_open_positions()
    assert exc_info.value.recommended_sleep_seconds > 0
    assert pre_calls is True  # sanity


def test_allowance_clears_after_window_expires() -> None:
    client, _, clock, _ = _client(rpm=2)
    client.fetch_open_positions()
    client.fetch_open_positions()
    clock[0] = 61.0
    client.fetch_open_positions()  # no raise


# --- Throttle escalation on allowance error -------------------------------


def test_throttle_recorded_on_allowance_error_message() -> None:
    client, svc, _, tracker = _client(rpm=10)
    svc.exceptions["fetch_open_positions"] = RuntimeError(
        "error.public-api.exceeded-api-key-allowance"
    )
    with pytest.raises(RuntimeError):
        client.fetch_open_positions()
    snap = tracker.snapshot()
    assert snap.throttle_count == 1


def test_throttle_not_recorded_on_generic_error() -> None:
    client, svc, _, tracker = _client(rpm=10)
    svc.exceptions["fetch_open_positions"] = RuntimeError("network unreachable")
    with pytest.raises(RuntimeError):
        client.fetch_open_positions()
    snap = tracker.snapshot()
    assert snap.throttle_count == 0


# --- Session + allowance exposed as properties ----------------------------


def test_session_and_allowance_accessible_for_diagnostics() -> None:
    client, svc, _, tracker = _client()
    assert client.session.service is svc
    assert client.allowance is tracker
