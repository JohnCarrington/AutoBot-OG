"""Typed payloads exchanged with the IG REST API (Phase 6).

trading_ig 0.0.16 returns pandas DataFrames by default (when its
``return_dataframe=True`` constructor flag is set) and bare dicts
otherwise. These dataclasses sit at the boundary: every public method
on :py:class:`feed.ig_rest.client.IGClient` accepts and returns one of
the types below, never a raw DataFrame or library-shaped dict. The
shim's job is to normalise away the library's representation choices.

All time fields are timezone-aware ``datetime`` instances in UTC. All
prices are in instrument units (broker quotes — e.g. ``1.30050`` for
GBPUSD). All distances / sizes are floats; the caller decides pip
conversion via :func:`config.pair_config.price_to_pips`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderRequest:
    """A market / limit order to open a new position.

    Maps to ``IGService.create_open_position``. Field defaults reflect
    the v1 spreadbet convention: market orders, no guaranteed stop, no
    trailing stop, force_open=True (don't aggregate into an existing
    position). Override per-call if a strategy needs different
    semantics.
    """

    epic: str
    direction: Literal["BUY", "SELL"]
    size: float
    stop_level: float
    limit_level: Optional[float] = None
    currency_code: str = "GBP"
    expiry: str = "-"
    order_type: Literal["MARKET", "LIMIT", "QUOTE"] = "MARKET"
    force_open: bool = True
    guaranteed_stop: bool = False
    trailing_stop: bool = False
    level: Optional[float] = None
    quote_id: Optional[str] = None


@dataclass(frozen=True)
class AmendRequest:
    """An update to an existing position's stop / limit levels.

    Maps to ``IGService.update_open_position``. Either or both of
    ``stop_level`` / ``limit_level`` may be ``None`` to leave that side
    of the position unchanged — the IG API treats a missing field as
    "preserve current value".
    """

    deal_id: str
    stop_level: Optional[float]
    limit_level: Optional[float] = None


@dataclass(frozen=True)
class CloseRequest:
    """A request to close an existing position.

    Maps to ``IGService.close_open_position``. The IG semantics require
    the *opposite* direction to the position being closed, the
    position's epic, and its size. The wrapper computes the opposite
    direction so callers can supply the position's own direction.
    """

    deal_id: str
    epic: str
    position_direction: Literal["BUY", "SELL"]
    size: float
    order_type: Literal["MARKET", "LIMIT", "QUOTE"] = "MARKET"
    expiry: str = "-"
    level: Optional[float] = None
    quote_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DealConfirmation:
    """Outcome record from a create / amend / close call.

    Returned by the IG ``confirm`` endpoint. Status is the canonical
    success / failure flag for the entire round-trip (a successful HTTP
    response can still carry ``dealStatus == "REJECTED"``). The wrapper
    surfaces this directly — callers must inspect ``status`` before
    treating any fields as authoritative.
    """

    deal_reference: str
    deal_id: Optional[str]
    status: Literal["ACCEPTED", "REJECTED"]
    deal_status: Optional[str] = None
    reason: Optional[str] = None
    epic: Optional[str] = None
    direction: Optional[Literal["BUY", "SELL"]] = None
    size: Optional[float] = None
    level: Optional[float] = None
    stop_level: Optional[float] = None
    limit_level: Optional[float] = None
    date: Optional[datetime] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerPosition:
    """A position as IG currently reports it.

    Returned by ``IGClient.fetch_open_positions()`` /
    ``fetch_open_position_by_deal_id()``. The fields are the subset our
    reconciliation and SL-management layers consume; the raw payload is
    preserved in ``raw`` for diagnostics.
    """

    deal_id: str
    deal_reference: Optional[str]
    epic: str
    direction: Literal["BUY", "SELL"]
    size: float
    open_level: float
    stop_level: Optional[float]
    limit_level: Optional[float]
    created_date_utc: Optional[datetime]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketInfo:
    """Subset of IG's ``/markets/{epic}`` payload that we actually need."""

    epic: str
    instrument_name: str
    bid: float
    offer: float
    market_status: str
    update_time_utc: Optional[datetime]
    min_deal_size: Optional[float]
    min_stop_distance: Optional[float]
    raw: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "AmendRequest",
    "BrokerPosition",
    "CloseRequest",
    "DealConfirmation",
    "MarketInfo",
    "OrderRequest",
]
