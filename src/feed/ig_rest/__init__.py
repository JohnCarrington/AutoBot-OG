"""IG REST client surface (Phase 6).

Sole public surface for talking to IG's REST API. The execution layer
(:py:mod:`execution`) depends on :py:class:`IGClient` and the request /
result dataclasses below; it never imports ``trading_ig`` directly,
so the dependency can be replaced or mocked at a single seam.

Public surface
--------------
- :py:class:`IGClient` — composed REST client with allowance tracking.
- :py:class:`IGSession`, :py:class:`IGCredentials` — auth bundle and env helpers.
- :py:exc:`AllowanceExceeded` — raised when a call would breach the limit.
- :py:class:`AllowanceTracker` — pure trailing-window counter.
- Request types: :py:class:`OrderRequest`, :py:class:`AmendRequest`,
  :py:class:`CloseRequest`.
- Result types: :py:class:`BrokerPosition`, :py:class:`DealConfirmation`,
  :py:class:`MarketInfo`.

Sub-modules
-----------
- :py:mod:`feed.ig_rest.auth` — env loader + ``IGService`` factory.
- :py:mod:`feed.ig_rest.client` — :py:class:`IGClient`.
- :py:mod:`feed.ig_rest.positions` — open / amend / close / read primitives.
- :py:mod:`feed.ig_rest.markets` — market-info lookup (Phase 7 consumer).
- :py:mod:`feed.ig_rest.history` — historical-prices fetch (Phase 7).
- :py:mod:`feed.ig_rest.allowance` — pure allowance tracker.
"""
from .allowance import AllowanceSnapshot, AllowanceTracker
from .auth import IGCredentials, IGSession, create_ig_service, load_ig_credentials
from .client import AllowanceExceeded, IGClient
from .types import (
    AmendRequest,
    BrokerPosition,
    CloseRequest,
    DealConfirmation,
    MarketInfo,
    OrderRequest,
)

__all__ = [
    "AllowanceExceeded",
    "AllowanceSnapshot",
    "AllowanceTracker",
    "AmendRequest",
    "BrokerPosition",
    "CloseRequest",
    "DealConfirmation",
    "IGClient",
    "IGCredentials",
    "IGSession",
    "MarketInfo",
    "OrderRequest",
    "create_ig_service",
    "load_ig_credentials",
]
