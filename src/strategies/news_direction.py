"""News data-surprise → pair-trade-direction mapping (step 5a).

Pure functions only — no I/O, no calendar fetch, no state. Step 5b
will wrap these in the actual ``detect_news`` detector, which is
responsible for (a) discovering which release just fired via
``events_in_window``, (b) reading its ``actual``/``estimate`` to
compute the deviation, and (c) calling ``direction_for_release``
below with that deviation.

The correctness gate here is the **sign convention** for "bad-news-
is-higher" releases. Most economic releases are DIRECT: when
``actual > forecast`` the currency strengthens (CPI beat, NFP beat,
GDP beat, retail-sales beat). A small set are INVERTED: a beat
means the currency *weakens* because the release measures economic
weakness (unemployment rate, jobless claims).

The :data:`INVERTED_RELEASES` set is intentionally conservative.
Adding a release here flips its sign across every pair, so the bar
for inclusion is "I am certain higher-is-bad for the currency,
end-of-discussion."

Out of scope here (step 5b owns these):
- discovering which event just fired,
- reading the actual / estimate / deviation,
- gating on `current_reaction` from the structure engine,
- the de-overlap contract vs ema_pullback / structure_break.
"""
from __future__ import annotations

from typing import Optional

from common import Direction
from risk.news_calendar.impact import DEVIATION_THRESHOLD
from risk.rules.news_blackout import _PAIR_CURRENCIES


# Releases where ``actual > forecast`` means the currency WEAKENS
# (not strengthens). Match by lowercase substring against the event
# name string Finnhub returns.
#
# Included (and why):
#   - "unemployment rate"        — direct labour-market weakness gauge.
#   - "unemployment claims"      — same series under the ForexFactory name.
#   - "initial jobless claims"   — US weekly labour-market weakness gauge.
#   - "jobless claims"           — substring covers "initial jobless claims",
#                                  "continuing jobless claims", etc.
#   - "continuing claims"        — US continuing-claims series.
#   - "claimant count change"    — UK weekly labour-market weakness gauge.
#
# Deliberately NOT included (these tempt you but are ambiguous, and a
# wrong sign on a real release is a $$ bug — better silent than wrong):
#   - "trade deficit" / "trade balance" — sign convention depends on
#       whether the series is reported as a signed balance or as a
#       deficit magnitude; Finnhub event names don't always disambiguate.
#   - "inflation expectations" — central-bank reaction function dominates;
#       a higher print does not cleanly map to "currency weaker".
#   - "import prices" / "export prices" — driven by FX itself; circular.
INVERTED_RELEASES: frozenset[str] = frozenset({
    "unemployment rate",
    "unemployment claims",
    "initial jobless claims",
    "jobless claims",
    "continuing claims",
    "claimant count change",
})


def _is_inverted(event_name: str) -> bool:
    """True if ``event_name`` matches any inverted-release keyword."""
    lower = event_name.lower()
    return any(pat in lower for pat in INVERTED_RELEASES)


def currency_strength_from_surprise(
    event_name: str,
    deviation: float,
    *,
    threshold: float = DEVIATION_THRESHOLD,
) -> Optional[str]:
    """Map (event, deviation) → ``"STRONGER"`` / ``"WEAKER"`` / ``None``.

    Returns ``None`` when ``abs(deviation) <= threshold`` (in-line; no
    actionable surprise). Otherwise computes the raw direction from the
    sign of ``deviation`` and flips it when ``event_name`` matches an
    inverted-release pattern.

    Parameters
    ----------
    event_name :
        Free-text event name as Finnhub returns it.
    deviation :
        Signed fractional surprise — ``(actual - forecast) / |forecast|``.
    threshold :
        Below this absolute deviation, the release is treated as in-line.
        Defaults to :data:`risk.news_calendar.impact.DEVIATION_THRESHOLD`
        so the threshold matches the rest of the calendar layer.
    """
    if abs(deviation) <= threshold:
        return None
    raw = "STRONGER" if deviation > 0 else "WEAKER"
    if _is_inverted(event_name):
        return "WEAKER" if raw == "STRONGER" else "STRONGER"
    return raw


def pair_direction_from_currency_strength(
    pair: str,
    currency: str,
    strength: str,
) -> Optional[Direction]:
    """Map (pair, the-currency-that-moved, its-strength) → ``Direction``.

    Base-currency stronger → ``BULLISH`` (pair up). Quote-currency
    stronger → ``BEARISH`` (pair down). Mirror for ``WEAKER``.

    Returns ``None`` if the pair is unknown or the currency is neither
    base nor quote — defensive guard; callers are expected to filter
    events to the pair's two currencies before calling.
    """
    pair_currencies = _PAIR_CURRENCIES.get(pair.upper())
    if pair_currencies is None:
        return None
    base, quote = pair_currencies
    cur = currency.upper()
    if cur == base:
        return Direction.BULLISH if strength == "STRONGER" else Direction.BEARISH
    if cur == quote:
        return Direction.BEARISH if strength == "STRONGER" else Direction.BULLISH
    return None


def direction_for_release(
    pair: str,
    release_currency: str,
    event_name: str,
    deviation: float,
    *,
    threshold: float = DEVIATION_THRESHOLD,
) -> Optional[Direction]:
    """End-to-end: (pair, release info) → ``Direction`` or ``None``.

    Convenience over :func:`currency_strength_from_surprise` +
    :func:`pair_direction_from_currency_strength`. Returns ``None`` when
    the surprise is in-line, the pair is unknown, or the release
    currency is not part of the pair.
    """
    strength = currency_strength_from_surprise(
        event_name, deviation, threshold=threshold,
    )
    if strength is None:
        return None
    return pair_direction_from_currency_strength(pair, release_currency, strength)


__all__ = [
    "INVERTED_RELEASES",
    "currency_strength_from_surprise",
    "pair_direction_from_currency_strength",
    "direction_for_release",
]
