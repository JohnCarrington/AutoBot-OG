# strategies

The three v1 strategies. Each is a stateless function gated to a single
regime; the dispatcher routes the engine's current regime to exactly
one detector.

## Owns
- **Bollinger Reclaim** — RANGE regime mean-reversion, fixed TP at BB midline.
- **EMA Continuation** — TREND regime pullback continuation, structure trail (TP=None).
- **Liquidity Sweep** — VOLATILE regime sweep-reversal, structure trail (TP=None).
- Entry signal generation per strategy (3-bar M5 pattern detection).
- `Signal` emission contract (frozen dataclass).
- Session predicates (`london_session`, `ny_session`, `london_ny_overlap`).

## Public surface
- `Signal` — frozen dataclass with enum-typed `direction` / `regime`.
- `StrategyName` — `Literal["bb_reclaim", "ema_continuation",
  "liquidity_sweep"]`.
- `detect_bb_reclaim`, `detect_ema_continuation`,
  `detect_liquidity_sweep` — per-strategy detectors. Same signature:
  `(df_m5, df_h1, regime_state, pair, current_time) → Optional[Signal]`.
- `detect_all_setups` — dispatcher; returns `list[Signal]` (length 0
  or 1 in v1).
- `compute_invalid_after` — `source_ts + M5_BAR_MINUTES` minutes.
- `london_session`, `ny_session`, `london_ny_overlap` — DST-aware
  session predicates via `zoneinfo`.

## Sub-modules
- `signal.py` — `Signal` + `StrategyName` + `compute_invalid_after`.
- `constants.py` — env-overridable strategy tunables (ATR mults,
  confidence bands, EMA pullback tolerance, sweep swing age, M5 cadence).
- `sessions.py` — Europe/London + America/New_York time-window
  predicates.
- `bb_reclaim.py` / `ema_continuation.py` / `liquidity_sweep.py` —
  one strategy per module; cross-import-free.
- `dispatcher.py` — `detect_all_setups`.

## Does NOT own
- Regime decision (`regime/`).
- Risk gates (`risk/`).
- Order placement (`execution/`).
- Trade management (BE-at-1R, structure trail) — Phase 6.
- EOD close policy — strategies declare *intent* via `regime` field
  on `Signal`; the policy is enforced in `risk/rules/eod_enforcement.py`.
- Pip math — lives in `config/pair_config.py` (`pip_to_price`,
  `price_to_pips`, `pip_size_for`).
