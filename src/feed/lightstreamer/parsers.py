"""Stateless payload → :class:`Candle` conversion (Phase 7).

The Lightstreamer SDK delivers ``IItemUpdate`` objects we read field
by field. We extract them into a plain dict in
:py:class:`feed.lightstreamer.client.LightstreamerSubscriber` and pass
the dict here. Keeping the parser pure and stateless lets the
:py:class:`feed.feed_manager.FeedManager` decide ``BAR_UPDATE`` vs
``BAR_CLOSE`` from the resulting candle without the parser needing
session memory.

What this module owns:

- ``parse_chart_payload(pair, payload)`` — build a :class:`Candle`
  from a single Lightstreamer ``CHART:{epic}:5MINUTE`` payload dict.
  The candle's ``source`` is always ``"LS_NATIVE_5M"``.
- ``is_bar_close(payload)`` — interpret the ``CONS_END`` flag, which
  is the canonical "this bar is final" signal.
- Field coercion helpers — IG returns every field as a string (the
  LS protocol is text-oriented), so we route through ``float`` /
  ``int`` with defensive ``None`` fallbacks for partial updates.

What this module does NOT own:

- Pair / epic resolution — the parser takes the symbol as an
  argument; mapping ``"CS.D.GBPUSD.TODAY.IP" → "GBPUSD"`` lives in
  ``config.pair_config.pair_from_epic``.
- Dedup or BAR_UPDATE / BAR_CLOSE selection —
  :py:class:`feed.feed_manager.FeedManager` does that.
- Volume scaling — we treat ``CONS_TICK_COUNT`` as the canonical
  volume proxy (the IG spreadbet markets don't publish notional
  volume). The strategy / regime layers can renormalise if they
  ever want to.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from ..types import Candle


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


class PartialPayloadError(ValueError):
    """Raised when a payload lacks the fields needed to construct a Candle.

    The Lightstreamer SDK delivers partial updates: a field absent
    from the latest tick keeps its previous value, but the *first*
    update on a subscription may carry only the changed fields. The
    feed manager catches this exception and skips emitting an event;
    the next update will likely carry the missing field.
    """


def parse_chart_payload(
    pair: str, payload: Mapping[str, Any]
) -> Candle:
    """Build a :class:`Candle` from a CHART:5MINUTE LS payload dict.

    ``payload`` is the dict of field-name → field-value the subscriber
    extracted via ``IItemUpdate.getValue(name)``. Every value may be a
    string (LS wire format) or ``None`` (field unchanged in this
    delta). We coerce to float/int and use the BID/OFR mid as the
    canonical OHLC for the bar — IG's CHART feed provides separate
    BID and OFR for each O/H/L/C, and trading decisions are made
    against the mid throughout the v1 stack.

    **Price scaling.** Prices are passed through *verbatim*; no
    scaling is applied. IG spreadbet and CFD instruments stream prices
    in raw broker **points** (e.g. ``"13339.2"`` for GBPUSD spreadbet
    on 2026-05-15). The legacy AutoBot's rolling-candle CSV
    (``/opt/tradingbot/cache/GBPUSD_candles_rolling.csv``) records
    these raw-point values without scaling, and the entire downstream
    stack — strategies, ATR, SL/TP arithmetic, ``pair_config`` pip
    math — has been operating on raw points for the production bot's
    lifetime. Re-introducing decimal scaling at the parser boundary
    would break every absolute-price comparison in the codebase. See
    ``scripts/probe_lightstreamer_output_2026-05-15.json`` and the
    Phase 7 adversarial review (C2) for the verification chain.

    Raises
    ------
    PartialPayloadError
        If ``UTM`` is missing, or if any of the eight BID/OFR fields
        needed to compute the mid OHLC is missing. The caller (the
        subscriber) handles this by skipping the update.
    """
    utm_raw = payload.get("UTM")
    if utm_raw in (None, ""):
        raise PartialPayloadError(
            f"parse_chart_payload({pair}): missing UTM in payload"
        )
    try:
        utm_ms = int(float(utm_raw))
    except (TypeError, ValueError) as exc:
        raise PartialPayloadError(
            f"parse_chart_payload({pair}): UTM={utm_raw!r} unparseable"
        ) from exc

    # IG sends bar OPEN time in UTM. The Candle convention is
    # close_time = open_time + 5min. Boundary handling: if UTM is
    # already on a 5-minute boundary (it should be for CHART:5MINUTE),
    # close_time is exactly 5 minutes later. We always project rather
    # than rounding because the CHART feed never delivers a mid-bar
    # UTM — only the bar-open timestamp.
    open_time = datetime.fromtimestamp(utm_ms / 1000.0, tz=timezone.utc)
    close_time = open_time.replace(microsecond=0) + _five_minutes()

    fields_mid = (
        ("open", "BID_OPEN", "OFR_OPEN"),
        ("high", "BID_HIGH", "OFR_HIGH"),
        ("low", "BID_LOW", "OFR_LOW"),
        ("close", "BID_CLOSE", "OFR_CLOSE"),
    )
    mids: dict[str, float] = {}
    # IG ships BID/OFR as raw broker points (e.g. ``"13339.2"``,
    # ``"13340.1"`` for GBPUSD spreadbet). We average to get the mid
    # in the same raw-points units and pass it through unchanged.
    # Decimal scaling is intentionally NOT applied here — see the
    # function docstring's "Price scaling" note for why.
    for label, bid_key, ofr_key in fields_mid:
        bid = _coerce_float(payload.get(bid_key))
        ofr = _coerce_float(payload.get(ofr_key))
        if bid is None or ofr is None:
            raise PartialPayloadError(
                f"parse_chart_payload({pair}): {label} missing "
                f"({bid_key}={payload.get(bid_key)!r}, "
                f"{ofr_key}={payload.get(ofr_key)!r})"
            )
        mids[label] = (bid + ofr) / 2.0

    # CONS_TICK_COUNT is the tick count over the consolidated bar; if
    # missing, fall back to LTV (last trade volume; same field IG uses
    # for tick counts on FX). A truly missing volume defaults to 0.0
    # rather than raising — the strategy layer never gates on volume
    # being non-zero in v1.
    vol = (
        _coerce_float(payload.get("CONS_TICK_COUNT"))
        or _coerce_float(payload.get("LTV"))
        or 0.0
    )

    return Candle(
        pair=pair,
        close_time=close_time,
        open=mids["open"],
        high=mids["high"],
        low=mids["low"],
        close=mids["close"],
        volume=float(vol),
        source="LS_NATIVE_5M",
    )


def is_bar_close(payload: Mapping[str, Any]) -> bool:
    """Return ``True`` when ``CONS_END=1`` in the payload.

    ``CONS_END`` is IG's canonical bar-close signal on the CHART
    feed: it flips from ``"0"`` to ``"1"`` on the tick that finalises
    the 5-minute bar, and stays at ``"1"`` for any post-close ticks
    that occasionally trail the boundary. We accept both string and
    int representations because the LS field wire format is
    text-oriented but tests sometimes seed cleaner dicts.

    A missing or unparseable ``CONS_END`` returns ``False`` — i.e.
    "treat as not closed yet". This is the safe default; the manager
    will emit a ``BAR_UPDATE`` and the next tick will resolve the
    bar's true state.
    """
    raw = payload.get("CONS_END")
    if raw is None:
        return False
    try:
        return int(float(raw)) == 1
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_float(raw: Any) -> Optional[float]:
    """Return ``raw`` as ``float``, or ``None`` if missing / unparseable.

    ``NaN`` is treated as missing — Lightstreamer occasionally
    delivers numeric strings the SDK has already parsed to ``nan``
    (e.g. when no BID has printed yet for a freshly-listed market).
    """
    if raw is None or raw == "":
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(val):
        return None
    return val


def _five_minutes():
    # Defined as a function so the import-time call is trivial; using
    # ``timedelta`` directly here avoids importing it at module scope
    # just for one constant.
    from datetime import timedelta

    return timedelta(minutes=5)


__all__ = ["PartialPayloadError", "is_bar_close", "parse_chart_payload"]
