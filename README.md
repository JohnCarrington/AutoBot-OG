# AutoBot-OG

A systematic FX trading bot (v1, GBPUSD-only) running three regime-gated strategies — Bollinger Reclaim (range), EMA Continuation (trend), and Liquidity Sweep (volatile) — over an H1-primary / M5-validated regime engine with hysteresis, structure-aware trade management, and risk gates (spread, position caps, daily DD stop, loss cooldown, news blackout).

See `docs/v1_architecture.md` for the full design.
