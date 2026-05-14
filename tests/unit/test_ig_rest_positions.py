"""Tests for feed.ig_rest.positions — parsing + wrapper dispatch."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from feed.ig_rest.auth import IGSession
from feed.ig_rest.positions import (
    _extract_positions_list,
    _parse_deal_confirmation,
    _parse_position,
    amend_position,
    close_position,
    fetch_deal_confirmation,
    fetch_open_position_by_deal_id,
    fetch_open_positions,
    open_position,
)
from feed.ig_rest.types import (
    AmendRequest,
    CloseRequest,
    OrderRequest,
)


# ---------------------------------------------------------------------------
# Fake IGService
# ---------------------------------------------------------------------------


class _FakeService:
    """Records calls and returns canned payloads. Mirrors IGService surface."""

    ACC_NUMBER = None

    def __init__(self) -> None:
        self.open_calls: list[dict] = []
        self.amend_calls: list[dict] = []
        self.close_calls: list[dict] = []
        self.fetch_open_calls: int = 0
        self.fetch_by_id_calls: list[str] = []
        self.confirm_calls: list[str] = []
        self.open_returns: object = None
        self.amend_returns: object = None
        self.close_returns: object = None
        self.fetch_open_returns: object = None
        self.fetch_by_id_returns: object = None
        self.fetch_by_id_raises: BaseException | None = None
        self.confirm_returns: object = None

    def create_open_position(self, **kwargs) -> object:
        self.open_calls.append(kwargs)
        return self.open_returns

    def update_open_position(self, **kwargs) -> object:
        self.amend_calls.append(kwargs)
        return self.amend_returns

    def close_open_position(self, **kwargs) -> object:
        self.close_calls.append(kwargs)
        return self.close_returns

    def fetch_open_positions(self) -> object:
        self.fetch_open_calls += 1
        return self.fetch_open_returns

    def fetch_open_position_by_deal_id(self, deal_id: str) -> object:
        self.fetch_by_id_calls.append(deal_id)
        if self.fetch_by_id_raises is not None:
            raise self.fetch_by_id_raises
        return self.fetch_by_id_returns

    def fetch_deal_by_deal_reference(self, deal_reference: str) -> object:
        self.confirm_calls.append(deal_reference)
        return self.confirm_returns


def _session(service: _FakeService) -> IGSession:
    return IGSession(service=service, account_id=None, acc_type="DEMO")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def test_extract_positions_list_handles_dict_wrapper() -> None:
    raw = {"positions": [{"position": {"dealId": "D1"}}]}
    assert _extract_positions_list(raw) == [{"position": {"dealId": "D1"}}]


def test_extract_positions_list_handles_bare_list() -> None:
    raw = [{"position": {"dealId": "D1"}}]
    assert _extract_positions_list(raw) == raw


def test_extract_positions_list_returns_empty_for_none() -> None:
    assert _extract_positions_list(None) == []


def test_parse_position_from_wrapped_payload() -> None:
    raw = {
        "position": {
            "dealId": "DIAA001",
            "dealReference": "ABC",
            "direction": "BUY",
            "dealSize": 1.0,
            "openLevel": 1.30000,
            "stopLevel": 1.29850,
            "limitLevel": None,
            "createdDateUTC": "2026-05-14T12:00:00",
        },
        "market": {
            "epic": "CS.D.GBPUSD.TODAY.IP",
        },
    }
    pos = _parse_position(raw)
    assert pos.deal_id == "DIAA001"
    assert pos.deal_reference == "ABC"
    assert pos.epic == "CS.D.GBPUSD.TODAY.IP"
    assert pos.direction == "BUY"
    assert pos.size == 1.0
    assert pos.open_level == 1.30000
    assert pos.stop_level == 1.29850
    assert pos.created_date_utc == datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)


def test_parse_position_rejects_unknown_direction() -> None:
    raw = {"position": {"dealId": "D1", "direction": "SIDEWAYS"}, "market": {"epic": "X"}}
    with pytest.raises(ValueError, match="direction"):
        _parse_position(raw)


def test_parse_position_requires_epic() -> None:
    raw = {"position": {"dealId": "D1", "direction": "BUY"}, "market": {}}
    with pytest.raises(ValueError, match="epic"):
        _parse_position(raw)


# --- Deal confirmation parsing ---------------------------------------------


def test_parse_deal_confirmation_accepted() -> None:
    raw = {
        "dealReference": "REF1",
        "dealId": "DEAL1",
        "status": "OPEN",
        "dealStatus": "ACCEPTED",
        "epic": "CS.D.GBPUSD.TODAY.IP",
        "direction": "BUY",
        "size": 1.0,
        "level": 1.30000,
        "stopLevel": 1.29850,
        "limitLevel": None,
        "date": "2026-05-14T12:00:00Z",
    }
    confirm = _parse_deal_confirmation(raw)
    assert confirm.status == "ACCEPTED"
    assert confirm.deal_id == "DEAL1"
    assert confirm.epic == "CS.D.GBPUSD.TODAY.IP"
    assert confirm.level == 1.30000
    assert confirm.date == datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)


def test_parse_deal_confirmation_rejected_preserves_reason() -> None:
    raw = {
        "dealReference": "REF1",
        "status": "REJECTED",
        "reason": "MARKET_OFFLINE",
    }
    confirm = _parse_deal_confirmation(raw)
    assert confirm.status == "REJECTED"
    assert confirm.reason == "MARKET_OFFLINE"
    assert confirm.deal_id is None


def test_parse_deal_confirmation_raises_on_non_dict() -> None:
    with pytest.raises(ValueError):
        _parse_deal_confirmation("not-a-dict")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Wrapper dispatch
# ---------------------------------------------------------------------------


def test_open_position_forwards_full_args() -> None:
    svc = _FakeService()
    svc.open_returns = {
        "dealReference": "REF",
        "dealId": "D1",
        "status": "OPEN",
        "level": 1.30,
    }
    order = OrderRequest(
        epic="CS.D.GBPUSD.TODAY.IP",
        direction="BUY",
        size=1.0,
        stop_level=1.29850,
        limit_level=None,
    )
    result = open_position(_session(svc), order)
    assert result.status == "ACCEPTED"
    assert result.deal_id == "D1"
    call = svc.open_calls[0]
    assert call["direction"] == "BUY"
    assert call["stop_level"] == 1.29850
    assert call["force_open"] is True
    assert call["order_type"] == "MARKET"


def test_amend_position_forwards_stop_and_limit() -> None:
    svc = _FakeService()
    svc.amend_returns = {"dealReference": "REF", "dealId": "D1", "status": "AMENDED"}
    amend_position(
        _session(svc),
        AmendRequest(deal_id="D1", stop_level=1.30010, limit_level=None),
    )
    call = svc.amend_calls[0]
    assert call["deal_id"] == "D1"
    assert call["stop_level"] == 1.30010


def test_close_position_sends_opposite_direction() -> None:
    svc = _FakeService()
    svc.close_returns = {"dealReference": "REF", "dealId": "D1", "status": "CLOSED"}
    close_position(
        _session(svc),
        CloseRequest(
            deal_id="D1",
            epic="CS.D.GBPUSD.TODAY.IP",
            position_direction="BUY",
            size=1.0,
        ),
    )
    call = svc.close_calls[0]
    assert call["direction"] == "SELL"


def test_fetch_open_positions_parses_each_entry() -> None:
    svc = _FakeService()
    svc.fetch_open_returns = {
        "positions": [
            {"position": {"dealId": "A", "direction": "BUY", "openLevel": 1.30},
             "market": {"epic": "X"}},
            {"position": {"dealId": "B", "direction": "SELL", "openLevel": 1.31},
             "market": {"epic": "Y"}},
        ]
    }
    out = fetch_open_positions(_session(svc))
    assert [p.deal_id for p in out] == ["A", "B"]


def test_fetch_by_deal_id_returns_none_on_exception() -> None:
    svc = _FakeService()
    svc.fetch_by_id_raises = RuntimeError("404 not found")
    out = fetch_open_position_by_deal_id(_session(svc), "D1")
    assert out is None


def test_fetch_by_deal_id_unwraps_positions_envelope() -> None:
    svc = _FakeService()
    svc.fetch_by_id_returns = {
        "positions": [
            {"position": {"dealId": "D1", "direction": "BUY", "openLevel": 1.30},
             "market": {"epic": "X"}},
        ]
    }
    pos = fetch_open_position_by_deal_id(_session(svc), "D1")
    assert pos is not None
    assert pos.deal_id == "D1"


def test_fetch_deal_confirmation_passes_through() -> None:
    svc = _FakeService()
    svc.confirm_returns = {"dealReference": "REF", "dealId": "D1", "status": "OPEN"}
    confirm = fetch_deal_confirmation(_session(svc), "REF")
    assert confirm.status == "ACCEPTED"
    assert svc.confirm_calls == ["REF"]
