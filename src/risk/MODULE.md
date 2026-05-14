# risk

Pre-trade gates and circuit breakers.

## Owns
- Spread filter (per-pair max acceptable spread)
- Position caps: max 2 concurrent, 1 per pair, 1 per regime
- Daily drawdown stop at −3R
- 4-consecutive-loss cooldown (4 hours)
- News blackout (±15 minutes around scheduled events)
- Per-trade R sizing inputs (stop distance → units)

## Does NOT own
- Strategy signals (`strategies/`)
- Order placement / SL amendment (`execution/`)
- News calendar sourcing — consumes a feed; sourcing TBD.
