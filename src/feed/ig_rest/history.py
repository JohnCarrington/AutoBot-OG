"""Historical price fetch (stub for Phase 7).

Phase 6 does not consume historical bars — the bot reads enriched
candles from the M5/H1 archive that Phase 7 builds. This module
exists so the IG REST surface is complete in :py:mod:`feed.ig_rest`:
Phase 7's candle archive backfill helpers will call
:func:`fetch_historical_prices` to populate gaps.

The function returns the raw IG payload unchanged — Phase 7 will
define the higher-level ``Candle`` dataclass and the
DataFrame-assembly helpers. Keeping this minimal here avoids
designing a contract before the consumer exists.
"""
from __future__ import annotations

from typing import Any

from .auth import IGSession


def fetch_historical_prices(
    session: IGSession,
    epic: str,
    *,
    resolution: str,
    num_points: int,
) -> dict[str, Any]:
    """Return the raw historical-prices payload for ``epic``.

    ``resolution`` accepts IG values such as ``"MINUTE_5"``, ``"HOUR"``,
    ``"DAY"``. ``num_points`` is the number of bars looking back from
    "now". The library normalises the response into a dict with a
    ``prices`` list — we pass it through verbatim for Phase 7 to
    parse.
    """
    raw = session.service.fetch_historical_prices_by_epic_and_num_points(
        epic=epic,
        resolution=resolution,
        numpoints=num_points,
    )
    return raw if isinstance(raw, dict) else {"prices": []}


__all__ = ["fetch_historical_prices"]
