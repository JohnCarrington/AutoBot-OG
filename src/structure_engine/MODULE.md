# structure_engine

Phase 11: Structure Engine. Converts enriched M5/M15/H1 candles into a
single `StructureState` per BAR_CLOSE that strategies consume in place
of bespoke pattern detection.

## Owns

- N-bar fractal swing detection (H1 / M15 / M5) — `swing_detector.py`
- ATR-padded zone construction + weighted-merge clustering — `zone_builder.py`
- Zone scoring (timeframe + touch + reaction + recency + liquidity + session − invalidation) — `scoring.py`
- HTF / local bias with **EMA warm-up degradation** — `bias_detector.py`
- Structure-mode classification (RANGE_BALANCE / TREND_CONTINUATION / VOLATILE_SWEEP_ZONE / TRANSITION / UNKNOWN) — `mode_classifier.py`
- Strict 3-bar reaction detection (8 reaction types) — `reaction_detector.py`
- Liquidity-pool selection above / below current price — `liquidity.py`
- `analyze_structure` orchestrator + StructureState dataclass — `structure_state.py`, `types.py`
- JSONL logging gated on `STRUCTURE_LOG_ENABLED` — `logging.py`

## Does NOT own

- Indicator math — `src/indicators/`.
- Regime classification — `src/regime/`.
- Strategy gating — `src/strategies/` (each strategy reads StructureState and applies its spec §13 gates).
- Trade execution / SL management — `src/execution/`.
- Legacy fractal swing columns used by Phase 3 regime classifier and
  Phase 6 SL trailing — those continue to live in `src/structure/`.

## Public API

```python
from structure_engine import analyze_structure, StructureState

state: StructureState = analyze_structure(
    pair="GBPUSD",
    candles_m5=df_m5_enriched,     # 50+ bars, indicators applied
    candles_m15=df_m15_enriched,   # derived from M5 by resample
    candles_h1=df_h1_enriched,     # derived from M5 by resample
    regime_state=engine.get_state(),
    session_state=None,            # stub in Phase 11
)
```

Strategies read `state.nearest_support`, `state.current_reaction`,
`state.structure_mode`, etc. per the spec §13 gate matrix.

## Locked design decisions

Cross-referenced to the Phase 11 plan + adversarial review prompts (2026-05-16, L-6 review fix):

| # | Decision | Where |
|---|---|---|
| 1 | H1 candles: derived from M5 via `df.resample("1h", label="right", closed="right")`, trim trailing partial. | `src/bot/loop.py:_derive_and_enrich_h1` |
| 2 | M15 candles: derived from M5 via `"15min"` resample, trim trailing AND leading partial (M-9). | `src/bot/loop.py:_derive_and_enrich_m15` |
| 3 | Phase 5 strategies replaced: read `StructureState`, apply §13 gates. No 3-bar pierce/reject/confirm in strategies. | `src/strategies/*.py` |
| 4 | Reactions bar-close deterministic, **3-bar strict lookback** (N-2 / N-1 / N). No intrabar reads. | `reaction_detector.py` |
| 5 | Regime label and `structure_mode` are independent. Strategies gate on both. | `mode_classifier.py` + spec §13 docstrings |
| 6 | A level can carry **both** S/R and liquidity roles (HIGH-side equal-high cluster = `RESISTANCE` + `LIQUIDITY_HIGH`). | `_wrap_levels` + `pick_liquidity_*` |
| 7 | Zone-merge weighted by timeframe score, not arithmetic mean. | `zone_builder._merge_pair` |
| 8 | **Touch = any bar whose `[low, high]` range overlaps the zone band.** Wick counts. | `structure_state._accumulate_touches` |
| 9 | EMA warm-up degradation (refinement A): HTF walks EMA200→100→50, local walks EMA50→21→13 (M-10). | `bias_detector._select_ema` |
| 10 | Confidence one-sided guard (refinement B): single-side structure not penalised. | `structure_state._compute_confidence` |
| 11 | Structure analysis runs on every BAR_CLOSE; signal gate suppresses *trades* only. | `src/bot/loop.py:_handle_bar_close` |
| 12 | Failed-reclaim beats acceptance-break on 3-bar collision. | `reaction_detector.classify_reaction` |

## Design notes

### EMA warm-up degradation (refinement A, 2026-05-16)

`bias_detector.detect_htf_bias` walks `ema_200 → ema_100 → ema_50` and
uses the first non-NaN value. The Phase 7 hydration loads 100 M5 bars,
which means EMA200 (computed on derived H1) is NaN for roughly the
first 200 H1 bars (~8 days of live operation in 24×5 markets, ~17
hours of trading-week candles depending on rollover).

Until EMA200 warms up, bias may "look weak" in the first ~17 hours of
live operation. This is **expected** — the engine records which EMA it
landed on in `StructureState.debug.htf_ema_used` so operators can
correlate weak bias with warm-up state. No hydration change is needed
for v1; EMA200 contributes once ~200 bars accumulate.

If EMA50 is also NaN (very early hydration), `htf_bias = NEUTRAL` with
`debug.reason = "insufficient_ema_data"`.

### Regime vs structure_mode (locked decision #4, 2026-05-16)

`RegimeLabel` (`TREND` / `RANGE` / `VOLATILE` / `TRANSITION`, from
`regime/labels.py`) and `StructureMode` (`TREND_CONTINUATION` /
`RANGE_BALANCE` / `VOLATILE_SWEEP_ZONE` / `TRANSITION` / `UNKNOWN`)
share names but are **independent classifications**.

- Regime is the H1-level classification produced by the
  `RegimeEngine`. The strategy dispatcher routes on regime
  (`RANGE → bb_reclaim`, `TREND → ema_continuation`,
  `VOLATILE → liquidity_sweep`, `TRANSITION → no signal`).
- Structure mode is the engine's finer-grained read on what price is
  doing right now (BB compression + EMA slope + ATR + level proximity
  + current reaction). Strategies use it as a **secondary gate** per
  spec §13.

A TREND regime can therefore see no signal if structure_mode is not
`TREND_CONTINUATION` — by design. This decouples macro routing from
micro confirmation.

### Confidence is not penalised by missing sides (refinement B, 2026-05-16)

A trend regime with only nearby support (no actionable resistance
above) should not get `confidence = 0`. `_compute_confidence` returns
the single side's normalised score in that case. Implemented with
explicit `None` checks so `min(None, x)` never crashes.

### Always-on cadence

`analyze_structure` runs on every BAR_CLOSE — including gap-fill bars
and stale windows — matching the always-on regime update at
`src/bot/loop.py:_handle_bar_close`. The signal gate at the same
function suppresses *trades* during STALE / RESUMING / SHUTTING_DOWN;
structure analysis itself stays observable.

### Reaction priority on collision (H-3, 2026-05-16)

In the strict 3-bar lookback window, two reactions can theoretically
apply to the same bars simultaneously:

- **ACCEPTANCE_BREAK** — `ACCEPTANCE_MIN_CLOSES` consecutive closes
  beyond the zone with a bearish/bullish-bodied confirmation.
- **FAILED_RECLAIM** — break beyond the zone, retest from outside
  (wick re-enters the zone), retest fails to close back inside.

When both conditions match, **FAILED_RECLAIM takes priority** because:

1. It is the stronger continuation signal — price actively tried to
   reclaim the level and failed, which is a more specific bearish
   (or bullish) confirmation than two raw closes beyond.
2. The failed-reclaim shape *includes* an acceptance signal as part of
   the pattern (the bars produce both shapes by construction in many
   common cases), so the broader pattern wins the tie.
3. Spec §13 treats failed-reclaim as a strategy gate equivalent to
   acceptance-break for the EMA Continuation strategy, but with the
   added structural evidence that strengthens the read.

`reaction_detector.classify_reaction` evaluates patterns in this fixed
order — first match wins:

1. Failed reclaim (above resistance / below support)
2. Acceptance break (above resistance / below support)
3. Sweep + reclaim (support / resistance)
4. Rejection (support / resistance)
5. Inside-range fallback

Operators reading `current_reaction` should remember: an absent
acceptance-break flag does **not** mean the level wasn't broken — it
may mean a more specific failed-reclaim won the tie. Both
`SUPPORT_ACCEPTANCE_BREAK` and `FAILED_RECLAIM_BELOW_SUPPORT` route to
the same EMA Continuation gate in spec §13, so the strategy layer
treats them equivalently.

### Deterministic by construction

Spec §17 rule #1: same candles in = same StructureState out. The
detector is bar-close only — no intrabar reads, no broker state, no
clock-dependence (timestamps come from the candle index, not
`datetime.now()`). This is why tests use synthetic OHLC fixture
builders, not recorded broker data.
