"""Market metadata lookup helpers.

Wraps ``IGService.fetch_market_by_epic`` (and search variants) and
returns a :py:class:`MarketInfo` dataclass. Phase 6 does not consume
this — it is defined to round out the IG REST surface and unblock
Phase 7 (alerts that include current spread / market status).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from .auth import IGSession
from .types import MarketInfo


def fetch_market_info(session: IGSession, epic: str) -> MarketInfo:
    """Return current bid / offer / market status for ``epic``."""
    raw = session.service.fetch_market_by_epic(epic)
    if not isinstance(raw, dict):
        raise ValueError(
            f"unexpected market-info payload type: {type(raw).__name__}"
        )
    instrument = raw.get("instrument") or {}
    snapshot = raw.get("snapshot") or {}
    dealing = raw.get("dealingRules") or {}

    return MarketInfo(
        epic=str(instrument.get("epic") or epic).strip(),
        instrument_name=str(
            instrument.get("name") or instrument.get("displayName") or epic
        ),
        bid=_to_float(snapshot.get("bid")),
        offer=_to_float(snapshot.get("offer")),
        market_status=str(snapshot.get("marketStatus") or "UNKNOWN"),
        update_time_utc=_parse_ig_datetime(
            snapshot.get("updateTime") or snapshot.get("updateTimeUTC")
        ),
        min_deal_size=_extract_dealing_value(dealing.get("minDealSize")),
        min_stop_distance=_extract_dealing_value(
            dealing.get("minNormalStopOrLimitDistance")
        ),
        raw=raw,
    )


def _extract_dealing_value(rule: Any) -> Optional[float]:
    """IG dealing rules are nested ``{"unit": "...", "value": float}``."""
    if rule is None:
        return None
    if isinstance(rule, dict):
        try:
            return float(rule.get("value")) if rule.get("value") is not None else None
        except (TypeError, ValueError):
            return None
    try:
        return float(rule)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _parse_ig_datetime(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    text = str(value).strip()
    for cand in (text, text.replace("Z", "+00:00"), text.replace("/", "-")):
        try:
            dt = datetime.fromisoformat(cand)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


__all__ = ["fetch_market_info"]
