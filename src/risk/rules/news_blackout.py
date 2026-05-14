"""News-blackout rule (§6.6).

Delegates the per-currency check to
:py:func:`risk.news_calendar.is_blackout` and rejects the entry if any
currency relevant to the candidate's pair is currently inside a
blackout window.

Phase 4 policy (locked decision): both ``HIGH`` and ``MEDIUM`` impact
events block new entries equally. The HIGH-vs-MEDIUM distinction
(HIGH also blocks stop modifications mid-window) lives in Phase 5's
execution layer — that's outside ``allow_entry``'s scope.
"""
from __future__ import annotations

from datetime import datetime

from ..news_calendar import is_blackout
from ..types import CandidateTrade, RuleResult


_RULE_NAME = "news_blackout"

# Pair → constituent currency codes (in order). Phase 4 supports the four
# major pairs from ``config/pair_config.py``; new pairs added to v2 must
# extend this map.
_PAIR_CURRENCIES: dict[str, tuple[str, str]] = {
    "GBPUSD": ("GBP", "USD"),
    "EURUSD": ("EUR", "USD"),
    "USDJPY": ("USD", "JPY"),
    "USDCAD": ("USD", "CAD"),
    "GBPJPY": ("GBP", "JPY"),
}


def _currencies_for(pair: str) -> tuple[str, ...]:
    key = pair.upper()
    return _PAIR_CURRENCIES.get(key, ())


def check_news_blackout(
    candidate: CandidateTrade,
    now_utc: datetime,
) -> RuleResult:
    """Allow only if no currency in the pair is inside a blackout window.

    Queries :py:func:`risk.news_calendar.is_blackout` per-currency. The
    first currency that returns ``is_blocked=True`` produces the
    rejection (and its ``event_summary`` is surfaced in ``reason``).

    Pairs whose currency mapping is unknown are conservatively rejected
    with reason ``"unknown_pair"`` so a silent typo in caller code does
    not let an unmappable pair slip past the blackout layer.
    """
    currencies = _currencies_for(candidate.pair)
    if not currencies:
        return RuleResult(
            allow=False,
            rule=_RULE_NAME,
            reason=(
                f"unknown_pair: '{candidate.pair}' has no currency mapping "
                "in risk.rules.news_blackout._PAIR_CURRENCIES"
            ),
        )

    for currency in currencies:
        result = is_blackout(currency, now_utc)
        if result.is_blocked:
            summary = result.event_summary or "(no event summary)"
            return RuleResult(
                allow=False,
                rule=_RULE_NAME,
                reason=(
                    f"{currency} blackout: {result.reason} — {summary}"
                ),
            )

    return RuleResult(allow=True, rule=_RULE_NAME, reason="ok")


__all__ = ["check_news_blackout"]
