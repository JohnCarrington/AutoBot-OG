# risk

Pre-trade gates and circuit breakers.

## Owns
- Spread filter (per-pair max acceptable spread)
- Position caps: max 2 concurrent, 1 per pair, 1 per regime
- Daily drawdown stop at −3R
- 4-consecutive-loss cooldown (4 hours)
- News blackout (±15 minutes around scheduled events)
- Per-trade R sizing inputs (stop distance → units)

## Sub-packages
- `news_calendar/` — Finnhub-backed economic calendar (ported from legacy
  AutoBot `te_calendar.py`). Owns:
  - HTTP fetch (`finnhub_client.py`)
  - cache + public lookup (`calendar.py`)
  - event-name + country matching (`matcher.py`, incl. the 03ac162 fix)
  - impact severity + actual-vs-forecast helpers (`impact.py`)

## Does NOT own
- Strategy signals (`strategies/`)
- Order placement / SL amendment (`execution/`)
- The blackout decision logic itself — that lives in this module and
  *consumes* `news_calendar/`. Phase 5 will wire them together.
