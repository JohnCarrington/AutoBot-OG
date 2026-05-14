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


def test_multiple_same_country_same_title_resolves_deterministically() -> None:
    """If two GB entries share a title, the tie-break (first > wins, not >=)
    must resolve to one of them — never leak into a different country."""
    events = [
        {
            "country": "GB", "event": "S&P Global Manufacturing PMI Flash",
            "actual": 53.6, "estimate": 49.9, "impact": "high",
        },
        {
            "country": "GB", "event": "S&P Global Manufacturing PMI Flash",
            "actual": 54.0, "estimate": 49.5, "impact": "high",
        },
    ]
    result = match_event(
        "S&P Global Manufacturing PMI Flash",
        currency="GBP",
        events=events,
    )
    assert result is not None
    assert result["country"] == "GB"
    assert result["actual"] in (53.6, 54.0)


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
