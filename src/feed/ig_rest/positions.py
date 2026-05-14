"""Thin wrappers around ``IGService`` position methods.

Each function takes an :py:class:`IGSession` plus a request dataclass
and returns one of the result dataclasses from
:py:mod:`feed.ig_rest.types`. The library is called with
``return_dataframe=False`` (set in :func:`feed.ig_rest.auth.create_ig_service`),
so every response we parse is a plain dict — no pandas in the IG client
surface.

The wrappers do not retry, do not back off, do not catch exceptions —
those concerns live in :py:class:`feed.ig_rest.client.IGClient`, which
composes :py:class:`feed.ig_rest.allowance.AllowanceTracker` around
these primitives.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from .auth import IGSession
from .types import (
    AmendRequest,
    BrokerPosition,
    CloseRequest,
    DealConfirmation,
    OrderRequest,
)


# ---------------------------------------------------------------------------
# Open / amend / close
# ---------------------------------------------------------------------------


def open_position(
    session: IGSession, order: OrderRequest
) -> DealConfirmation:
    """Submit a market / limit order, return the deal confirmation."""
    raw = session.service.create_open_position(
        currency_code=order.currency_code,
        direction=order.direction,
        epic=order.epic,
        expiry=order.expiry,
        force_open=order.force_open,
        guaranteed_stop=order.guaranteed_stop,
        level=order.level,
        limit_distance=None,
        limit_level=order.limit_level,
        order_type=order.order_type,
        quote_id=order.quote_id,
        size=order.size,
        stop_distance=None,
        stop_level=order.stop_level,
        trailing_stop=order.trailing_stop,
        trailing_stop_increment=None,
    )
    return _parse_deal_confirmation(raw)


def amend_position(
    session: IGSession, amend: AmendRequest
) -> DealConfirmation:
    """Update stop / limit levels on an existing position."""
    raw = session.service.update_open_position(
        limit_level=amend.limit_level,
        stop_level=amend.stop_level,
        deal_id=amend.deal_id,
        guaranteed_stop=False,
        trailing_stop=False,
        trailing_stop_distance=None,
        trailing_stop_increment=None,
    )
    return _parse_deal_confirmation(raw)


def close_position(
    session: IGSession, close: CloseRequest
) -> DealConfirmation:
    """Close an open position.

    IG semantics: pass the *opposite* direction to the position's own
    direction. This wrapper computes the opposite so callers can supply
    the position's direction directly.
    """
    opposite = "SELL" if close.position_direction == "BUY" else "BUY"
    raw = session.service.close_open_position(
        deal_id=close.deal_id,
        direction=opposite,
        epic=close.epic,
        expiry=close.expiry,
        level=close.level,
        order_type=close.order_type,
        quote_id=close.quote_id,
        size=close.size,
    )
    return _parse_deal_confirmation(raw)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def fetch_open_positions(session: IGSession) -> list[BrokerPosition]:
    """Return every position IG reports as currently open for the account."""
    raw = session.service.fetch_open_positions()
    positions = _extract_positions_list(raw)
    return [_parse_position(p) for p in positions]


def fetch_open_position_by_deal_id(
    session: IGSession, deal_id: str
) -> Optional[BrokerPosition]:
    """Return a single position by deal id, or ``None`` if not found."""
    try:
        raw = session.service.fetch_open_position_by_deal_id(deal_id)
    except Exception:
        # The library raises on a 404. Reconciliation treats "not
        # found" as a normal-path outcome (position closed manually
        # via web UI, or deal_id transcription error).
        return None
    if raw is None:
        return None
    if isinstance(raw, dict) and "positions" in raw:
        positions = raw.get("positions") or []
        if not positions:
            return None
        return _parse_position(positions[0])
    if isinstance(raw, dict):
        return _parse_position(raw)
    return None


def fetch_deal_confirmation(
    session: IGSession, deal_reference: str
) -> DealConfirmation:
    """Look up the confirmation record for a previously-submitted deal."""
    raw = session.service.fetch_deal_by_deal_reference(deal_reference)
    return _parse_deal_confirmation(raw)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _extract_positions_list(raw: Any) -> list[dict]:
    """Normalise the library's response shape into ``list[dict]``."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [p for p in raw if isinstance(p, dict)]
    if isinstance(raw, dict):
        return [p for p in (raw.get("positions") or []) if isinstance(p, dict)]
    return []


def _parse_position(item: dict) -> BrokerPosition:
    """Build a :py:class:`BrokerPosition` from a single IG dict.

    IG returns ``{"position": {...}, "market": {...}}`` from
    ``/positions``. ``fetch_open_position_by_deal_id`` returns the same
    shape. The function accepts either the wrapped form or a flat
    dict for ease of testing.
    """
    pos = item.get("position", item) if isinstance(item, dict) else {}
    mkt = item.get("market", {}) if isinstance(item, dict) else {}

    deal_id = str(pos.get("dealId") or "").strip()
    deal_ref = pos.get("dealReference")
    direction = str(pos.get("direction") or "").upper()
    if direction not in ("BUY", "SELL"):
        raise ValueError(f"unexpected position direction: {direction!r}")
    epic = str(mkt.get("epic") or pos.get("epic") or "").strip()
    if not epic:
        raise ValueError("position has no epic")

    return BrokerPosition(
        deal_id=deal_id,
        deal_reference=str(deal_ref) if deal_ref is not None else None,
        epic=epic,
        direction=direction,  # type: ignore[arg-type]
        size=_to_float(
            pos.get("dealSize") or pos.get("size") or pos.get("contractSize"),
            default=0.0,
        ),
        open_level=_to_float(pos.get("openLevel") or pos.get("level")),
        stop_level=_to_float_or_none(pos.get("stopLevel")),
        limit_level=_to_float_or_none(pos.get("limitLevel")),
        created_date_utc=_parse_ig_datetime(
            pos.get("createdDateUTC") or pos.get("createdDate")
        ),
        raw=item if isinstance(item, dict) else {},
    )


def _parse_deal_confirmation(raw: Any) -> DealConfirmation:
    """Build a :py:class:`DealConfirmation` from IG's ``/confirms`` payload."""
    if raw is None or not isinstance(raw, dict):
        raise ValueError(
            f"unexpected deal-confirmation payload type: {type(raw).__name__}"
        )

    deal_ref = str(raw.get("dealReference") or "").strip()
    deal_id_raw = raw.get("dealId")
    deal_id = (
        str(deal_id_raw).strip()
        if deal_id_raw is not None and str(deal_id_raw).strip()
        else None
    )

    raw_status = str(raw.get("status") or "").strip().upper()
    # IG sometimes returns "OPEN" / "UPDATED" / "AMENDED" for accepted
    # operations; "REJECTED" is the unambiguous failure.
    if raw_status == "REJECTED":
        status: Any = "REJECTED"
    else:
        status = "ACCEPTED"

    raw_direction = raw.get("direction")
    direction = (
        str(raw_direction).upper()
        if isinstance(raw_direction, str) and raw_direction.upper() in ("BUY", "SELL")
        else None
    )

    return DealConfirmation(
        deal_reference=deal_ref,
        deal_id=deal_id,
        status=status,
        deal_status=str(raw.get("dealStatus")) if raw.get("dealStatus") else None,
        reason=str(raw.get("reason")) if raw.get("reason") else None,
        epic=str(raw.get("epic")) if raw.get("epic") else None,
        direction=direction,  # type: ignore[arg-type]
        size=_to_float_or_none(raw.get("size")),
        level=_to_float_or_none(raw.get("level")),
        stop_level=_to_float_or_none(raw.get("stopLevel")),
        limit_level=_to_float_or_none(raw.get("limitLevel")),
        date=_parse_ig_datetime(raw.get("date")),
        raw=raw,
    )


def _parse_ig_datetime(value: Any) -> Optional[datetime]:
    """Parse IG's ``createdDateUTC`` / ``date`` strings into tz-aware UTC."""
    if value is None or value == "":
        return None
    text = str(value).strip()
    # Common shapes IG returns: '2026-05-14T17:30:00.123Z',
    # '2026-05-14T17:30:00', '2026/05/14 17:30:00'.
    candidates = (
        text,
        text.replace("Z", "+00:00"),
        text.replace("/", "-"),
    )
    for cand in candidates:
        try:
            dt = datetime.fromisoformat(cand)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # Fallback for "%Y-%m-%d %H:%M:%S.%f" without timezone.
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _to_float(value: Any, *, default: float = 0.0) -> float:
    """Convert ``value`` to float; return ``default`` on failure."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_float_or_none(value: Any) -> Optional[float]:
    """Convert ``value`` to float; return ``None`` on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "amend_position",
    "close_position",
    "fetch_deal_confirmation",
    "fetch_open_position_by_deal_id",
    "fetch_open_positions",
    "open_position",
]
