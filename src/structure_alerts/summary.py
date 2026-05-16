"""Phase 12 hourly summary — passive structure heartbeat per spec §11.

:func:`build_hourly_summary` renders the engine's per-pair
:class:`StructureState` into a single ``HOURLY_SUMMARY`` AlertEvent
(INFO severity). BotLoop (C-6) fires this at every M5 BAR_CLOSE
where ``candle.close_time.minute == 0``, once per configured pair.

Heartbeat semantics
-------------------

Absence of an hourly summary is one of several heartbeat signals
(documented in MODULE.md). Combined with feed-state events and
trade-lifecycle alerts, it forms the passive availability picture.
The function itself has no awareness of "heartbeat" status — it's a
plain state renderer.

Dedupe key
----------

``{pair}_HOURLY_SUMMARY_{hour_bucket}`` where ``hour_bucket`` is the
state's bar-close timestamp truncated to the hour. Combined with the
INFO 2-hour cooldown, this means:

- A 14:00 bar close fires summary with bucket "...T14:00:00+00:00"
- A 15:00 bar close fires with bucket "...T15:00:00+00:00"
- A duplicate 14:00 bar (rare: gap-fill replaying old data) would
  re-attempt the same bucket, blocked by dedupe
- A 14:00 summary missed (BotLoop not running at the boundary) is
  not recovered — see heartbeat semantics

NaN / None handling
-------------------

The renderer is defensive: ``None`` levels render as ``—``, ``NaN``
prices/scores render as ``—``, ``NaN`` confidence renders as ``—``.
The engine can legitimately emit any of these during warm-up or
under feed degradation; an operator's hourly summary should remain
readable rather than crash the bar-close pipeline.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

from structure_engine.types import StructureLevel, StructureState
from config.pair_config import pip_size_for

from .types import AlertEvent, AlertEventKind, severity_for


def build_hourly_summary(
    state: StructureState,
    *,
    now: datetime,
) -> AlertEvent:
    """Render a HOURLY_SUMMARY :class:`AlertEvent` for ``state``.

    Parameters
    ----------
    state
        The Phase 11 :class:`StructureState` produced for the bar at
        which the summary fires. Read-only: this function does not
        consult prev-bar state.
    now
        Wall-clock UTC datetime used as the AlertEvent's timestamp.
        Caller (BotLoop in C-6) passes ``self._clock()``. The
        ``hour_bucket`` in the dedupe key derives from
        ``state.timestamp``, not ``now`` — what matters for dedupe is
        "which bar's hour is this", not "when did the BotLoop notice".
    """
    pair = state.pair
    full_text = _render_full_text(state)
    short_text = _render_short_text(state)
    hour_bucket = _hour_bucket(state.timestamp)
    dedupe_key = f"{pair}_HOURLY_SUMMARY_{hour_bucket}"
    kind = AlertEventKind.HOURLY_SUMMARY
    return AlertEvent(
        kind=kind,
        pair=pair,
        severity=severity_for(kind),
        timestamp=now,
        dedupe_key=dedupe_key,
        full_text=full_text,
        short_text=short_text,
        debug={
            "htf_bias": state.htf_bias,
            "local_bias": state.local_bias,
            "structure_mode": state.structure_mode,
            "current_reaction": state.current_reaction,
            "acceptance_state": state.acceptance_state,
            "confidence": (
                None
                if isinstance(state.confidence, float) and math.isnan(state.confidence)
                else state.confidence
            ),
            "hour_bucket": hour_bucket,
            "bar_timestamp": state.timestamp,
        },
    )


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _render_full_text(state: StructureState) -> str:
    """Spec §11 four-line summary body."""
    pair = state.pair
    confidence = _format_confidence(state.confidence)
    header = (
        f"HTF={state.htf_bias} local={state.local_bias} "
        f"mode={state.structure_mode} conf={confidence}"
    )
    levels_line = _render_levels_line(pair, state)
    liquidity_line = _render_liquidity_line(pair, state)
    reaction_line = (
        f"reaction={state.current_reaction} "
        f"acceptance={state.acceptance_state}"
    )
    return "\n".join([header, levels_line, liquidity_line, reaction_line])


def _render_short_text(state: StructureState) -> str:
    """One-line summary used inside Phase 9 coalesce bullets.

    Pair shows up in the bullet body because the coalescer's header
    only carries pair when every bullet is the same pair — and
    HOURLY_SUMMARY for two pairs in the same 30s window is structured
    as two different coalesce keys (different pair). So pair is in
    the header naturally; the bullet body just carries the digest.
    """
    confidence = _format_confidence(state.confidence)
    return (
        f"HTF={state.htf_bias} mode={state.structure_mode} "
        f"conf={confidence}"
    )


def _render_levels_line(pair: str, state: StructureState) -> str:
    support = _format_level_token("support", pair, state.nearest_support)
    resistance = _format_level_token(
        "resistance", pair, state.nearest_resistance,
    )
    return f"{support}  {resistance}"


def _render_liquidity_line(pair: str, state: StructureState) -> str:
    above = _format_price_or_dash(pair, _level_price(state.liquidity_above))
    below = _format_price_or_dash(pair, _level_price(state.liquidity_below))
    return f"liq_above={above}  liq_below={below}"


def _format_level_token(
    label: str, pair: str, level: Optional[StructureLevel],
) -> str:
    if level is None:
        return f"{label}=—"
    price = _format_price_or_dash(pair, level.price)
    score = level.score
    if isinstance(score, float) and math.isnan(score):
        return f"{label}={price}"
    return f"{label}={price} (s={score:.1f})"


def _format_price_or_dash(pair: str, price: Optional[float]) -> str:
    if price is None:
        return "—"
    if isinstance(price, float) and math.isnan(price):
        return "—"
    if pair.upper().endswith("JPY"):
        return f"{price:.3f}"
    return f"{price:.5f}"


def _format_confidence(confidence: float) -> str:
    if isinstance(confidence, float) and math.isnan(confidence):
        return "—"
    return f"{confidence:.2f}"


def _level_price(level: Optional[StructureLevel]) -> Optional[float]:
    if level is None:
        return None
    return level.price


# ---------------------------------------------------------------------------
# Hour-bucket parser
# ---------------------------------------------------------------------------


def _hour_bucket(timestamp_iso: str) -> str:
    """Truncate ``state.timestamp`` (engine ISO string) to the hour.

    ``"2026-05-16T09:05:00+00:00"`` → ``"2026-05-16T09:00:00+00:00"``.

    Robust to a few minute / second offsets — at minute=0 in
    practice, but a bar arriving at 09:00:30 still buckets to 09:00.

    If the timestamp string fails to parse (e.g., empty from an
    invalid-state record), returns the input verbatim. The dedupe
    key is then bar-stable but not hour-rolling — acceptable for the
    edge case.
    """
    if not timestamp_iso:
        return timestamp_iso
    try:
        parsed = datetime.fromisoformat(timestamp_iso)
    except ValueError:
        return timestamp_iso
    rounded = parsed.replace(minute=0, second=0, microsecond=0)
    return rounded.isoformat()


__all__ = ["build_hourly_summary"]
