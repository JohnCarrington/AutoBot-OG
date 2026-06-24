"""News strategy (BIG_NEWS_DAY only) — step 5b.

This is the fourth strategy. Unlike ema_pullback / structure_break,
which take direction from ``structure_state.htf_bias``, ``detect_news``
takes direction from the **data surprise** — the released actual vs
the forecast — so it can trade COUNTER-trend when the data warrants.

Pipeline (per BAR_CLOSE on BIG_NEWS_DAY):
    1. Find a HIGH-impact release for the pair's currencies whose
       release time falls inside the current "release window"
       (``[release - NEWS_WINDOW_PRE_MIN, release + NEWS_WINDOW_POST_MIN]``).
    2. Read its ``actual`` and ``estimate``; skip if ``actual is None``
       (release scheduled but not yet published).
    3. Compute the deviation via
       :py:func:`risk.news_calendar.impact.compute_deviation`. In-line
       (``|deviation| <= threshold``) → no signal.
    4. Map (event_name, deviation) → pair direction via
       :py:func:`strategies.news_direction.direction_for_release`,
       handling the per-event sign convention for inverted releases.
    5. Take the structure-anchored entry IN THAT DIRECTION using the
       shared ``_structure_entry`` helpers (``build_sl``, MACD
       confidence). Direction comes from data, NOT ``htf_bias``.
    6. Emit ``Signal(strategy_name="news", day_type, direction, …)``.

Window suppression vs structure_break / ema_pullback is owned by the
dispatcher (``strategies.dispatcher``); inside the release window only
``detect_news`` runs, outside it only the structure detectors do. So
when ``detect_news`` is called by the dispatcher, the window
membership is already confirmed — this function does not re-gate on
the window itself, only verifies a published release with a real
surprise exists for the pair's currencies.

Out of scope here:
    - Polling for actuals — done in ``bot.loop._handle_bar_close``
      (step 4.5).
    - The dispatcher decision to mute structure detectors — done in
      ``strategies.dispatcher``.
    - Threshold tuning — owned by
      :data:`risk.news_calendar.impact.DEVIATION_THRESHOLD`.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd

from common import Direction
from day_type import DayType
from risk.news_calendar import Impact, events_in_window, parse_event_time
from risk.news_calendar.impact import compute_deviation
from risk.rules.news_blackout import _currencies_for
from structure_engine import StructureLevel, StructureState

from ._structure_entry import (
    build_sl,
    latest_atr,
    latest_close,
    latest_timestamp,
    macd_aligned_confidence,
)
from .constants import STRUCT_BREAK_CONF_HIGH, STRUCT_BREAK_CONF_LOW
from .management import profile_for
from .news_direction import direction_for_release
from .news_window import NEWS_WINDOW_POST_MIN, NEWS_WINDOW_PRE_MIN
from .signal import Signal, compute_invalid_after


logger = logging.getLogger(__name__)


_STRATEGY_NAME = "news"


# Reverse map: Finnhub country → ISO currency. The pair's currency set
# tells us which countries we care about; this map lets us pick the
# right currency to feed to ``direction_for_release`` from a matched
# event's ``country`` field. EUR composite / member-state countries
# all map to "EUR".
_CURRENCY_FOR_COUNTRY: dict[str, str] = {
    "US": "USD",
    "GB": "GBP",
    "EU": "EUR",
    "DE": "EUR",
    "FR": "EUR",
    "IT": "EUR",
    "ES": "EUR",
    "NL": "EUR",
    "JP": "JPY",
    "CA": "CAD",
}


def detect_news(
    df_m5: pd.DataFrame,
    df_h1: pd.DataFrame,
    day_type: DayType,
    structure_state: StructureState,
    pair: str,
    current_time: datetime,
) -> Optional[Signal]:
    """Emit a Signal when a published HIGH-impact release surprised in
    a direction that has a usable structure anchor.

    Returns ``None`` when:
      - the structure-engine snapshot is invalid;
      - no HIGH-impact release for the pair's currencies is inside the
        release window at ``current_time``;
      - the matching release has not yet published its ``actual``;
      - the surprise is in-line (below threshold);
      - the data-implied direction has no usable structure anchor
        (``nearest_resistance`` for BULLISH; ``nearest_support`` for
        BEARISH).
    """
    if not structure_state.is_valid:
        return None

    currencies = _currencies_for(pair)
    if not currencies:
        return None

    chosen = _pick_release(current_time, currencies)
    if chosen is None:
        return None
    ev, release_dt = chosen

    actual = ev.get("actual")
    estimate = ev.get("estimate")
    if actual is None or estimate is None:
        return None
    try:
        actual_f = float(actual)
        estimate_f = float(estimate)
    except (TypeError, ValueError):
        return None
    if estimate_f == 0:
        return None

    dev_payload = compute_deviation(actual_f, estimate_f)
    deviation = dev_payload.get("deviation")
    if deviation is None:
        return None

    release_currency = _CURRENCY_FOR_COUNTRY.get(ev.get("country", ""))
    if release_currency is None or release_currency not in currencies:
        return None

    event_name = str(ev.get("event", ""))
    direction = direction_for_release(
        pair, release_currency, event_name, deviation,
    )
    if direction is None:
        return None

    anchor_level = _anchor_for_direction(direction, structure_state)
    if anchor_level is None:
        return None
    if direction == Direction.BULLISH:
        anchor_price = anchor_level.zone_low
    else:
        anchor_price = anchor_level.zone_high

    atr_m5 = latest_atr(df_m5)
    if math.isnan(atr_m5) or atr_m5 <= 0:
        return None

    entry_price = latest_close(df_m5)
    if math.isnan(entry_price):
        return None

    profile = profile_for(_STRATEGY_NAME, day_type)
    sl_price = build_sl(
        direction=direction,
        anchor_price=anchor_price,
        atr_m5=atr_m5,
        pair=pair,
        sl_atr_mult=profile.sl_atr_mult,
        sl_floor_pips_override=profile.sl_floor_pips_override,
    )
    confidence = macd_aligned_confidence(
        direction=direction, df_h1=df_h1,
        high=STRUCT_BREAK_CONF_HIGH, low=STRUCT_BREAK_CONF_LOW,
    )
    source_ts = latest_timestamp(df_m5)
    if source_ts is None:
        return None

    debug: dict[str, Any] = {
        "release_event": event_name,
        "release_country": ev.get("country"),
        "release_currency": release_currency,
        "release_time": ev.get("time"),
        "release_actual": actual_f,
        "release_estimate": estimate_f,
        "release_deviation": float(deviation),
        "release_beat_miss": dev_payload.get("beat_miss"),
        "data_direction": direction.value,
        "htf_bias": structure_state.htf_bias,
        "anchor_level_price": anchor_level.price,
        "anchor_level_score": anchor_level.score,
        "atr_m5": float(atr_m5),
        "window_pre_min": NEWS_WINDOW_PRE_MIN,
        "window_post_min": NEWS_WINDOW_POST_MIN,
    }

    return Signal(
        pair=pair.upper(),
        direction=direction,
        day_type=day_type,
        strategy_name=_STRATEGY_NAME,
        suggested_entry_price=entry_price,
        suggested_sl_price=sl_price,
        suggested_tp_price=None,
        confidence_score=confidence,
        source_candle_ts=source_ts,
        invalid_after_candle_ts=compute_invalid_after(source_ts),
        debug=debug,
    )


def _pick_release(
    current_time: datetime, currencies: tuple[str, ...],
) -> Optional[tuple[dict[str, Any], datetime]]:
    """Return the (event, release_dt) whose window contains ``current_time``.

    Searches HIGH-impact releases for ``currencies`` whose release time
    is within ``[current_time - NEWS_WINDOW_POST_MIN,
    current_time + NEWS_WINDOW_PRE_MIN]`` (i.e. the release fired up
    to POST_MIN ago, or fires up to PRE_MIN ahead — the symmetric
    inverse of the window-check semantics).

    When multiple events match, the **closest in time** to
    ``current_time`` wins — typically a clustered release slot (e.g.
    five US economic figures at 12:30 UTC) yields a single dominant
    candidate per slot.
    """
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    lookback = current_time - timedelta(minutes=NEWS_WINDOW_POST_MIN)
    lookahead = current_time + timedelta(minutes=NEWS_WINDOW_PRE_MIN)
    candidates = events_in_window(
        currencies=currencies,
        start_utc=lookback,
        end_utc=lookahead,
        impact_min=Impact.HIGH,
    )
    best: Optional[tuple[dict[str, Any], datetime]] = None
    best_delta: Optional[float] = None
    for ev in candidates:
        release_dt = parse_event_time(ev.get("time"))
        if release_dt is None:
            continue
        # window contract: current_time in [release - PRE, release + POST]
        window_start = release_dt - timedelta(minutes=NEWS_WINDOW_PRE_MIN)
        window_end = release_dt + timedelta(minutes=NEWS_WINDOW_POST_MIN)
        if not (window_start <= current_time <= window_end):
            continue
        delta = abs((release_dt - current_time).total_seconds())
        if best is None or (best_delta is not None and delta < best_delta):
            best = (ev, release_dt)
            best_delta = delta
    return best


def _anchor_for_direction(
    direction: Direction, state: StructureState,
) -> Optional[StructureLevel]:
    if direction == Direction.BULLISH:
        return state.nearest_resistance
    if direction == Direction.BEARISH:
        return state.nearest_support
    return None


__all__ = ["detect_news"]
