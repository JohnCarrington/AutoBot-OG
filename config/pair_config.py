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


__all__ = [
    "PAIRS",
    "POINTS_PER_PIP",
    "DEFAULT_PPP",
    "MIN_SL_PIPS",
    "get_ppp",
    "pair_from_epic",
]
