# indicators

Pure technical-indicator math.

## Owns
- EMA (with slope variants)
- Bollinger Bands (period 20, 2σ) and BB width
- ATR (period 14)
- MACD (12, 26, 9)
- ATR-normalised forms (e.g. EMA slope / ATR, BB width / ATR)

## Does NOT own
- Candle data sourcing (`feed/`)
- Swing / fractal structure (`structure/`)
- Regime decisions that compose indicators (`regime/`)
- Any I/O, broker calls, or persistence — functions here must be pure.
