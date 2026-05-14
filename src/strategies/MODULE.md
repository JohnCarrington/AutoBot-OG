# strategies

The three v1 strategies, each gated to a single regime.

## Owns
- **Bollinger Reclaim** — RANGE regime, fixed TP
- **EMA Continuation** — TREND regime, structure-based trailing stop
- **Liquidity Sweep** — VOLATILE / REVERSAL regime, hybrid TP+trail
- Entry signal generation per strategy
- Per-strategy exit rules (TP / trail style)

## Does NOT own
- Regime decision (`regime/`)
- Risk gates: spread filter, position caps, DD stop, news blackout (`risk/`)
- Order placement (`execution/`)
- EOD close policy — strategies declare *intent* (range/sweep always flat at NY close, trend can hold Mon–Thu if profitable); the policy is enforced in `bot/` or `execution/`.
