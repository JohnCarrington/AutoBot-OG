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

from .impact import Impact, parse_impact


logger = logging.getLogger(__name__)


# Tiebreaker ranks for impact severity (review H3). Higher = preferred.
_IMPACT_RANK: dict[Impact, int] = {
    Impact.HIGH: 2,
    Impact.MEDIUM: 1,
    Impact.LOW: 0,
}


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


# ---------------------------------------------------------------------------
# Block-pair classifier (review H2 fix — bidirectional).
#
# ADP Employment Change and Non-Farm Payrolls share "employment"/"change"
# vocabulary but are different US labor releases that publish ~2 days
# apart. The legacy ``_BLOCK_PAIRS = {("adp", "non farm payrolls"), ...}``
# only blocked the ADP→NFP direction (request mentions "adp" AND candidate
# mentions "non farm payrolls"). The reverse (request title "Non-Farm
# Employment Change", candidate "ADP Employment Change") fell through —
# same shape of bug as the 2026-04-23 country incident.
#
# A literal symmetric set-of-tuples (the review's suggested fix) does
# NOT actually catch the reverse case either, because the request title
# in that direction ("Non-Farm Employment Change") contains no
# "payrolls" substring. So instead we classify each side independently
# into one of {"adp", "nfp", None} and block when the two sides land in
# DIFFERENT groups (in either direction).
# ---------------------------------------------------------------------------
_NFP_TOKENS: frozenset[str] = frozenset(
    {"non farm", "non-farm", "nonfarm", "payrolls"}
)


def _classify_adp_or_nfp(text_lower: str) -> Optional[str]:
    """Return ``"adp"``, ``"nfp"``, or ``None``.

    ``"adp"`` wins precedence — the ForexFactory request title for the
    ADP release is ``"ADP Non-Farm Employment Change"``, which contains
    BOTH vocabularies. Classifying it as ADP (the more specific token)
    is what stops ADP↔ADP matches from being blocked while still
    catching ADP↔NFP cross-matches.
    """
    if "adp" in text_lower:
        return "adp"
    if any(tok in text_lower for tok in _NFP_TOKENS):
        return "nfp"
    return None


def is_blocked_match(event_title: str, candidate_event: str) -> bool:
    """Return True if matching this candidate would be a known false-positive.

    Currently blocks ADP↔NFP cross-matches in either direction:
    - ADP request vs NFP candidate (the legacy case);
    - NFP request vs ADP candidate (review H2 — newly fixed).
    """
    t = _classify_adp_or_nfp(event_title.lower())
    c = _classify_adp_or_nfp(candidate_event.lower())
    if t is None or c is None:
        return False
    return t != c


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

    Tiebreaker (review H3 — deterministic across payload orderings):
    when two candidates score equally, prefer in order:
      1. matched against the original title (not via an alias);
      2. higher impact (HIGH > MEDIUM > LOW);
      3. alphabetically earlier country code.

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

    # Collect every candidate that scores > 0. Score per (alias_title, event)
    # pair; same event can appear multiple times across alias iterations —
    # the sort below picks the best entry overall.
    candidates: list[tuple[float, bool, dict[str, Any]]] = []
    titles = expand_title(event_title)
    for try_index, try_title in enumerate(titles):
        title_words = normalize(try_title)
        is_original = try_index == 0
        for ev in events:
            if ev.get("country") not in allowed:
                continue
            if is_blocked_match(event_title, ev.get("event", "")):
                continue
            score = fuzzy_score(title_words, ev.get("event", ""))
            if score > 0:
                candidates.append((score, is_original, ev))

    if not candidates:
        return None

    def _sort_key(c: tuple[float, bool, dict[str, Any]]) -> tuple[float, int, int, str]:
        score, is_original, ev = c
        return (
            -score,                                    # higher score first
            0 if is_original else 1,                   # original-title match first
            -_IMPACT_RANK[parse_impact(ev.get("impact"))],  # higher impact first
            str(ev.get("country") or "ZZ"),            # alphabetical (asc)
        )

    candidates.sort(key=_sort_key)
    best = candidates[0][2]

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
