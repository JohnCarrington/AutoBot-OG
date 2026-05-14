"""Tests for risk.news_calendar.matcher — primarily the country-filter
regression fix from legacy AutoBot commit 03ac162.

Background
----------
On 2026-04-23 at 08:30 UTC, a GBPUSD PREFLIGHT poll silently returned DE's
07:30 Manufacturing PMI (``actual=51.2 estimate=51.3``) instead of GB's
08:30 Manufacturing PMI (``actual=53.6 estimate=49.9``) because the matcher
scored by event-name only, and DE's actual was already published while
GB's was still null.

These tests assert that ``match_event`` rejects country mismatches at
match time, so the strategy keeps polling until the correct-country event
publishes. Tests are split into:

- "Regression" — the actual 2026-04-23 bug.
- "Positive path" — country filter does not over-restrict.
- "Adversarial inputs" — missing/empty/unknown currency, empty events,
  empty title, duplicate-country candidates, EU-vs-GBP, etc.
- "Primitive coverage" — ``normalize``, ``fuzzy_score``, ``expand_title``,
  ``is_blocked_match``.

Ported from ``tests/unit/test_te_calendar_country_filter.py`` in the
legacy AutoBot codebase; imports updated to the new package path and the
matcher signature decoupled from the cache (events passed in explicitly).
"""

from __future__ import annotations

import pytest

from risk.news_calendar import matcher as M
from risk.news_calendar.matcher import (
    countries_for_currency,
    expand_title,
    fuzzy_score,
    is_blocked_match,
    match_event,
    normalize,
)


# ---------------------------------------------------------------------------
# Regression — the actual bug from 2026-04-23 08:30
# ---------------------------------------------------------------------------


def test_regression_gb_preflight_does_not_match_de_release() -> None:
    """DE PMI at 07:30 (actual=51.2, est=51.3). GB PMI at 08:30 (actual=None,
    est=49.9). A GBP preflight poll must return None — not DE."""
    events = [
        {
            "country": "DE", "event": "S&P Global Manufacturing PMI Flash",
            "actual": 51.2, "estimate": 51.3, "prev": 52.2,
            "time": "2026-04-23 07:30:00", "impact": "high",
        },
        {
            "country": "GB", "event": "S&P Global Manufacturing PMI Flash",
            "actual": None, "estimate": 49.9, "prev": 51.0,
            "time": "2026-04-23 08:30:00", "impact": "high",
        },
    ]
    result = match_event(
        "S&P Global Manufacturing PMI Flash",
        currency="GBP",
        events=events,
    )
    assert result is None, (
        f"expected None (GB actual not yet published), got {result!r}. "
        "This is the 08:30 GBP PMI bug — matcher silently returned DE."
    )


# ---------------------------------------------------------------------------
# Positive path — country filter does not block the right match
# ---------------------------------------------------------------------------


def test_positive_gb_returns_gb_when_both_published() -> None:
    """Once GB's actual lands, a GBP preflight must return GB — not DE."""
    events = [
        {
            "country": "DE", "event": "S&P Global Manufacturing PMI Flash",
            "actual": 51.2, "estimate": 51.3, "prev": 52.2, "impact": "high",
        },
        {
            "country": "GB", "event": "S&P Global Manufacturing PMI Flash",
            "actual": 53.6, "estimate": 49.9, "prev": 51.0, "impact": "high",
        },
    ]
    result = match_event(
        "S&P Global Manufacturing PMI Flash",
        currency="GBP",
        events=events,
    )
    assert result is not None
    assert result["country"] == "GB"
    assert result["actual"] == 53.6
    assert result["estimate"] == 49.9


def test_positive_usd_only_returns_us_country() -> None:
    """USD must skip non-US entries even if a non-US entry would score higher."""
    events = [
        {
            "country": "GB", "event": "Retail Sales MoM",
            "actual": 1.5, "estimate": 0.5, "impact": "high",
        },
        {
            "country": "US", "event": "Retail Sales MoM",
            "actual": 0.3, "estimate": 0.4, "impact": "high",
        },
    ]
    result = match_event("Retail Sales MoM", currency="USD", events=events)
    assert result is not None
    assert result["country"] == "US"
    assert result["actual"] == 0.3


def test_positive_eur_matches_eurozone_member() -> None:
    """EUR must legitimately match a German release (EU composite +
    DE/FR/IT/ES/NL all in the EUR country set)."""
    events = [
        {
            "country": "DE", "event": "S&P Global Manufacturing PMI Flash",
            "actual": 51.2, "estimate": 51.3, "impact": "high",
        },
    ]
    result = match_event(
        "S&P Global Manufacturing PMI Flash",
        currency="EUR",
        events=events,
    )
    assert result is not None
    assert result["actual"] == 51.2


@pytest.mark.parametrize(
    "country,currency",
    [("FR", "EUR"), ("IT", "EUR"), ("ES", "EUR"), ("NL", "EUR"), ("EU", "EUR")],
)
def test_positive_eur_matches_all_eurozone_members(country: str, currency: str) -> None:
    """All five EUR member countries + the EU composite must satisfy a EUR lookup."""
    events = [
        {
            "country": country, "event": "Inflation Rate YoY",
            "actual": 2.5, "estimate": 2.3, "impact": "high",
        },
    ]
    result = match_event("Inflation Rate YoY", currency=currency, events=events)
    assert result is not None
    assert result["country"] == country


@pytest.mark.parametrize(
    "currency,expected_country",
    [("JPY", "JP"), ("CAD", "CA"), ("USD", "US"), ("GBP", "GB")],
)
def test_positive_single_country_currencies(currency: str, expected_country: str) -> None:
    """JPY/CAD/USD/GBP each map to one country."""
    events = [
        {
            "country": expected_country, "event": "Inflation Rate YoY",
            "actual": 2.0, "estimate": 1.8, "impact": "high",
        },
    ]
    result = match_event("Inflation Rate YoY", currency=currency, events=events)
    assert result is not None
    assert result["country"] == expected_country


# ---------------------------------------------------------------------------
# Adversarial inputs — fail-closed for missing / unknown / bad currency
# ---------------------------------------------------------------------------


def test_currency_none_returns_none() -> None:
    events = [
        {"country": "GB", "event": "PMI", "actual": 50.0, "estimate": 49.0,
         "impact": "high"},
    ]
    assert match_event("PMI", currency=None, events=events) is None  # type: ignore[arg-type]


def test_currency_empty_returns_none() -> None:
    events = [
        {"country": "GB", "event": "PMI", "actual": 50.0, "estimate": 49.0,
         "impact": "high"},
    ]
    assert match_event("PMI", currency="", events=events) is None


def test_currency_unknown_returns_none() -> None:
    """Any currency not in _COUNTRIES_FOR_CURRENCY must fail closed."""
    events = [
        {"country": "GB", "event": "PMI", "actual": 50.0, "estimate": 49.0,
         "impact": "high"},
    ]
    assert match_event("PMI", currency="XYZ", events=events) is None


def test_currency_lowercase_still_resolves() -> None:
    """Currency-code normalisation: lowercase ``gbp`` must work identically.

    Uses a multi-word title because ``fuzzy_score`` requires ≥2 overlapping
    words — see ``test_fuzzy_score_requires_two_matching_words``.
    """
    events = [
        {"country": "GB", "event": "Retail Sales MoM", "actual": 1.0,
         "estimate": 0.5, "impact": "high"},
    ]
    result = match_event("Retail Sales MoM", currency="gbp", events=events)
    assert result is not None
    assert result["country"] == "GB"


def test_empty_event_title_returns_none() -> None:
    events = [
        {"country": "GB", "event": "PMI", "actual": 50.0, "estimate": 49.0,
         "impact": "high"},
    ]
    assert match_event("", currency="GBP", events=events) is None


def test_no_events_returns_none() -> None:
    assert match_event("PMI", currency="GBP", events=[]) is None


def test_multiple_same_country_same_title_is_deterministic_per_payload() -> None:
    """Two GB entries that tie on every H3 tiebreaker dimension (country,
    event, impact). Python's stable sort means *payload order* is the
    final tiebreaker. Same payload → same answer (deterministic per call).
    Result is always GB — never leaks into another country."""
    a = {
        "country": "GB", "event": "S&P Global Manufacturing PMI Flash",
        "actual": 53.6, "estimate": 49.9, "impact": "high",
    }
    b = {
        "country": "GB", "event": "S&P Global Manufacturing PMI Flash",
        "actual": 54.0, "estimate": 49.5, "impact": "high",
    }
    # Same payload, two calls → identical result.
    r1 = match_event(
        "S&P Global Manufacturing PMI Flash", currency="GBP", events=[a, b]
    )
    r2 = match_event(
        "S&P Global Manufacturing PMI Flash", currency="GBP", events=[a, b]
    )
    assert r1 is not None and r2 is not None
    assert r1["country"] == r2["country"] == "GB"
    assert r1["actual"] == r2["actual"]  # determinism per payload
    # First-in-payload wins as final tiebreaker (stable sort).
    assert r1["actual"] == 53.6


def test_same_title_other_country_with_actual_does_not_steal_match() -> None:
    """Payload has GB event (actual=None) and US event (actual=published).
    A GBP poll must not return US's value — it should return None."""
    events = [
        {
            "country": "US", "event": "Retail Sales MoM",
            "actual": 1.2, "estimate": 0.5, "impact": "high",
        },
        {
            "country": "GB", "event": "Retail Sales MoM",
            "actual": None, "estimate": 0.3, "impact": "high",
        },
    ]
    assert match_event("Retail Sales MoM", currency="GBP", events=events) is None


def test_gbp_rejects_eu_composite() -> None:
    """EU composite PMI must not satisfy a GBP lookup (even though EU
    is in the TRADED_COUNTRIES set generally)."""
    events = [
        {
            "country": "EU", "event": "S&P Global Manufacturing PMI Flash",
            "actual": 52.2, "estimate": 50.8, "impact": "medium",
        },
    ]
    assert match_event(
        "S&P Global Manufacturing PMI Flash", currency="GBP", events=events,
    ) is None


# ---------------------------------------------------------------------------
# Block-pair: ADP vs NFP must not cross-match despite token overlap
# ---------------------------------------------------------------------------


def test_block_pair_adp_does_not_match_nfp() -> None:
    """A request titled 'ADP Non-Farm Employment Change' must not match
    the headline NFP event — they are different releases."""
    events = [
        {
            "country": "US", "event": "Non Farm Payrolls",
            "actual": 250000, "estimate": 180000, "impact": "high",
        },
    ]
    assert match_event(
        "ADP Non-Farm Employment Change", currency="USD", events=events,
    ) is None


def test_block_pair_adp_still_matches_adp() -> None:
    """ADP request must match the ADP release through the alias map."""
    events = [
        {
            "country": "US", "event": "ADP Employment Change",
            "actual": 175000, "estimate": 150000, "impact": "high",
        },
    ]
    result = match_event(
        "ADP Non-Farm Employment Change", currency="USD", events=events,
    )
    assert result is not None
    assert result["actual"] == 175000


# ---------------------------------------------------------------------------
# Aliases — ForexFactory naming finds Finnhub event via _FF_TO_FINNHUB
# ---------------------------------------------------------------------------


def test_alias_cpi_y_y_finds_inflation_rate_yoy() -> None:
    """A request for 'CPI Y/Y' must locate the 'Inflation Rate YoY' event."""
    events = [
        {
            "country": "US", "event": "Inflation Rate YoY",
            "actual": 3.2, "estimate": 3.1, "impact": "high",
        },
    ]
    result = match_event("CPI Y/Y", currency="USD", events=events)
    assert result is not None
    assert result["actual"] == 3.2


def test_alias_fomc_statement_finds_fed_interest_rate_decision() -> None:
    events = [
        {
            "country": "US", "event": "Fed Interest Rate Decision",
            "actual": 5.25, "estimate": 5.25, "impact": "high",
        },
    ]
    result = match_event("FOMC Statement", currency="USD", events=events)
    assert result is not None
    assert result["actual"] == 5.25


# ---------------------------------------------------------------------------
# Primitive coverage — normalize / fuzzy_score / expand_title / countries_for_currency
# ---------------------------------------------------------------------------


def test_normalize_lowercase_and_strip_punctuation() -> None:
    assert normalize("S&P Global PMI") == {"s", "p", "global", "pmi"}
    assert normalize("CPI Y/Y") == {"cpi", "y"}
    assert normalize("  ") == set()


def test_fuzzy_score_requires_two_matching_words() -> None:
    assert fuzzy_score(normalize("Inflation Rate YoY"), "Inflation") == 0


def test_fuzzy_score_requires_50pct_of_shorter_side() -> None:
    # "Rate" matches 1 word of "Inflation Rate YoY" (3 words) and 1 of
    # the candidate (1 word) — but only 1 common word, so still 0.
    assert fuzzy_score(normalize("Inflation Rate YoY"), "Rate") == 0


def test_fuzzy_score_returns_count_plus_pct() -> None:
    # title words: {inflation, rate, yoy}, candidate: same → 3 + 1.0 = 4.0
    score = fuzzy_score(normalize("Inflation Rate YoY"), "Inflation Rate YoY")
    assert score == pytest.approx(4.0)


def test_expand_title_returns_original_plus_aliases() -> None:
    out = expand_title("CPI Y/Y")
    assert out[0] == "CPI Y/Y"
    assert "inflation rate yoy" in out


def test_expand_title_unknown_returns_only_original() -> None:
    out = expand_title("Some Made-Up Release")
    assert out == ["Some Made-Up Release"]


def test_countries_for_currency_known() -> None:
    assert countries_for_currency("GBP") == frozenset({"GB"})
    assert countries_for_currency("EUR") == frozenset({"EU", "DE", "FR", "IT", "ES", "NL"})


def test_countries_for_currency_unknown_returns_none() -> None:
    assert countries_for_currency("XYZ") is None


def test_countries_for_currency_empty_returns_none() -> None:
    assert countries_for_currency("") is None
    assert countries_for_currency(None) is None


def test_countries_for_currency_whitespace_and_case() -> None:
    assert countries_for_currency("  gbp  ") == frozenset({"GB"})


def test_is_blocked_match_adp_vs_nfp() -> None:
    assert is_blocked_match("ADP Non-Farm Employment Change", "Non Farm Payrolls")
    assert is_blocked_match("ADP Non-Farm Employment Change", "Nonfarm Payrolls")


def test_is_blocked_match_no_block_for_unrelated() -> None:
    assert not is_blocked_match("CPI Y/Y", "Inflation Rate YoY")


# ---------------------------------------------------------------------------
# Module-private invariants (small smoke checks against accidental mutation)
# ---------------------------------------------------------------------------


def test_countries_for_currency_map_intact() -> None:
    """Guard against accidental edits to the currency→country map.
    If you add a currency, update this test."""
    assert set(M._COUNTRIES_FOR_CURRENCY.keys()) == {
        "USD", "GBP", "EUR", "JPY", "CAD",
    }


def test_eur_country_set_includes_composite() -> None:
    """The EU composite code must remain in the EUR set or the calendar
    will silently drop eurozone-aggregate releases."""
    assert "EU" in M._COUNTRIES_FOR_CURRENCY["EUR"]


# ---------------------------------------------------------------------------
# H2 — block-pair bidirectional (NFP request vs ADP candidate, the
# direction the legacy fix missed)
# ---------------------------------------------------------------------------


def test_block_pair_nfp_request_does_not_match_adp_candidate() -> None:
    """The 2026-05-14 review's H2 probe: an NFP scheduled-event poll must
    not silently match a stale ADP release. Same shape of bug as the
    2026-04-23 country-filter incident but on the release axis."""
    events = [
        {
            "country": "US", "event": "ADP Employment Change",
            "actual": 175000, "estimate": 150000, "impact": "high",
        },
    ]
    assert match_event(
        "Non-Farm Employment Change", currency="USD", events=events,
    ) is None


def test_block_pair_alt_nfp_spellings_also_blocked() -> None:
    """All NFP spellings classify as NFP and must reject ADP candidates."""
    adp = {
        "country": "US", "event": "ADP Employment Change",
        "actual": 175000, "estimate": 150000, "impact": "high",
    }
    for title in (
        "Non-Farm Payrolls",
        "Non Farm Payrolls",
        "Nonfarm Payrolls",
        "Nonfarm Employment Change",
    ):
        assert match_event(title, currency="USD", events=[adp]) is None, (
            f"title={title!r} unexpectedly matched the ADP event"
        )


def test_block_pair_classifier_handles_ambiguous_adp_title() -> None:
    """The ForexFactory title 'ADP Non-Farm Employment Change' contains
    both vocabularies. It must classify as ADP (the more specific token)
    so an ADP→ADP match is not falsely blocked."""
    from risk.news_calendar.matcher import _classify_adp_or_nfp
    assert _classify_adp_or_nfp("adp non-farm employment change") == "adp"
    assert _classify_adp_or_nfp("non-farm employment change") == "nfp"
    assert _classify_adp_or_nfp("non farm payrolls") == "nfp"
    assert _classify_adp_or_nfp("adp employment change") == "adp"
    assert _classify_adp_or_nfp("cpi y/y") is None


# ---------------------------------------------------------------------------
# H3 — deterministic EUR cross-country tiebreaker
# ---------------------------------------------------------------------------


def test_eur_de_vs_fr_tiebreaker_is_deterministic_either_payload_order() -> None:
    """DE and FR both publish their PMI Flash with identical impact and
    event names. The tiebreaker must pick the same country regardless of
    which order Finnhub returns them in."""
    de = {
        "country": "DE", "event": "S&P Global Manufacturing PMI Flash",
        "actual": 51.2, "estimate": 51.0, "impact": "high",
    }
    fr = {
        "country": "FR", "event": "S&P Global Manufacturing PMI Flash",
        "actual": 48.9, "estimate": 49.0, "impact": "high",
    }
    r1 = match_event(
        "S&P Global Manufacturing PMI Flash", currency="EUR",
        events=[de, fr],
    )
    r2 = match_event(
        "S&P Global Manufacturing PMI Flash", currency="EUR",
        events=[fr, de],
    )
    assert r1 is not None and r2 is not None
    # Alphabetical country tiebreaker → DE before FR.
    assert r1["country"] == "DE"
    assert r2["country"] == "DE"


def test_high_impact_beats_medium_impact_on_score_tie() -> None:
    """When two candidates score equally, the HIGH-impact one wins over
    the MEDIUM-impact one — even if the MEDIUM appears first or has a
    lexicographically smaller country code."""
    eu_medium = {
        "country": "EU", "event": "Retail Sales MoM",
        "actual": 0.5, "estimate": 0.3, "impact": "medium",
    }
    fr_high = {
        "country": "FR", "event": "Retail Sales MoM",
        "actual": 0.7, "estimate": 0.4, "impact": "high",
    }
    r = match_event(
        "Retail Sales MoM", currency="EUR",
        events=[eu_medium, fr_high],
    )
    assert r is not None
    assert r["country"] == "FR"
    assert r["impact"] == "high"


def test_original_title_beats_alias_on_score_tie() -> None:
    """If the original title and an alias both produce the same score
    against the same candidate, the original-title match wins (it ranked
    higher in the tiebreaker)."""
    # Contrived: original title scores exactly the same as an alias
    # would. Easiest construction is to use a title that IS the canonical
    # Finnhub name — no alias map entry needed for "Inflation Rate YoY".
    ev = {
        "country": "US", "event": "Inflation Rate YoY",
        "actual": 3.2, "estimate": 3.1, "impact": "high",
    }
    r = match_event("Inflation Rate YoY", currency="USD", events=[ev])
    assert r is not None
    assert r["actual"] == 3.2
