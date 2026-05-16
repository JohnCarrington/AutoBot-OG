"""Phase 12 startup hydration — rebuild ``_previous_structure``
from the engine jsonl.

C-4 closes the gap that would otherwise make BotLoop restart drop
the previous-bar reference for every pair, producing:

- Spurious cold-start LEVEL_INVALIDATED events the moment the next
  bar arrives (because ``prev is None`` returns ``[]`` from
  :func:`structure_alerts.diff.compute_structure_diff`, but the
  bar AFTER that has a fresh prev that omits any levels the engine
  had been tracking pre-restart).
- Duplicate WARNING / CRITICAL alerts for transitions that already
  paged the operator before the restart.

Reading the latest line per pair from
``data/structure/structure_state.jsonl`` (the Phase 11 engine log)
gives the BotLoop a faithful-enough prev to compare against on the
first post-startup bar. "Faithful enough" — full DataFrames and
per-zone debug payloads are not rehydrated; the diff layer only
reads HTF bias, structure mode, current reaction, levels (price +
side), and ``nearest_*.price / .score / .timeframe``.

Refinement A (locked in C-1 plan)
---------------------------------

The compact level entry in the jsonl includes ``"tf"`` (timeframe).
Hydration threads it back into each rehydrated ``StructureLevel`` so
the LEVEL_INVALIDATED full_text rendering ("Support level at X
invalidated (H1)") reads the right timeframe even when the level was
removed from the engine's catalogue between the restart and the
first post-startup bar.

Failure isolation
-----------------

- Missing file → ``{}``, no exception. Cold first run.
- Corrupt line → skip, log WARNING, continue with remainder.
- Record missing ``pair`` → skip, log WARNING.
- Record fails rehydration (e.g., unknown enum value) → skip pair,
  log WARNING with exc_info; other pairs unaffected.

The function is called once at startup
(:meth:`BotLoop.hydrate` in C-6). Subsequent runtime mutation of
``_previous_structure`` is purely in-memory.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional, Union

from structure_engine.types import StructureLevel, StructureState

from .constants import quantise_price
from .diff import level_side


logger = logging.getLogger(__name__)


_VALID_LEVEL_TYPES: frozenset[str] = frozenset(
    {"SUPPORT", "RESISTANCE", "LIQUIDITY_HIGH", "LIQUIDITY_LOW"}
)
_VALID_TIMEFRAMES: frozenset[str] = frozenset({"H1", "M15", "M5"})


def load_latest_structure_state_per_pair(
    path: Union[str, Path],
) -> dict[str, StructureState]:
    """Hydrate previous-structure state per pair from the engine jsonl.

    Linear scan, last-write-wins per pair (jsonl is append-only, so
    the last line for a pair is the most recent).

    Parameters
    ----------
    path
        Filesystem location of the Phase 11 engine jsonl. Typically
        ``data/structure/structure_state.jsonl`` (controlled by
        :data:`structure_engine.constants.STRUCTURE_LOG_PATH`).

    Returns
    -------
    dict[str, StructureState]
        One entry per pair found. Pairs absent from the jsonl do not
        appear in the dict — BotLoop's caller-side ``.get(pair)``
        yields ``None`` for them, which the C-2 diff layer treats as
        the cold-start case.
    """
    target = Path(path)
    if not target.exists():
        return {}

    latest_by_pair: dict[str, dict[str, Any]] = {}
    try:
        with target.open("r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, 1):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    logger.warning(
                        "structure_alerts.hydration: line %d unparseable, skip",
                        lineno,
                    )
                    continue
                pair = rec.get("pair")
                if not pair or not isinstance(pair, str):
                    logger.warning(
                        "structure_alerts.hydration: line %d missing pair, skip",
                        lineno,
                    )
                    continue
                latest_by_pair[pair] = rec
    except OSError as exc:
        logger.warning(
            "structure_alerts.hydration: failed to read %s: %s — returning empty",
            target,
            exc,
        )
        return {}

    out: dict[str, StructureState] = {}
    for pair, rec in latest_by_pair.items():
        try:
            out[pair] = _rec_to_state(rec)
        except Exception:
            # Defensive — a single malformed record must not poison
            # hydration for other pairs. exc_info=True so the
            # operator's journalctl carries enough context to
            # diagnose the bad record.
            logger.warning(
                "structure_alerts.hydration: failed to rehydrate %s, skip",
                pair,
                exc_info=True,
            )
    return out


def _rec_to_state(rec: dict[str, Any]) -> StructureState:
    """Rebuild a :class:`StructureState` from one jsonl record."""
    pair = rec["pair"]
    levels = _rehydrate_levels(pair, rec.get("levels"))
    nearest_support = _rehydrate_nearest(
        pair=pair,
        price=rec.get("nearest_support"),
        score=rec.get("nearest_support_score"),
        side="SUPPORT",
        levels=levels,
    )
    nearest_resistance = _rehydrate_nearest(
        pair=pair,
        price=rec.get("nearest_resistance"),
        score=rec.get("nearest_resistance_score"),
        side="RESISTANCE",
        levels=levels,
    )
    return StructureState(
        pair=pair,
        timestamp=rec.get("timestamp", ""),
        is_valid=bool(rec.get("is_valid", True)),
        htf_bias=rec.get("htf_bias", "NEUTRAL"),  # type: ignore[arg-type]
        local_bias=rec.get("local_bias", "NEUTRAL"),  # type: ignore[arg-type]
        nearest_support=nearest_support,
        nearest_resistance=nearest_resistance,
        # liquidity_above / _below are not consulted by the diff or
        # trigger layers; leaving them unhydrated keeps the surface
        # small and the rehydration cost bounded.
        liquidity_above=None,
        liquidity_below=None,
        current_reaction=rec.get("current_reaction", "NONE"),  # type: ignore[arg-type]
        acceptance_state=rec.get("acceptance_state", "NONE"),  # type: ignore[arg-type]
        structure_mode=rec.get("structure_mode", "UNKNOWN"),  # type: ignore[arg-type]
        confidence=float(rec.get("confidence", 0.0)),
        reason=rec.get("reason", "hydrated"),
        levels=levels,
        debug={"hydrated": True},
    )


def _rehydrate_levels(
    pair: str, raw_levels: Optional[list[Any]],
) -> list[StructureLevel]:
    """Rebuild :class:`StructureLevel` list from the compact entries.

    Each input entry must be a dict with keys ``p`` (price float),
    ``s`` (level_type — one of the four engine values), ``sc``
    (score float), ``tf`` (timeframe — H1/M15/M5). Malformed entries
    are silently skipped (per-record skip would be too loud on the
    inevitable engine-format-rev cases).
    """
    if not raw_levels:
        return []
    out: list[StructureLevel] = []
    for entry in raw_levels:
        if not isinstance(entry, dict):
            continue
        try:
            price = float(entry["p"])
            score = float(entry["sc"])
        except (KeyError, TypeError, ValueError):
            continue
        level_type = entry.get("s")
        tf = entry.get("tf")
        if level_type not in _VALID_LEVEL_TYPES:
            continue
        if tf not in _VALID_TIMEFRAMES:
            continue
        out.append(
            StructureLevel(
                pair=pair,
                level_type=level_type,  # type: ignore[arg-type]
                price=price,
                # Zone band is not in the compact entry — use a
                # zero-width band. The diff layer compares quantised
                # prices, not band overlap; triggers do not read
                # zone_low / zone_high either.
                zone_low=price,
                zone_high=price,
                timeframe=tf,  # type: ignore[arg-type]
                score=score,
                touch_count=0,
                last_touched_ts=None,
                source="hydrated",
                debug={"hydrated": True},
            )
        )
    return out


def _rehydrate_nearest(
    *,
    pair: str,
    price: Optional[float],
    score: Optional[float],
    side: str,
    levels: list[StructureLevel],
) -> Optional[StructureLevel]:
    """Reconstruct ``nearest_support`` / ``nearest_resistance``.

    ``side`` is the canonical ``SUPPORT`` / ``RESISTANCE`` value the
    engine uses for nearest fields (per ``_zone_to_level`` in
    :mod:`structure_engine.structure_state` — the level_type is
    always ``"SUPPORT"`` or ``"RESISTANCE"`` for these fields, even
    when the underlying zone is a LIQUIDITY_LOW / LIQUIDITY_HIGH
    cluster).

    Hydration searches ``levels`` for a matching ``(side,
    quantised_price)`` entry via :func:`structure_alerts.diff.level_side`,
    so a LIQUIDITY_LOW level in the compact list is matched as a
    SUPPORT candidate. Fields recovered from the match: ``timeframe``,
    ``zone_low``, ``zone_high``, ``touch_count``, ``last_touched_ts``,
    ``source``.

    Fallback when no match is found (cap eviction, pre-refinement-A
    record): conservative ``"H1"`` timeframe (longest, most common
    engine output) plus a zero-width zone band. The diff layer does
    not gate on timeframe, so the fallback only affects rendering
    fidelity of a LEVEL_INVALIDATED event for the first post-restart
    bar of an edge-case pair.
    """
    if price is None:
        return None

    target_score = float(score) if score is not None else 0.0
    target_q = quantise_price(pair, float(price))
    for level in levels:
        if (
            level_side(level) == side
            and quantise_price(pair, level.price) == target_q
        ):
            return StructureLevel(
                pair=pair,
                level_type=side,  # type: ignore[arg-type]
                price=float(price),
                zone_low=level.zone_low,
                zone_high=level.zone_high,
                timeframe=level.timeframe,
                score=target_score,
                touch_count=level.touch_count,
                last_touched_ts=level.last_touched_ts,
                source=level.source,
                debug={"hydrated": True},
            )

    return StructureLevel(
        pair=pair,
        level_type=side,  # type: ignore[arg-type]
        price=float(price),
        zone_low=float(price),
        zone_high=float(price),
        timeframe="H1",
        score=target_score,
        touch_count=0,
        last_touched_ts=None,
        source="hydrated",
        debug={"hydrated_fallback": True},
    )


__all__ = ["load_latest_structure_state_per_pair"]
