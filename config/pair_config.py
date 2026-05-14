"""Per-pair configuration: pip conventions, SL floors, epic→pair parsing.

Single source of truth for pip sizing, broker-side minimum stop distances,
and small helpers shared across the risk and execution layers.

Source
------
Ported from the legacy AutoBot codebase (``/opt/tradingbot/pair_config.py``),
which served the same role for the prior bot. The legacy file additionally
carried a MACD-histogram column-name shim (``pick_macd_hist`` and friends)
that handled five historical naming variants; that shim is intentionally
**not** ported here. ``src/indicators/macd.py`` owns the MACD surface in
this repo with a single naming convention and the v1-locked parameter set
(fast=12, slow=26, signal=9) — see ``docs/v1_architecture.md`` §3.4.

Scope
-----
All four major pairs (plus GBPJPY scaffolding) are retained even though
v1 trades GBPUSD only. Multi-pair semantics are documented in
``docs/v1_architecture.md`` §6.8 (concurrent caps) and §8.1 (EURUSD v2).
"""

from __future__ import annotations

import os
from typing import Final


# ---------------------------------------------------------------------------
# Supported pairs
# ---------------------------------------------------------------------------
PAIRS: Final[tuple[str, ...]] = ("GBPUSD", "EURUSD", "USDJPY", "USDCAD")

# ---------------------------------------------------------------------------
# Points-per-pip: all IG spread-bet FX pairs are 1 point = 1 pip.
# ---------------------------------------------------------------------------
POINTS_PER_PIP: Final[dict[str, float]] = {
    "GBPUSD": 1.0,
    "EURUSD": 1.0,
    "USDJPY": 1.0,
    "USDCAD": 1.0,
    "GBPJPY": 1.0,
}
DEFAULT_PPP: Final[float] = 1.0


def get_ppp(epic_or_symbol: str) -> float:
    """Return IG points-per-pip for an epic or symbol string.

    Accepts either a raw pair symbol (e.g. ``"GBPUSD"``) or a full IG
    epic (e.g. ``"CS.D.GBPUSD.TODAY.IP"``); does a direct lookup first,
    then a substring scan as a fallback.
    """
    key = epic_or_symbol.upper()
    if key in POINTS_PER_PIP:
        return POINTS_PER_PIP[key]
    for sym, val in POINTS_PER_PIP.items():
        if sym in key:
            return val
    return DEFAULT_PPP


# ---------------------------------------------------------------------------
# Per-pair minimum SL floors (pips), env-overridable.
#
# These are the broker / execution-noise floors, not the v1 strategy
# floors. The v1 spec (``docs/v1_architecture.md`` §6.1) defines
# ``SL = max(pip_floor, multiplier × ATR_M5)`` per strategy, where
# ``pip_floor`` is the value below. Override via env var
# ``<PAIR>_MIN_SL_PIPS`` (e.g. ``GBPUSD_MIN_SL_PIPS=15``).
# ---------------------------------------------------------------------------
MIN_SL_PIPS: dict[str, float] = {}
for _p in PAIRS:
    _e = os.getenv(f"{_p}_MIN_SL_PIPS")
    if _e is not None:
        MIN_SL_PIPS[_p] = float(_e)
# GBPUSD floor intentionally diverges from the legacy port (was 12.0).
# v1 spec ``docs/v1_architecture.md`` §6.1 locks GBPUSD at 15p (band 15-20),
# so the default is set here. Override via ``GBPUSD_MIN_SL_PIPS`` still works.
MIN_SL_PIPS.setdefault("GBPUSD", 15.0)
MIN_SL_PIPS.setdefault("EURUSD", 10.0)
MIN_SL_PIPS.setdefault("USDJPY", 15.0)
MIN_SL_PIPS.setdefault("USDCAD", 10.0)
MIN_SL_PIPS.setdefault("GBPJPY", 12.0)


def pair_from_epic(epic: str) -> str:
    """Extract pair symbol from an IG epic.

    ``"CS.D.USDJPY.TODAY.IP" -> "USDJPY"``. Falls back to the upper-cased
    input if the epic shape is unexpected.
    """
    parts = epic.split(".")
    return parts[2] if len(parts) >= 3 else epic.upper()


# ---------------------------------------------------------------------------
# Pip ↔ price conversion (Phase 5).
#
# A "pip" is the smallest *quoted* price increment for a pair (one tenth of
# the table-stakes quote unit for JPY pairs, four decimal places elsewhere).
# We expose this as a per-pair constant rather than deriving it from the
# raw quote because broker conventions diverge for exotic pairs — making
# the table the source of truth means the strategy layer never has to
# inspect price magnitudes to decide.
# ---------------------------------------------------------------------------
PIP_SIZE: Final[dict[str, float]] = {
    "GBPUSD": 0.0001,
    "EURUSD": 0.0001,
    "USDJPY": 0.01,
    "USDCAD": 0.0001,
    "GBPJPY": 0.01,
}
_DEFAULT_PIP_SIZE: Final[float] = 0.0001


def pip_size_for(pair: str) -> float:
    """Return the price increment of one pip for ``pair``.

    Returns ``0.0001`` for major non-JPY pairs and ``0.01`` for JPY
    pairs. Unknown pairs default to ``0.0001`` (the four-decimal
    convention) — explicit lookup is preferred over magnitude-based
    detection so a new pair surfaces here rather than in price math.
    """
    return PIP_SIZE.get(pair.upper(), _DEFAULT_PIP_SIZE)


def pip_to_price(pair: str, pips: float) -> float:
    """Convert a pip count to a price-units delta for ``pair``.

    Used by the strategy and risk layers when translating tunables
    expressed in pips (``MIN_SL_PIPS``, ATR multipliers in pip-space)
    into price-units offsets applied to candle prices.
    """
    return pips * pip_size_for(pair)


def price_to_pips(pair: str, price_diff: float) -> float:
    """Convert a price-units delta to pips for ``pair``.

    Inverse of :py:func:`pip_to_price`. Sign-preserving: a negative
    ``price_diff`` returns a negative pip count.
    """
    return price_diff / pip_size_for(pair)


__all__ = [
    "PAIRS",
    "POINTS_PER_PIP",
    "DEFAULT_PPP",
    "MIN_SL_PIPS",
    "PIP_SIZE",
    "get_ppp",
    "pair_from_epic",
    "pip_size_for",
    "pip_to_price",
    "price_to_pips",
]
