"""Feed-layer tunables (Phase 7).

Values follow the same env-override pattern as :py:mod:`risk.constants`
and :py:mod:`execution.constants`: the default is the locked v1 value,
and an environment variable matching the constant name lets ops bump
it without a deploy. Constants are read once at import time — a
runtime env change does not propagate.

Locked decisions
----------------

- ``FEED_BACKFILL_BARS = 100`` — enough for every v1 indicator to seed
  (the slowest is MACD's 26-EMA which converges by ~75 bars). 100 ×
  4 pairs = 400 historical calls on cold start, which is < 5% of
  IG's ~10,000-point daily allowance.
- ``FEED_BUFFER_CAPACITY = 600`` — 50 hours of 5-minute bars,
  comfortably covering any single strategy lookback (longest is the
  H1 EMA composition, ~200 H1 bars ≈ 200/12 = 17 H1 bars worth of M5
  context if rolled up).
- ``FEED_FRESHNESS_THRESHOLD_MIN = 60`` — if the newest cached bar is
  within an hour and there are at least ``FEED_BACKFILL_BARS`` rows
  in the cache, skip the REST top-up entirely. The strategy layer
  cannot trade on a stale-by-an-hour book anyway (Phase 4 spread /
  EOD rules will reject it), so eating the REST quota to refresh is
  wasteful.
- ``FEED_GAP_FILL_WINDOW_MIN = 30`` — on reconnect, only request the
  REST gap if it's less than 30 minutes. A longer outage means
  something operational happened (overnight maintenance, network
  partition, IG outage), and silently back-filling a multi-hour gap
  risks injecting bars the strategy layer would otherwise have
  skipped (e.g. NY close gap, weekend cross).
- ``FEED_WATCHDOG_STALE_SEC = 600`` — per-pair last-update watchdog.
  If no LS update arrives within 10 minutes during market hours,
  emit ``FEED_STALE``. The threshold is 2× the bar period to absorb
  legitimately-quiet bars (LTV=0 mid-session) without firing.

Locked from the probe (``scripts/probe_lightstreamer_output_2026-05-15.json``):

- ``LIGHTSTREAMER_CANDLE_ITEM_TEMPLATE = "CHART:{epic}:5MINUTE"`` —
  verified on IG demo against ``CS.D.GBPUSD.TODAY.IP``; subscription
  accepted and first payload contained every requested field. The
  Lightstreamer *adapter set* is the separate ``"DEFAULT"`` string we
  pass to :class:`LightstreamerClient` — this constant is the item
  template, not the adapter set. (L1, follow-up cleanup 2026-05-15.)
- ``LIGHTSTREAMER_CANDLE_MODE = "MERGE"`` — the only mode IG accepts
  for CHART items.
- ``LIGHTSTREAMER_CANDLE_FIELDS`` — exactly the 12-field list the probe
  validated. Do not add fields without re-probing.

If tempted to add a REST poll loop, **don't**. Use Lightstreamer.
Cold-start hydration is the *only* place we burn REST quota for
historical bars; everything else is live-streamed.
"""
from __future__ import annotations

import os


def _f(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw is not None else default


def _i(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw is not None else default


def _s(name: str, default: str) -> str:
    raw = os.getenv(name)
    return raw if raw is not None else default


# --- Hydration / buffer ----------------------------------------------------
FEED_BACKFILL_BARS: int = _i("FEED_BACKFILL_BARS", 100)
FEED_BUFFER_CAPACITY: int = _i("FEED_BUFFER_CAPACITY", 600)
FEED_FRESHNESS_THRESHOLD_MIN: int = _i("FEED_FRESHNESS_THRESHOLD_MIN", 60)

# When REST top-up fails on cold start but the cache holds at least
# this many bars, hydrate from cache alone and mark the pair
# ``cache_only_degraded`` instead of ``failed``. Default 50 covers the
# slowest v1 indicator seed (MACD 26-EMA converges ~75 bars but
# tolerates partial state for a small number of bars before being
# trustworthy). Lower than this means strategies can't make decisions
# anyway, so we surface the failure instead of pretending we have data.
FEED_MIN_USABLE_BARS: int = _i("FEED_MIN_USABLE_BARS", 50)

# --- Reconnect / gap fill --------------------------------------------------
FEED_GAP_FILL_WINDOW_MIN: int = _i("FEED_GAP_FILL_WINDOW_MIN", 30)

# --- Watchdog --------------------------------------------------------------
# Per-pair "no-update" alert threshold during market hours. 2× the M5
# period absorbs legitimately-quiet bars.
FEED_WATCHDOG_STALE_SEC: int = _i("FEED_WATCHDOG_STALE_SEC", 600)

# --- Lightstreamer subscription (probe-locked) -----------------------------
# ITEM_TEMPLATE, not adapter set — see L1 in the Phase 7 follow-up.
LIGHTSTREAMER_CANDLE_ITEM_TEMPLATE: str = "CHART:{epic}:5MINUTE"
LIGHTSTREAMER_CANDLE_MODE: str = "MERGE"
LIGHTSTREAMER_CANDLE_FIELDS: tuple[str, ...] = (
    "UTM",
    "LTV",
    "CONS_TICK_COUNT",
    "CONS_END",
    "BID_OPEN",
    "BID_HIGH",
    "BID_LOW",
    "BID_CLOSE",
    "OFR_OPEN",
    "OFR_HIGH",
    "OFR_LOW",
    "OFR_CLOSE",
)

# Endpoint selection mirrors the probe and the legacy streamer: IG
# routes demo vs. live to different LS hosts and the SDK does not
# auto-detect.
LIGHTSTREAMER_ENDPOINT_BY_ACC: dict[str, str] = {
    "LIVE": "https://apd.marketdatasystems.com",
    "DEMO": "https://demo-apd.marketdatasystems.com",
}

# --- Archive ---------------------------------------------------------------
# One CSV per pair under ``data/candles/`` (gitignored). Override the
# directory via env if a different mount is preferred.
FEED_ARCHIVE_DIR: str = _s("FEED_ARCHIVE_DIR", "data/candles")
FEED_ARCHIVE_CSV_TEMPLATE: str = "{pair}_5m.csv"

# H1 archive uses the same dir + schema; only the filename suffix
# differs so an operator can `ls data/candles/` and tell M5 from H1
# at a glance. CandleArchive accepts a custom template via its
# ``template=`` constructor arg — see Phase B Commit 1.
FEED_ARCHIVE_CSV_TEMPLATE_H1: str = "{pair}_1h.csv"


# --- H1 hydration (Phase B) -------------------------------------------------
# Independent H1 backfill so the regime classifier (EMA-50 + 10-bar
# slope = 60 H1 bars minimum) is non-TRANSITION from the first live
# BAR_CLOSE. Flag-gated; default OFF so the M5-resample fallback
# remains the active path until ops verifies parity per the design
# doc §7.2.
FEED_H1_HYDRATION_ENABLED: bool = _i("FEED_H1_HYDRATION_ENABLED", 0) != 0

# Per-pair H1 REST budget (bars fetched on cold start). 72 = 60
# classifier minimum + 12 headroom. Sits well inside the per-pair
# ~120-bar ceiling the design doc constrains us to.
FEED_H1_BACKFILL_BARS: int = _i("FEED_H1_BACKFILL_BARS", 72)

# RollingBuffer capacity for the H1 buffer. Matches the backfill
# budget so a cold start fills the buffer exactly. Hard floor 60
# enforced at FeedManager construction.
FEED_H1_BUFFER_CAPACITY: int = _i("FEED_H1_BUFFER_CAPACITY", 72)

# Below this count the bot loop falls back to the legacy
# M5-resample H1 derivation, even when the flag is on. Protects the
# new-market / illiquid-epic case (R1 in the design doc risk scan).
FEED_H1_MIN_USABLE_BARS: int = _i("FEED_H1_MIN_USABLE_BARS", 60)

# Schema written to every archive CSV. ``close_time`` is the ISO-8601
# UTC string ("2026-05-15T13:00:00+00:00"); ``close_time_ms`` is the
# integer epoch-milliseconds copy used by the file-tail dedup check
# without a parser round-trip.
FEED_ARCHIVE_COLUMNS: tuple[str, ...] = (
    "close_time",
    "close_time_ms",
    "open",
    "high",
    "low",
    "close",
    "volume",
)


# --- Market-hours guard ----------------------------------------------------
# Phase 7 only consumes this for the watchdog gating: do not emit
# FEED_STALE during weekend / FX-close hours. The trading hours
# enforcement itself lives in ``risk.rules.eod_enforcement``.
#
# Stored as a tuple of (weekday_start, weekday_end) ISO weekday numbers
# where Monday=1, Sunday=7. The FX market is conventionally open
# Sunday ~22:00 UTC → Friday ~22:00 UTC; we treat the entire span as
# "market open" for the watchdog and let the risk layer enforce
# finer-grained session bans.
MARKET_HOURS_GUARDS: dict[str, tuple[int, int]] = {
    # ISO weekday: Mon=1 ... Sun=7. We're "open" Sunday (7) → Friday (5).
    "OPEN_WEEKDAYS": (7, 5),
}


__all__ = [
    "FEED_ARCHIVE_COLUMNS",
    "FEED_ARCHIVE_CSV_TEMPLATE",
    "FEED_ARCHIVE_CSV_TEMPLATE_H1",
    "FEED_ARCHIVE_DIR",
    "FEED_BACKFILL_BARS",
    "FEED_BUFFER_CAPACITY",
    "FEED_FRESHNESS_THRESHOLD_MIN",
    "FEED_GAP_FILL_WINDOW_MIN",
    "FEED_H1_BACKFILL_BARS",
    "FEED_H1_BUFFER_CAPACITY",
    "FEED_H1_HYDRATION_ENABLED",
    "FEED_H1_MIN_USABLE_BARS",
    "FEED_MIN_USABLE_BARS",
    "FEED_WATCHDOG_STALE_SEC",
    "LIGHTSTREAMER_CANDLE_FIELDS",
    "LIGHTSTREAMER_CANDLE_ITEM_TEMPLATE",
    "LIGHTSTREAMER_CANDLE_MODE",
    "LIGHTSTREAMER_ENDPOINT_BY_ACC",
    "MARKET_HOURS_GUARDS",
]
