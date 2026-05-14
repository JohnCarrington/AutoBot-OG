"""Spread filter rule (§6.7).

Rejects new entries when the current spread is wide relative to either an
absolute pip cap or a volatility-relative cap (``ATR_MULT × ATR_M5``).
The lower of the two caps wins so a quiet bar with tight ATR can still
reject a wide spread.
"""
from __future__ import annotations

import math

from ..constants import SPREAD_ABS_CAP_PIPS, SPREAD_ATR_MULT
from ..types import MarketSnapshot, RuleResult


_RULE_NAME = "spread_filter"


def check_spread_filter(market: MarketSnapshot) -> RuleResult:
    """Allow only if ``current_spread <= min(ABS_CAP, ATR_MULT * atr_m5)``.

    Parameters
    ----------
    market : MarketSnapshot
        Carries ``current_spread_pips`` and ``atr_m5_pips``. If
        ``atr_m5_pips`` is non-positive or NaN, the ATR cap is ignored
        and only the absolute cap applies (defensive: a malformed ATR
        should not block all trading).

    Returns
    -------
    RuleResult
        ``allow=True`` when spread is within cap; otherwise
        ``allow=False`` with a reason naming the binding cap.
    """
    spread = market.current_spread_pips
    atr_pips = market.atr_m5_pips
    abs_cap = SPREAD_ABS_CAP_PIPS
    use_atr = atr_pips is not None and atr_pips > 0 and not math.isnan(atr_pips)
    atr_cap = SPREAD_ATR_MULT * atr_pips if use_atr else float("inf")
    cap = min(abs_cap, atr_cap)

    if spread <= cap:
        return RuleResult(allow=True, rule=_RULE_NAME, reason="ok")

    binding = "atr_cap" if atr_cap < abs_cap else "abs_cap"
    return RuleResult(
        allow=False,
        rule=_RULE_NAME,
        reason=(
            f"spread {spread:.2f}p > {binding}={cap:.2f}p "
            f"(abs={abs_cap:.2f}p, atr_mult×ATR_M5="
            f"{atr_cap if use_atr else 'n/a'})"
        ),
    )


__all__ = ["check_spread_filter"]
