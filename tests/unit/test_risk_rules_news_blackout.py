"""Tests for risk.rules.news_blackout.

The rule delegates per-currency lookups to
:py:func:`risk.news_calendar.is_blackout`; tests monkeypatch that
function so the rule's branching can be exercised without touching the
real calendar cache.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from day_type import DayType
from regime.labels import Direction

from risk.news_calendar.calendar import BlackoutResult
from risk.rules import news_blackout
from risk.rules.news_blackout import check_news_blackout
from risk.types import CandidateTrade


def _candidate(pair: str = "GBPUSD") -> CandidateTrade:
    return CandidateTrade(
        pair=pair,
        intended_direction=Direction.BULLISH,
        intended_day_type=DayType.NORMAL,
        planned_entry_price=1.30,
        strategy_name="bb_bounce",
    )


def _now() -> datetime:
    return datetime(2025, 5, 14, 12, 0, tzinfo=timezone.utc)


def _stub(impact_outcomes: dict[str, BlackoutResult], monkeypatch):
    """Patch `is_blackout` to return per-currency BlackoutResults."""

    def fake_is_blackout(currency: str, query_time, *, lookback_min=15, lookahead_min=15):
        if currency in impact_outcomes:
            return impact_outcomes[currency]
        return BlackoutResult(
            is_blocked=False,
            reason="no event in window",
            event_summary=None,
            confidence="low",
        )

    monkeypatch.setattr(news_blackout, "is_blackout", fake_is_blackout)


# --- Allows ----------------------------------------------------------------


def test_allows_when_no_blackout(monkeypatch) -> None:
    _stub({}, monkeypatch)
    result = check_news_blackout(_candidate(), _now())
    assert result.allow is True
    assert result.rule == "news_blackout"


# --- Per-currency rejection -------------------------------------------------


def test_rejects_when_first_currency_blocked(monkeypatch) -> None:
    _stub(
        {
            "GBP": BlackoutResult(
                is_blocked=True,
                reason="high-impact event in window",
                event_summary="BoE Interest Rate Decision @ 2025-05-14 12:00",
                confidence="high",
            )
        },
        monkeypatch,
    )
    result = check_news_blackout(_candidate("GBPUSD"), _now())
    assert result.allow is False
    assert "GBP" in result.reason
    assert "BoE" in result.reason


def test_rejects_when_second_currency_blocked(monkeypatch) -> None:
    _stub(
        {
            "USD": BlackoutResult(
                is_blocked=True,
                reason="medium-impact event in window",
                event_summary="US CPI @ 2025-05-14 12:00",
                confidence="medium",
            )
        },
        monkeypatch,
    )
    result = check_news_blackout(_candidate("GBPUSD"), _now())
    assert result.allow is False
    assert "USD" in result.reason
    assert "CPI" in result.reason


def test_medium_impact_blocks_in_phase_4(monkeypatch) -> None:
    """Locked decision: MEDIUM blocks new entries the same as HIGH does."""
    _stub(
        {
            "USD": BlackoutResult(
                is_blocked=True,
                reason="medium-impact event in window",
                event_summary="US Retail Sales @ 2025-05-14 12:00",
                confidence="medium",
            )
        },
        monkeypatch,
    )
    result = check_news_blackout(_candidate("EURUSD"), _now())
    assert result.allow is False


def test_high_impact_blocks(monkeypatch) -> None:
    _stub(
        {
            "USD": BlackoutResult(
                is_blocked=True,
                reason="high-impact event in window",
                event_summary="FOMC @ 2025-05-14 12:00",
                confidence="high",
            )
        },
        monkeypatch,
    )
    result = check_news_blackout(_candidate("EURUSD"), _now())
    assert result.allow is False


# --- Unknown pair handling --------------------------------------------------


def test_rejects_unknown_pair(monkeypatch) -> None:
    _stub({}, monkeypatch)
    result = check_news_blackout(_candidate("BTCUSD"), _now())
    assert result.allow is False
    assert "unknown_pair" in result.reason


def test_unknown_pair_does_not_call_is_blackout(monkeypatch) -> None:
    """Defensive: an unmappable pair must short-circuit BEFORE the lookup."""
    calls = []

    def spy(currency, query_time, *, lookback_min=15, lookahead_min=15):
        calls.append(currency)
        return BlackoutResult(
            is_blocked=False,
            reason="no event in window",
            event_summary=None,
            confidence="low",
        )

    monkeypatch.setattr(news_blackout, "is_blackout", spy)
    check_news_blackout(_candidate("XYZUSD"), _now())
    assert calls == []


# --- Currency-mapping coverage ---------------------------------------------


@pytest.mark.parametrize(
    "pair,currencies",
    [
        ("GBPUSD", ("GBP", "USD")),
        ("EURUSD", ("EUR", "USD")),
        ("USDJPY", ("USD", "JPY")),
        ("USDCAD", ("USD", "CAD")),
        ("GBPJPY", ("GBP", "JPY")),
    ],
)
def test_each_pair_queries_both_currencies(pair, currencies, monkeypatch) -> None:
    queried: list[str] = []

    def fake_is_blackout(currency, query_time, *, lookback_min=15, lookahead_min=15):
        queried.append(currency)
        return BlackoutResult(
            is_blocked=False,
            reason="no event in window",
            event_summary=None,
            confidence="low",
        )

    monkeypatch.setattr(news_blackout, "is_blackout", fake_is_blackout)
    check_news_blackout(_candidate(pair), _now())
    assert tuple(queried) == currencies


def test_first_block_short_circuits(monkeypatch) -> None:
    """If the first currency is blocked, the second is not queried."""
    queried: list[str] = []

    def fake_is_blackout(currency, query_time, *, lookback_min=15, lookahead_min=15):
        queried.append(currency)
        if currency == "GBP":
            return BlackoutResult(
                is_blocked=True,
                reason="high-impact event in window",
                event_summary="BoE @ 2025-05-14 12:00",
                confidence="high",
            )
        return BlackoutResult(
            is_blocked=False,
            reason="no event in window",
            event_summary=None,
            confidence="low",
        )

    monkeypatch.setattr(news_blackout, "is_blackout", fake_is_blackout)
    check_news_blackout(_candidate("GBPUSD"), _now())
    assert queried == ["GBP"]
