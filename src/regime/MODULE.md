# regime

Market-regime classification: TREND / RANGE / VOLATILE.

## Owns
- H1 primary regime classifier
- M5 validation layer (3 agreeing closes)
- Hysteresis to prevent flip-flopping (1 H1 close + 3 M5 agreement)
- Indicator hierarchy: structure > EMA slope > BB width > MACD
- Current-regime state and transitions

## Does NOT own
- The indicators themselves (`indicators/`)
- Structure detection (`structure/`)
- Strategy selection per regime (handled in `bot/` or `strategies/` dispatch)
- Trade decisions — emits regime only.
