# execution

Order placement and live-position management.

## Owns
- Trade executor: market / limit order placement via IG
- Broker-side stop-loss amendment (trailing updates)
- Single-position invariant enforcement at the broker boundary
- Fill confirmation and idempotency handling
- EOD close enforcement per the strategy's hold policy

## Does NOT own
- Whether to enter — that's `strategies/` gated by `risk/`
- Where to trail to — `structure/` provides swings; strategies define rules
- Telegram notifications (`alerts/`)
