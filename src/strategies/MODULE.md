# strategies

Stateless pattern detectors gated by day-type. The dispatcher routes
the day_type from :py:func:`day_type.classify_day_type` to a tuple of
detectors per the dispatch table.

## Owns
- **BB Bounce** — NORMAL day-type mean-reversion, fixed TP at the
  opposite zone midpoint.
- **EMA Pullback** — BIG_NEWS_DAY / PRE_BIG_NEWS continuation, structure
  trail (TP=None).
- Entry signal generation per strategy.
- `Signal` emission contract (frozen dataclass).
- Session predicates (`london_session`, `ny_session`, `london_ny_overlap`).

`detect_news` (step 3) and `detect_structure_break` (step 5) live as
stubs in `dispatcher.py` until their respective steps land.

## Public surface
- `Signal` — frozen dataclass with `direction: Direction` and
  `day_type: DayType`.
- `StrategyName` — closed Literal: `"bb_bounce"`, `"ema_pullback"`,
  `"news"`, `"structure_break"`.
- `detect_bb_bounce`, `detect_ema_pullback` — per-strategy detectors.
  Signature: `(df_m5, df_h1, day_type, structure_state, pair, current_time) → Optional[Signal]`.
- `detect_all_setups` — dispatcher; returns `list[Signal]` (may be
  multi-element on `BIG_NEWS_DAY` / `PRE_BIG_NEWS`).
- `compute_invalid_after` — `source_ts + M5_BAR_MINUTES` minutes.
- `london_session`, `ny_session`, `london_ny_overlap` — DST-aware
  session predicates via `zoneinfo`.

## Sub-modules
- `signal.py` — `Signal` + `StrategyName` + `compute_invalid_after`.
- `constants.py` — env-overridable strategy tunables.
- `sessions.py` — Europe/London + America/New_York time-window
  predicates.
- `bb_bounce.py` / `ema_pullback.py` — one strategy per module;
  cross-import-free.
- `dispatcher.py` — `detect_all_setups` + DISPATCH table +
  step-3/step-5 stub detectors.

## Does NOT own
- Day-type classification (`day_type/`).
- Regime decision (`regime/` — still consumed by EOD / risk rules
  until step 2c/2d).
- Risk gates (`risk/`).
- Order placement (`execution/`).
- Trade management (BE-at-1R, structure trail) — Phase 6.
- EOD close policy — strategies declare *intent* via `day_type` field
  on `Signal`; the policy is enforced in `risk/rules/eod_enforcement.py`.
- Pip math — lives in `config/pair_config.py`.
