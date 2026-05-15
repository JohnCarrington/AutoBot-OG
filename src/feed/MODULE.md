# feed

Market data ingress: IG broker connectivity (REST + Lightstreamer) and
the M5 candle archive.

## Owns

- **Phase 6** — `ig_rest/`: IG REST client (auth, session refresh,
  positions, deal confirmation, historical prices, allowance gate).
- **Phase 7** — live M5 feed:
  - `types.py` — `Candle`, `FeedEvent`, `FeedEventKind`.
  - `constants.py` — backfill / freshness / buffer / gap-fill tunables;
    locked Lightstreamer adapter + field list (probe-verified
    `CHART:{epic}:5MINUTE`, MERGE, 12 fields).
  - `rolling_buffer.py` — bounded deque of recent candles per pair,
    thread-safe, exposes `to_dataframe()` for indicator pipelines.
  - `archive.py` — append-only CSV per pair at
    `data/candles/{pair}_5m.csv`; file-tail `_last_ts` dedup; corrupt
    row recovery on load.
  - `lightstreamer/` — `parsers.py` (stateless payload → `Candle`,
    `is_bar_close`) and `client.py` (`LightstreamerSubscriber` over
    `lightstreamer.client`).
  - `hydration.py` — cold-start cache-first hydration, REST top-up
    only when cache is stale or short, parallel across pairs.
  - `feed_manager.py` — orchestrator: hydrate → subscribe → dispatch
    `FeedEvent` callbacks. Two-layer dedup
    (`BAR_UPDATE`/`BAR_CLOSE` decision + archive `_last_ts`).
    `FEED_STALE` on LS drop, `FEED_RESUMED` + bounded
    `GAP_FILLED` on reconnect.

## Does NOT own

- Indicator computation (`indicators/`).
- Order placement or position queries used for risk decisions
  (`execution/`).
- Storage of strategy or regime state.
- Higher-timeframe assembly (H1, H4) — Phase 7 is M5-only.

## Bounded REST budget

Cold-start hydration is the only path that issues REST history
requests. Default budget: `FEED_BACKFILL_BARS = 100` per pair × 4
pairs = 400 historical points on a clean start (4% of IG's ~10k
daily allowance). Reconnect gap-fill is bounded to
`FEED_GAP_FILL_WINDOW_MIN = 30` minutes; outages beyond that are
logged and skipped — no surprise REST storms.

**If tempted to add a REST poll loop, don't.** Use Lightstreamer.

## Probe reference

The Lightstreamer adapter and field list are locked from
`scripts/probe_lightstreamer_output_2026-05-15.json` (run against
the IG demo endpoint, GBPUSD `CS.D.GBPUSD.TODAY.IP`). The
subscription was accepted and a sample payload was received within
the probe window. Re-probe before changing the field list.
