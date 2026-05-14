# risk

Pre-trade gates and circuit breakers. Phase 4 ships the composing
orchestrator `RiskGuard` and its five entry-gate rules plus EOD-close
enforcement; Phase 4-A previously shipped the Finnhub economic calendar
this module consumes.

## Owns
- Spread filter (per-pair max acceptable spread, abs-cap + ATR-relative)
- Position caps: max 2 concurrent global, 1 per pair, 1 per regime
- Daily drawdown stop at −3R, resets at NY close
- 4-consecutive-loss cooldown (4 hours)
- Regime-instability cooldown (commits > 3 OR M5 resets > 5 in 60 min →
  1 hr pause, extended until regime is live at next H1 close)
- News blackout (±15 min around scheduled HIGH/MEDIUM events)
- End-of-day enforcement (pre-EOD entry suppression + per-regime
  force-close policy)

## Public surface
- `RiskGuard` — orchestrator (composes rules in short-circuit order)
- `OpenPosition`, `AccountState`, `CandidateTrade`, `MarketSnapshot` —
  caller-supplied inputs
- `RiskDecision`, `RuleResult`, `ForceCloseOrder` — outputs
- `CircuitBreakerState` — persisted state at
  `data/risk/circuit_breaker_state.json`
- Individual rule functions are also re-exported for tests and advanced
  consumers (`check_circuit_breakers`, `check_position_caps`, etc.).

## Sub-packages
- `news_calendar/` — Finnhub-backed economic calendar (ported from
  legacy AutoBot `te_calendar.py`). Owns HTTP fetch
  (`finnhub_client.py`), cache + public lookup (`calendar.py`),
  event-name + country matching (`matcher.py`, incl. the 03ac162 fix),
  impact severity + actual-vs-forecast helpers (`impact.py`).
- `rules/` — one module per rule (`spread_filter`, `position_caps`,
  `news_blackout`, `circuit_breakers`, `eod_enforcement`). Each rule is
  a pure function returning `RuleResult`. The orchestrator imports each
  rule directly so an import-time failure localises to its module.
- `state/` — persisted state objects. v1 contains
  `circuit_breaker_state.py` only.
- `constants.py` — risk-layer tunables. All values default to the
  locked v1 spec (`docs/v1_architecture.md` §6) and are
  env-var-overridable per the `pair_config.py` pattern.
- `types.py` — frozen dataclasses for the rule pipeline.

## Does NOT own
- Strategy signals (`strategies/`)
- Order placement / SL amendment (`execution/`)
- Per-pair pip-floor SL sizing (out of scope for Phase 4 — Phase 5
  consumes `config/pair_config.py::MIN_SL_PIPS` directly).
- Realised-PnL ledger — the caller maintains `account.realized_pnl_today_r`
  and feeds it to `allow_entry`. The circuit-breaker state only
  persists cooldown timestamps and the loss-streak counter.
