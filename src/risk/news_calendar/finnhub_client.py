"""Finnhub economic-calendar HTTP client.

Reads ``FINNHUB_API_KEY`` from env. ``fetch_calendar`` returns today's
+ tomorrow's economic events filtered to traded-currency countries and
to non-``LOW`` impact (HIGH and MEDIUM only — see
``docs/v1_architecture.md`` §6.6).

The legacy AutoBot fetcher kept only ``impact == "high"``. This client
widens the filter to keep MEDIUM events too, because v1's risk layer
applies a *soft* block to medium-impact releases (skip new entries;
existing trades unchanged) and therefore needs them in the cache.

Authentication
--------------
The API token is sent via the ``X-Finnhub-Token`` request header rather
than as a URL query parameter. ``requests`` exception strings include
the failing URL verbatim — putting the token in the query string would
leak it into application logs on any network blip (review H1).

Failure semantics
-----------------
``fetch_calendar`` returns ``None`` on any failure (network, HTTP
non-200, JSON decode, Finnhub disabled). It returns ``list[dict]``
(possibly empty) only on confirmed success. Callers in ``calendar.py``
use this distinction to preserve the previous good cache on transient
failures rather than overwriting it with empty data (review H4).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from .impact import Impact, parse_impact


logger = logging.getLogger(__name__)


FINNHUB_API_KEY: str = os.getenv("FINNHUB_API_KEY", "")
FINNHUB_BASE_URL: str = "https://finnhub.io/api/v1"
FINNHUB_ENABLED: bool = bool(FINNHUB_API_KEY)


# Finnhub's /calendar/economic averages ~5s latency, with spikes >10s
# per their status page. The legacy 5s timeout was on the edge of normal
# latency. 15s gives meaningful headroom without blocking the strategy
# dispatch loop. Legacy env names are honoured.
FETCH_TIMEOUT: float = float(
    os.getenv(
        "FINNHUB_FETCH_TIMEOUT_SECS",
        os.getenv("TE_FETCH_TIMEOUT_SECS", os.getenv("TE_TIMEOUT_SECS", "15")),
    )
)


# Countries for the currencies the bot trades (now or in v2/v3). EUR is
# represented by the eurozone composite (EU) plus reporting member states.
# Override via FINNHUB_CALENDAR_COUNTRIES (comma-separated ISO 2-letter
# codes). The legacy env name TE_CALENDAR_COUNTRIES is also accepted.
_DEFAULT_TRADED_COUNTRIES: tuple[str, ...] = (
    "US", "GB",                          # USD, GBP
    "EU", "DE", "FR", "IT", "ES", "NL",  # EUR (composite + member states)
    "JP",                                # JPY
    "CA",                                # CAD
)
TRADED_COUNTRIES: frozenset[str] = frozenset(
    c.strip().upper()
    for c in (
        os.getenv("FINNHUB_CALENDAR_COUNTRIES")
        or os.getenv("TE_CALENDAR_COUNTRIES")
        or ",".join(_DEFAULT_TRADED_COUNTRIES)
    ).split(",")
    if c.strip()
)


def fetch_calendar() -> Optional[list[dict]]:
    """Fetch today + tomorrow from Finnhub's ``/calendar/economic`` endpoint.

    Returns raw event dicts filtered to:
    - ``country in TRADED_COUNTRIES``
    - ``impact != Impact.LOW``

    Failure handling (review H4): returns ``None`` on any error or when
    ``FINNHUB_ENABLED`` is False. Returns ``list[dict]`` (possibly empty)
    only on confirmed success. The empty-list case is "fetch worked, no
    matching events in the window"; the ``None`` case is "we don't know
    what the calendar contains right now". ``calendar.poll_for_actual``
    uses the distinction to preserve the previous good cache on
    transient failures.

    Never raises — callers can poll on a schedule without try/except.

    Authentication (review H1): the API token is sent via the
    ``X-Finnhub-Token`` header to keep it out of ``requests`` exception
    strings (which include the failing URL).
    """
    if not FINNHUB_ENABLED:
        return None

    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        url = (
            f"{FINNHUB_BASE_URL}/calendar/economic"
            f"?from={today}&to={tomorrow}"
        )
        resp = requests.get(
            url,
            headers={"X-Finnhub-Token": FINNHUB_API_KEY},
            timeout=FETCH_TIMEOUT,
        )
    except Exception as e:
        logger.warning("[NEWS-CAL] Finnhub fetch failed: %s", e)
        return None

    if resp.status_code != 200:
        logger.warning("[NEWS-CAL] Finnhub returned %d", resp.status_code)
        return None

    try:
        data = resp.json()
    except Exception as e:
        logger.warning("[NEWS-CAL] Finnhub JSON decode failed: %s", e)
        return None

    events = data.get("economicCalendar", []) or []

    kept: list[dict] = []
    for ev in events:
        country = ev.get("country")
        impact = parse_impact(ev.get("impact"))
        if country in TRADED_COUNTRIES and impact is not Impact.LOW:
            kept.append(ev)
        else:
            logger.debug(
                "[NEWS-CAL] filtered out %s from %s (impact=%s)",
                ev.get("event", "?"), country, impact.value,
            )
    return kept


__all__ = [
    "FINNHUB_API_KEY",
    "FINNHUB_BASE_URL",
    "FINNHUB_ENABLED",
    "FETCH_TIMEOUT",
    "TRADED_COUNTRIES",
    "fetch_calendar",
]
