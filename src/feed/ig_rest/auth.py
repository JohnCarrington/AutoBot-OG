"""IG session authentication.

Ported (much trimmed) from the legacy
``/home/autobot/autobot-og-port-staging/ig_auth.py``. The legacy file
carried a lot of compatibility shims for older ``trading_ig`` versions
(0.0.10 → 0.0.16 had API differences in token-extraction surface, plus
a pandas-3 ``to_offset`` patch). v1 pins ``trading_ig==0.0.16``
exactly, so those shims are gone — this module is the slimmest possible
session wrapper around ``IGService``.

Environment variables consumed:

- ``IG_USERNAME``      — IG account username (required)
- ``IG_PASSWORD``      — IG account password (required)
- ``IG_API_KEY``       — IG REST API key (required)
- ``IG_ACC_TYPE``      — ``"LIVE"`` or ``"DEMO"`` (required; selects endpoint)
- ``IG_ACCOUNT_ID``    — sub-account id (optional; e.g. SPREADBET account)
- ``IG_PRODUCT_TYPE``  — ``"SPREADBET"`` / ``"CFD"`` (optional fallback)

Public API:

- :func:`load_ig_credentials` — read env + return an ``IGCredentials`` dataclass.
- :func:`create_ig_service` — construct an ``IGService`` and call ``create_session``.
- :class:`IGSession` — convenience wrapper bundling the session + account id.

All real network calls live in ``trading_ig.IGService`` — this module
just supplies and wraps it.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IGCredentials:
    """Container for the env-supplied IG REST credentials."""

    username: str
    password: str
    api_key: str
    acc_type: str  # "LIVE" | "DEMO"
    account_id: Optional[str] = None
    product_type: Optional[str] = None  # "SPREADBET" | "CFD" | None


def load_ig_credentials() -> IGCredentials:
    """Read IG credentials from environment variables.

    Raises ``RuntimeError`` if any required variable is missing or if
    ``IG_ACC_TYPE`` is not one of ``LIVE`` / ``DEMO``. The check
    intentionally runs at call time (not import time) so tests can
    construct an ``IGClient`` with an injected fake session without
    real credentials in the environment.
    """
    username = os.getenv("IG_USERNAME") or ""
    password = os.getenv("IG_PASSWORD") or ""
    api_key = os.getenv("IG_API_KEY") or ""
    acc_type = (os.getenv("IG_ACC_TYPE") or "").upper()
    account_id = (os.getenv("IG_ACCOUNT_ID") or "").strip() or None
    product_type_raw = (os.getenv("IG_PRODUCT_TYPE") or "").strip().upper() or None

    missing = [
        name
        for name, val in (
            ("IG_USERNAME", username),
            ("IG_PASSWORD", password),
            ("IG_API_KEY", api_key),
            ("IG_ACC_TYPE", acc_type),
        )
        if not val
    ]
    if missing:
        raise RuntimeError(
            f"Missing required IG environment variables: {', '.join(missing)}"
        )
    if acc_type not in ("LIVE", "DEMO"):
        raise RuntimeError(
            f"IG_ACC_TYPE must be 'LIVE' or 'DEMO', got {acc_type!r}"
        )
    if product_type_raw and product_type_raw not in ("SPREADBET", "CFD"):
        raise RuntimeError(
            f"IG_PRODUCT_TYPE must be 'SPREADBET' or 'CFD', got "
            f"{product_type_raw!r}"
        )
    return IGCredentials(
        username=username,
        password=password,
        api_key=api_key,
        acc_type=acc_type,
        account_id=account_id,
        product_type=product_type_raw,
    )


# ---------------------------------------------------------------------------
# Session bundle
# ---------------------------------------------------------------------------


class _IGServiceLike(Protocol):
    """Protocol matching the ``trading_ig.IGService`` surface we use."""

    ACC_NUMBER: Optional[str]

    def create_session(self, session=None, encryption=False, version: str = "2"): ...
    def switch_account(self, account_id: str, default_account): ...


@dataclass
class IGSession:
    """Authenticated IG session ready for REST calls.

    Carries the live :py:class:`trading_ig.IGService` instance plus the
    selected sub-account id. The constructor is intentionally bare —
    use :func:`create_ig_service` to build one from environment.
    """

    service: _IGServiceLike
    account_id: Optional[str]
    acc_type: str  # "LIVE" | "DEMO"


def create_ig_service(
    credentials: Optional[IGCredentials] = None,
    *,
    service_factory=None,
) -> IGSession:
    """Construct an authenticated :py:class:`IGSession`.

    Parameters
    ----------
    credentials : IGCredentials, optional
        Pre-loaded credentials. If omitted, loaded from environment via
        :func:`load_ig_credentials`.
    service_factory : callable, optional
        Test seam: a zero-arg-after-credentials callable that returns a
        ready-to-use ``_IGServiceLike``. Defaults to constructing a
        real :py:class:`trading_ig.IGService`. Tests inject a stub so
        no network call is made.

    Returns
    -------
    IGSession
        The bundled session + account_id.
    """
    creds = credentials or load_ig_credentials()
    if service_factory is None:
        from trading_ig import IGService  # local import — keeps imports cheap

        service = IGService(
            username=creds.username,
            password=creds.password,
            api_key=creds.api_key,
            acc_type=creds.acc_type.lower(),
            acc_number=creds.account_id,
            # We normalise the library's response payloads into our own
            # dataclasses (``BrokerPosition``, ``DealConfirmation``,
            # ``MarketInfo``) inside ``feed.ig_rest.positions``. Asking
            # the library for raw dicts is simpler than parsing its
            # DataFrame columns, and removes a pandas dependency from
            # the IG client surface.
            return_dataframe=False,
        )
    else:
        service = service_factory(creds)

    service.create_session()
    logger.info(
        "IG session created (acc_type=%s, account_id=%s)",
        creds.acc_type,
        creds.account_id or "<default>",
    )
    return IGSession(
        service=service,
        account_id=creds.account_id,
        acc_type=creds.acc_type,
    )


__all__ = [
    "IGCredentials",
    "IGSession",
    "create_ig_service",
    "load_ig_credentials",
]
