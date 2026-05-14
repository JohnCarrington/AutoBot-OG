# feed

Market data ingress: IG broker connectivity and the candle archive.

## Owns
- IG REST client (auth, session refresh, account/market metadata)
- Lightstreamer streaming subscription (live ticks / candles)
- Candle archive: backfill, persistence, gap repair
- Timeframe assembly (M5, H1, etc.) from raw ticks if needed

## Does NOT own
- Indicator computation (`indicators/`)
- Order placement or position queries used for risk decisions (`execution/`)
- Storage of strategy or regime state
