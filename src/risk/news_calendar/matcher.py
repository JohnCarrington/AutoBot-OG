"""Event matching: aliases, fuzzy scoring, country gate.

This module contains the regression fix from legacy AutoBot commit
**03ac162** ("fix(te_calendar): country filter in _lookup_finnhub").
Events must match on BOTH event-name AND currency/country, not just
on event-name.

Background
----------
On 2026-04-23 at 08:30 UTC, a GBPUSD PREFLIGHT poll silently returned
DE's 07:30 PMI (``actual=51.2 estimate=51.3``) instead of GB's 08:30
PMI (``actual=53.6 estimate=49.9``). The matcher scored by event-name
only, and DE's actual was already published while GB's was still null,
so DE became the highest-scoring candidate. The strategy mis-classified
IN_LINE on the wrong country's values and never fired on a +7.4% GB BEAT.

Fix
---
``countries_for_currency`` returns the allowed Finnhub country set for
an ISO currency. ``match_event`` rejects mismatched candidates BEFORE
scoring, so a wrong-country event cannot compete for ``best_score``.
When no same-country match exists, ``match_event`` returns ``None`` and
the caller keeps polling for the correct release to land.

EUR explicitly covers the eurozone composite (``EU``) plus reporting
member states (``DE``, ``FR``, ``IT``, ``ES``, ``NL``); a EUR poll
legitimately matches a German PMI, but a GBP poll must not.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Currency → allowed Finnhub country codes (the 2026-04-23 regression fix).
# ---------------------------------------------------------------------------
_COUNTRIES_FOR_CURRENCY: dict[str, frozenset[str]] = {
    "USD": frozenset({"US"}),
    "GBP": frozenset({"GB"}),
    "EUR": frozenset({"EU", "DE", "FR", "IT", "ES", "NL"}),
    "JPY": frozenset({"JP"}),
    "CAD": frozenset({"CA"}),
}


def countries_for_currency(currency: Optional[str]) -> Optional[frozenset[str]]:
    """Return allowed Finnhub country codes for an ISO currency.

    Returns ``None`` if currency is missing or unrecognised — callers
    MUST treat ``None`` as a fail-closed signal (no match returned).
    """
    if not currency:
        return None
    return _COUNTRIES_FOR_CURRENCY.get(currency.strip().upper())


# ---------------------------------------------------------------------------
# ForexFactory → Finnhub event-name aliases.
# Calendars name the same release differently; the alias map lets a
# ForexFactory-shaped scheduled-event title still find its Finnhub entry.
# ---------------------------------------------------------------------------
_FF_TO_FINNHUB: dict[str, list[str]] = {
    "cpi y/y":                        ["inflation rate yoy"],
    "cpi m/m":                        ["inflation rate mom"],
    "core cpi m/m":                   ["core inflation rate mom"],
    "core cpi y/y":                   ["core inflation rate yoy"],
    "non-farm employment change":     ["non farm payrolls", "nonfarm payrolls"],
    "adp non-farm employment change": ["adp employment change"],
    "final gdp q/q":                  ["gdp growth rate", "gdp growth rate qoq"],
    "advance gdp q/q":                ["gdp growth rate", "gdp growth rate qoq adv"],
    "prelim gdp q/q":                 ["gdp growth rate", "gdp growth rate qoq 2nd"],
    "unemployment claims":            ["initial jobless claims"],
    "retail sales m/m":               ["retail sales mom"],
    "core retail sales m/m":          ["retail sales ex autos mom"],
    "ism manufacturing pmi":          ["ism manufacturing pmi"],
    "ism services pmi":               ["ism non manufacturing pmi", "ism services pmi"],
    "flash manufacturing pmi":        ["s&p global manufacturing pmi flash",
                                       "manufacturing pmi"],
    "flash services pmi":             ["s&p global services pmi flash",
                                       "services pmi"],
    "average hourly earnings m/m":    ["average hourly earnings mom"],
    "fomc statement":                 ["fed interest rate decision",
                                       "fomc meeting minutes"],
    "boe monetary policy summary":    ["boe interest rate decision"],
    "claimant count change":          ["claimant count change"],
}


# Pairs of (request-word, candidate-event-substring) that must NOT match
# despite token overlap. ADP and Non-Farm Payrolls share 60% of their
# tokens but are entirely different releases.
_BLOCK_PAIRS: set[tuple[str, str]] = {
    ("adp", "non farm payrolls"),
    ("adp", "nonfarm payrolls"),
}


def is_blocked_match(event_title: str, candidate_event: str) -> bool:
    """Return True if matching this candidate would be a known false-positive."""
    t_lower = event_title.lower()
    c_lower = candidate_event.lower()
    for block_word, block_event in _BLOCK_PAIRS:
        if block_word in t_lower and block_event in c_lower:
            return True
    return False


def expand_title(event_title: str) -> list[str]:
    """Return the original title plus all known Finnhub aliases for it."""
    key = event_title.lower().strip()
    aliases = _FF_TO_FINNHUB.get(key, [])
    return [event_title] + aliases


def normalize(text: str) -> set[str]:
    """Lowercase, strip punctuation, split on whitespace — return word set."""
    cleaned = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return set(cleaned.split())


def fuzzy_score(title_words: set[str], candidate: str) -> float:
    """Score how well a candidate event-name matches the request.

    Returns 0 for no match. Otherwise: ``common + pct`` where ``common``
    is the count of overlapping words and ``pct`` is the fraction of the
    shorter word-set matched. Requires:
    - at least 2 overlapping words
    - at least 50% of the shorter name matched

    The legacy gating was kept verbatim — a CPI release that shares only
    one word with the request shouldn't win against a closer match.
    """
    candidate_words = normalize(candidate)
    common = len(title_words & candidate_words)
    if common < 2:
        return 0.0
    shorter = min(len(title_words), len(candidate_words))
    if shorter == 0:
        return 0.0
    pct = common / shorter
    if pct < 0.5:
        return 0.0
    return common + pct


def match_event(
    event_title: str,
    currency: str,
    events: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Find the best-scoring Finnhub event for ``(event_title, currency)``.

    Country gate applies BEFORE scoring — wrong-country events cannot
    compete for ``best_score``. This is the 2026-04-23 regression fix.

    Returns ``None`` when:
    - ``event_title`` is empty;
    - ``currency`` is missing or unrecognised;
    - no same-country candidate scores > 0;
    - the matched event has ``actual=None`` (release not yet published).
    """
    if not event_title:
        return None
    allowed = countries_for_currency(currency)
    if allowed is None:
        logger.warning(
            "[NEWS-CAL] match_event rejecting unknown/empty currency %r "
            "for event %r — returning None.",
            currency, event_title,
        )
        return None
    if not events:
        return None

    best: Optional[dict[str, Any]] = None
    best_score = 0.0
    for try_title in expand_title(event_title):
        title_words = normalize(try_title)
        for ev in events:
            if ev.get("country") not in allowed:
                continue
            if is_blocked_match(event_title, ev.get("event", "")):
                continue
            score = fuzzy_score(title_words, ev.get("event", ""))
            if score > best_score:
                best_score = score
                best = ev

    if best is None or best_score <= 0:
        return None
    if best.get("actual") is None:
        return None
    return best


__all__ = [
    "countries_for_currency",
    "is_blocked_match",
    "expand_title",
    "normalize",
    "fuzzy_score",
    "match_event",
]
