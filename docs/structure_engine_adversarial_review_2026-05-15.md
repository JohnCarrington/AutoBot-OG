# Adversarial Review — Phase 11 Structure Engine

**Branch:** `feature/structure-engine`
**Base:** `develop` (78e4145)
**Reviewer:** Claude Opus 4.7 (1M context)
**Date:** 2026-05-15
**Scope:** Read-only review of the Structure Engine module, its integration in `bot/loop.py`, and the Phase 11 strategy rewrites.

---

## Summary

- **Tests:** 960 passed, 0 failed (matches expected count).
- **Module isolation:** Clean. `src/structure_engine/` imports only from `config.pair_config` + itself. Legacy `src/structure/` untouched and still in use by Phase 3 (`add_fractal_swings`) and Phase 6 (`get_structure_state`).
- **Most locked decisions implemented as agreed.** One material deviation (H-1) plus one silent-data bug (H-2) prevent unconditional approval.
- **Recommendation:** **APPROVE WITH CONDITIONS** — fix H-1 (liquidity_sweep mode gate) and H-2 (equal-HL cluster detection) before merge; the others can be follow-up tickets but should be queued so they don't drift.

The engine is well-structured, the EMA degradation works as designed, the confidence None-guard is correct, and the 8 reaction detectors fire on their dedicated unit tests. The defects below are concentrated in two areas: (a) one strategy missed its `structure_mode` gate, and (b) the equal-highs/lows cluster detector counts the wrong thing.

---

## Issues by severity

### CRITICAL

_None._ No issue was found that silently corrupts `StructureState` in a way that would route trades through wrong gates given the current strategy gate matrix. (H-2 below is close — it silently misclassifies levels and would corrupt liquidity selection if H-1 were already fixed.)

---

### HIGH

#### H-1. `liquidity_sweep` does not gate on `structure_mode == VOLATILE_SWEEP_ZONE`

- **Location:** `src/strategies/liquidity_sweep.py:64–93`
- **Severity:** HIGH — design deviation from locked decision / spec §13 gate matrix.
- **Description:**
  Per the locked design (review prompt §2c and spec §13), each strategy must gate on **both** `regime_state.current_regime` (dispatcher) **and** `structure_state.structure_mode` (additional gate). `bb_reclaim` and `ema_continuation` correctly do so:

  - `bb_reclaim.py:78` — `if structure_state.structure_mode != "RANGE_BALANCE": return None`
  - `ema_continuation.py:75` — `if structure_state.structure_mode != "TREND_CONTINUATION": return None`

  `liquidity_sweep.py` has **no** equivalent check. It gates on `regime == VOLATILE` (line 65), `is_valid` (line 67), `_direction_from(...)` (which checks reaction/htf/liquidity presence), and the session — but never on `structure_state.structure_mode`. The string `"VOLATILE_SWEEP_ZONE"` is not present anywhere in the strategy file (`grep` confirms).

  The strategy will therefore fire on a `RESISTANCE_SWEEP_RECLAIM` reaction during a `VOLATILE` regime even if the engine classifies `structure_mode = RANGE_BALANCE` or `TRANSITION` — i.e., even when the engine itself is signalling "no sweep zone". This is exactly the asymmetric gate behaviour the locked decision was meant to prevent.

- **Test gap:** `test_strategy_liquidity_sweep.py` only ever passes `structure_mode="VOLATILE_SWEEP_ZONE"` (line 88), so the missing gate is invisible to the suite. A test that constructs the same `StructureState` with `structure_mode="RANGE_BALANCE"` would currently emit a signal where it should not.
- **Suggested fix:**
  ```python
  if structure_state.structure_mode != "VOLATILE_SWEEP_ZONE":
      return None
  ```
  Insert directly after the `is_valid` check at line 68. Add a `test_non_volatile_sweep_zone_mode_returns_none` test mirroring `test_non_trend_continuation_mode_returns_none` in `test_strategy_ema_continuation.py`.

---

#### H-2. Equal-HL cluster detection counts unique source labels, not member swings

- **Location:** `src/structure_engine/structure_state.py:355–364`, with the underlying merge in `zone_builder.py:141–170`.
- **Severity:** HIGH — silently incorrect `is_equal_hl_cluster` flag corrupts liquidity selection, scoring, and (downstream) `liquidity_sweep` confidence.
- **Description:**
  `_mark_equal_hl_clusters` decides "cluster vs not" by counting **distinct `swing_*` source strings** in `z.sources`:

  ```python
  swing_sources = [s for s in z.sources if s.startswith("swing_")]
  if len(swing_sources) >= EQUAL_HL_MIN_COUNT:
      z.is_equal_hl_cluster = True
  ```

  But `_merge_pair` dedupes sources with `sorted(set(a.sources + b.sources))` (zone_builder.py:157). Every H1 swing zone is created with `source="swing_h1"` (structure_state.py:314), so **any number** of merged H1 swings collapses to a single `"swing_h1"` source label — `len(swing_sources) == 1 < EQUAL_HL_MIN_COUNT(=2)`.

  Consequence: the canonical "equal highs" / "equal lows" pattern — multiple H1 swings clustering at the same price — **never gets `is_equal_hl_cluster=True`**. The only zones that get flagged are those formed by merging swings from **different timeframes** (H1+M5, H1+M15, etc.), which is the opposite of what the spec calls a cluster.

  Downstream effects:
  1. `scoring._reaction_component` and `LIQUIDITY_SCORE_EQUAL_HL` (1.5 pts) are never added to true equal-HL clusters → their score under-shoots by 1.5.
  2. `_wrap_levels` falls through to assigning `level_type = "RESISTANCE"`/`"SUPPORT"` instead of `"LIQUIDITY_HIGH"`/`"LIQUIDITY_LOW"`.
  3. `liquidity.pick_liquidity_above` / `pick_liquidity_below` prefer `is_equal_hl_cluster=True` zones (liquidity.py:58) → the canonical cluster is silently demoted to the "any HIGH-side zone" tier; only session levels can outrank it.
  4. `_direction_from` in `liquidity_sweep.py` requires `state.liquidity_below is not None` — which the cluster would have supplied. In single-pair sweeps with no session data (Phase 11 stub case), this can be the deciding factor in whether the strategy fires.

- **Test gap:** No test creates >1 same-TF swing at the same price and asserts the merged zone has `is_equal_hl_cluster=True`. The `test_liquidity_above_prefers_equal_hl_cluster` test (test_liquidity.py:24) **hand-sets** the flag rather than exercising the detection path.

- **Suggested fix:** Count member swings, not labels. Two viable approaches:
  1. Track an explicit `member_count: int` on `CandidateZone`; increment per swing in `make_zone` and sum in `_merge_pair`.
  2. Reuse `swing_strengths` (already preserved through merge): `if len(z.swing_strengths) >= EQUAL_HL_MIN_COUNT`. This is a one-line change — `swing_strengths` is currently a dead list that just happens to be the count we need.

  Add a regression test that creates 3 H1 swings within `ZONE_MERGE_MULT` distance, calls `analyze_structure`, and asserts the merged zone has `level_type in ("LIQUIDITY_HIGH", "LIQUIDITY_LOW")`.

---

#### H-3. Failed-reclaim-over-acceptance-break priority is undocumented in MODULE.md

- **Location:** `src/structure_engine/reaction_detector.py:53–94`, `src/structure_engine/MODULE.md`.
- **Severity:** HIGH (process), MEDIUM (functional impact).
- **Description:**
  The review prompt §1c explicitly flagged this:

  > "failed-reclaim takes priority over acceptance-break on collision" — this priority decision was NOT pre-approved. Session 1 made it during implementation. Verify the priority is defensible … Is this priority documented in MODULE.md?

  The priority **is** justified in the in-file comment (reaction_detector.py:53–56) — "failed-reclaim is the more specific pattern, supersets the acceptance-break shape." That justification is correct: every `FAILED_RECLAIM_BELOW_SUPPORT` setup (setup close below, rejection wick into zone but close still below, bearish confirmation) also satisfies the `SUPPORT_ACCEPTANCE_BREAK` condition (≥2 trailing closes below + bearish-bodied confirmation), so the two collide on most genuine break-and-retest patterns.

  **Functional impact:** Low — both reactions are accepted by the same `ema_continuation` BEARISH/BULLISH branch (`_BEARISH_REACTIONS`/`_BULLISH_REACTIONS` in ema_continuation.py:54–59), so the strategy gates the same way either way. The reaction label is what differs in logs and `StructureState.debug`.

  **Process issue:** MODULE.md (the design contract for this module) does **not** mention this priority. Locked decision #6 ("levels can carry both flags") is documented; this priority is not. A future caller (or a follow-up strategy that distinguishes the two) would have no MODULE.md anchor for the design choice.

- **Suggested fix:** Add a short subsection to MODULE.md under "Design notes":
  > **Reaction priority on collision (2026-05-16)**
  > When a 3-bar window satisfies both `FAILED_RECLAIM_*` and `*_ACCEPTANCE_BREAK`, the detector returns the failed-reclaim. Rationale: failed-reclaim adds the retest-from-the-wrong-side observation, so it is strictly more informative. Both reactions feed the same `ema_continuation` gates in v1.

  No code change required.

---

### MEDIUM

#### M-1. `swing.strength` is computed but never consumed

- **Location:** `src/structure_engine/swing_detector.py:192–223`; `src/structure_engine/scoring.py`.
- **Description:** `detect_swings` computes a per-swing `strength` from displacement, wick, and recency, and `_zones_from_swings` passes it into `make_zone(..., swing_strength=...)` which appends to `CandidateZone.swing_strengths`. `_merge_pair` preserves the list across merges. But `score_zone` reads none of: `swing_strengths`, `swing_strength`. The signal is dead-loaded.
- **Impact:** The spec §5 "strength" concept and the post-swing-displacement metric are inert; level scoring is purely (timeframe + touch + reaction_atr_mult + recency + liquidity + session − invalidation).
- **Suggested fix:** Either (a) wire `max(z.swing_strengths)` into `score_zone` with a small weight, or (b) drop the strength computation and the `swing_strengths` list. Decision should be explicit, not implicit.
- **Note:** This is also why M-2 below isn't impactful in v1.

#### M-2. `_recency(i, total_bars)` is non-deterministic across buffer growth

- **Location:** `src/structure_engine/swing_detector.py:270–275`.
- **Description:** The recency component is `i / (total_bars - 1)`. A swing at fixed `bar_index=50` has recency `50/99` in a 100-bar call and `50/199` in a 200-bar call. Same swing object identity, different `Swing.strength`. The review prompt §5c asks specifically: "same swing emitted multiple times across overlapping calls — idempotent?"
- **Impact:** Currently **none**, because `Swing.strength` is dead (M-1). If M-1 is fixed by wiring strength into scoring, this becomes a determinism bug.
- **Suggested fix:** Use absolute distance from the latest bar, not a normalised position: `recency = max(0.0, 1.0 - (total_bars - 1 - i) / RECENCY_BARS_MEDIUM)` or similar. Stays bounded and is stable as the buffer grows.

#### M-3. `near_liquidity` is "liquidity exists anywhere", not "price near liquidity"

- **Location:** `src/structure_engine/structure_state.py:174`.
- **Description:** `near_liquidity = liquidity_above_zone is not None or liquidity_below_zone is not None`. Once the engine identifies any HIGH-side zone above and any LOW-side zone below current price, `near_liquidity=True` — which is almost always. The `mode_classifier` then only needs elevated ATR to fire `VOLATILE_SWEEP_ZONE` (mode_classifier.py:63–73), regardless of whether price is actually approaching the liquidity zone.
- **Impact:** `VOLATILE_SWEEP_ZONE` over-fires whenever ATR is elevated, undermining the intent of "near liquidity pool" as a positional confirmation. Combined with H-1, this is what currently gates `liquidity_sweep`'s mode independence.
- **Suggested fix:** Define proximity in ATR or pip units: `near_liquidity = any(abs(price - z.price) < N * atr_m5 for z in (liquidity_above_zone, liquidity_below_zone) if z is not None)`. Tune `N` via constant.

#### M-4. Acceptance-break detectors require bearish/bullish body on the latest bar — beyond spec

- **Location:** `src/structure_engine/reaction_detector.py:280–283` (support side), `306–310` (resistance side).
- **Description:** After confirming N trailing closes below `zone_low`, the support break also requires `closes[-1] < conf_open` (bearish body on the confirmation bar). The locked decision (review prompt §1) and spec §10E describe acceptance as "N consecutive closes beyond the zone" — no requirement on the confirmation bar's body shape.

  Example missed pattern: bar N-2 close above zone, bar N-1 bearish breakout closes well below zone_low, bar N small bullish pullback bar still below zone_low. The detector returns `None` because bar N has `close >= open`, even though acceptance (≥2 consecutive closes below) has occurred.

- **Impact:** False negatives on valid acceptance breaks → `TREND_CONTINUATION` mode and `ema_continuation` gate may miss the signal one bar after a clean break.
- **Suggested fix:** Drop the `closes[-1] < conf_open` check, or relax to "either the last close is below zone_low **or** the body of any bar in the trailing window was bearish."

#### M-5. `test_support_rejection_reaches_state` accepts `NONE` as a pass

- **Location:** `tests/unit/structure_engine/test_analyze_structure.py:117–119`.
- **Description:**
  ```python
  assert state.current_reaction in (
      "SUPPORT_REJECTION", "SUPPORT_SWEEP_RECLAIM", "NONE",
  )
  ```
  `"NONE"` makes the assertion trivially satisfied. This is the **only** end-to-end test that pipes a reaction-builder through `analyze_structure`, so the weakened assertion hides whether the orchestrator actually surfaces the reaction the builder constructs.
- **Suggested fix:** Require `state.current_reaction != "NONE"` and assert at least one of the support reactions. If the zone-detection pipeline doesn't reliably surface a support zone for this fixture, fix the fixture instead of widening the assertion.

#### M-6. `_derive_and_enrich_m15` and `_m15_for_test` are untested

- **Location:** `src/bot/loop.py:758–807`.
- **Description:** A brand-new ~50-line method on the critical-path BAR_CLOSE handler. `grep -rn "_derive_and_enrich_m15\|_m15_for_test" tests/` returns no hits.
- **Risk:** Pandas resample edge cases (DST, missing bars, irregular timestamps, leading partial bin) are easy to get subtly wrong. The trim rule `m5_close_time.minute % 15 != 0` is right for in-step bars but doesn't guard against off-schedule arrivals.
- **Suggested fix:** A focused test under `tests/unit/test_bot_loop_*` that calls `_m15_for_test` with three scenarios: (a) m5_close on a 15-min boundary, (b) off-boundary so trim fires, (c) buffer with fewer than 3 M5 bars producing an empty/single-row M15.

#### M-7. No determinism regression test on `analyze_structure`

- **Location:** `tests/unit/structure_engine/test_analyze_structure.py`.
- **Description:** Spec §17 rule #1 and MODULE.md "Deterministic by construction" promise same-inputs-same-output. No test calls `analyze_structure` twice on the same fixture and asserts identity / equality of the resulting `StructureState`.
- **Suggested fix:** Add `test_analyze_structure_is_deterministic` that runs the orchestrator twice, compares `dataclasses.asdict(state_a) == dataclasses.asdict(state_b)`. Cheap, catches future regressions where someone introduces `datetime.now()` or random tie-breakers.

#### M-8. `_compute_confidence` has no dedicated unit test

- **Location:** `src/structure_engine/structure_state.py:512–527`; tests in `test_analyze_structure.py:69–92`.
- **Description:** `test_one_sided_structure_doesnt_crash_confidence` only asserts the value lies in [0, 1]; the actual one-sided / both-None branches aren't independently verified. Refinement B was the whole point of this function.
- **Suggested fix:** Add three direct calls to `_compute_confidence` (both-present takes min, only-support, only-resistance, both-None=0.0). Each is two lines of assertion.

#### M-9. First M15 bar after resample is a partial aggregate

- **Location:** `src/bot/loop.py:778–795`.
- **Description:** With `closed="right", label="right"`, the leftmost output bin can contain fewer than 3 M5 bars depending on where the buffer starts. Only the trailing bin is trimmed via `agg.iloc[:-1]`. The leading partial bin propagates into indicator computation.
- **Impact:** EMA/ATR seed values absorb one less-aggregated bar. Unlikely to flip a signal on its own, but increases the warm-up bar count of M15 indicators by one. Worth a comment + a leading-edge trim when the buffer doesn't start on a 15-min boundary.

#### M-10. `local_bias` walks the HTF EMA priority (`ema_200/100/50`), not shorter EMAs

- **Location:** `src/structure_engine/bias_detector.py:81–123`.
- **Description:** `detect_local_bias` calls the same `_select_ema(row)` as `detect_htf_bias`, which iterates `BIAS_EMA_PRIORITY = ("ema_200", "ema_100", "ema_50")`. For local (M5/M15), shorter EMAs (`ema_21`, `ema_50`) would more closely match the "local" semantics. The current behaviour is to prefer `ema_200` even on M5 when it's present.
- **Impact:** Once warm-up completes, local_bias behaves nearly identically to HTF bias on M5 (both prefer ema_200). The local/HTF distinction collapses to "different DataFrame", not "different time horizon".
- **Suggested fix:** Either (a) define a `LOCAL_EMA_PRIORITY = ("ema_50", "ema_21")` and walk that in `detect_local_bias`, or (b) document this as intentional in MODULE.md so a follow-up phase can pick the right answer.

---

### LOW

#### L-1. `_STRONG_LEVEL_THRESHOLD = 6.0` hardcoded in `bb_reclaim.py`

- **Location:** `src/strategies/bb_reclaim.py:54`.
- **Description:** Parallel constant `STRONG_LEVEL_THRESHOLD = 6.0` already exists in `structure_engine.constants` and is env-overridable. Hard-coding it in the strategy means a `STRUCTURE_STRONG_LEVEL_THRESHOLD` env override silently doesn't apply to BB Reclaim.
- **Fix:** `from structure_engine.constants import STRONG_LEVEL_THRESHOLD`.

#### L-2. `STRUCTURE_LOG_ENABLED` / `STRUCTURE_LOG_PATH` are import-time bound

- **Location:** `src/structure_engine/constants.py:174–179`, used in `src/structure_engine/logging.py:35–47`.
- **Description:** The env var is read once at module import. Setting `STRUCTURE_LOG_ENABLED=1` after the process is up has no effect (tests monkeypatch the module attribute to work around this). Ops will expect the env var to work like other v1 settings.
- **Fix:** Read the env var inside `log_structure_state` each call: `if os.getenv("STRUCTURE_LOG_ENABLED", "0").lower() in ("1","true","yes"):`. Same for the path.

#### L-3. Unused imports in `structure_state.py`

- **Location:** `src/structure_engine/structure_state.py:29, 37–38`.
- **Description:** `pip_to_price`, `PAIR_MIN_ZONE_PIPS`, `DEFAULT_MIN_ZONE_PIPS` are imported but never referenced in this file (they are used transitively in `zone_builder.py`).
- **Fix:** Delete.

#### L-4. `FAILED_RECLAIM_ABOVE_RESISTANCE` is bullish despite the name

- **Location:** `src/structure_engine/reaction_detector.py:341–362`, `src/structure_engine/types.py:31`.
- **Description:** The naming reads "above_resistance" but the pattern fires on a **bullish** continuation (price broke up through resistance, retested from above, bears failed, bulls continue). A reader has to trace the bar logic to see that the named direction is the *defender's* failed attempt, not the resulting move.
- **Impact:** Cognitive load + risk of an editor misclassifying the strategy mapping later. The current strategy code (ema_continuation.py:57–59) maps it correctly.
- **Suggested fix:** Either add a one-line docstring above the type alias clarifying "the reclaim that failed", or rename to `FAILED_RECLAIM_BACK_BELOW_RESISTANCE` / similar. No urgency.

#### L-5. `_zone_to_level` double-scores the same zone

- **Location:** `src/structure_engine/structure_state.py:431–509`.
- **Description:** `_wrap_levels` scores every merged zone (line 434). `_zone_to_level` scores the same zone again when wrapping nearest_support / nearest_resistance / liquidity_above / liquidity_below (line 490). Up to 5× duplicate `score_zone` calls per analysis cycle.
- **Impact:** Negligible CPU; cleanliness only.
- **Suggested fix:** Cache `(score, components)` on the `CandidateZone` after the first call.

#### L-6. MODULE.md does not mention the strict-strength wick formula or the touch criterion

- **Location:** `src/structure_engine/MODULE.md`.
- **Description:** "Owns" lists the modules but does not pin down decisions #5 (3-bar strict lookback), #8 (touch = wick overlap), or #6 (S/R + liquidity coexistence). MODULE.md is the document a future caller reads first; these decisions belong there.
- **Suggested fix:** A two-sentence "Locked design decisions" subsection citing the review prompt indexes.

---

## Verification of the prompt's targeted concerns

The review prompt called out specific items. Each is checked below:

| Concern | Status | Notes |
| --- | --- | --- |
| 1a. All 8 reactions classified per §10 | **OK** | Each detector implemented; unit-tested. |
| 1b. Strict 3-bar lookback N-2/N-1/N | **OK** | `bars.iloc[0/1/-1]` consistently. |
| 1c. Failed-reclaim priority | **OK functionally; docs gap → H-3** | See H-3. |
| 1d. Determinism on zone-midpoint close | **OK** | Boundary comparisons use mixed strict/inclusive deliberately (verified). |
| 1e. Wick-vs-close distinction | **OK** | Rejection requires `rej_low <= zone_high` (wick) AND `rej_close > midpoint` (close); acceptance requires consecutive closes beyond. |
| 2a. BB Reclaim gates on `RANGE_BALANCE` not regime | **OK** | bb_reclaim.py:78. |
| 2b. EMA Continuation gates on `TREND_CONTINUATION` + htf | **OK** | ema_continuation.py:75 + `_direction_from`. |
| 2c. Liquidity Sweep gates on `VOLATILE_SWEEP_ZONE` | **FAIL → H-1** | Missing entirely. |
| 2d. Strategies don't read raw DataFrames | **MOSTLY** | They still read `df_m5` for ATR/close/timestamp + `df_h1` for MACD confidence. Pattern detection is gone, which is the locked goal. |
| 3a-d. EMA degradation tests | **OK** | Three warm-up states covered (only-50, 50+100, all three). |
| 3e. EMA100 NaN handled same way | **OK** | `_select_ema` walks the priority unconditionally. |
| 4a-d. Confidence None-guard | **OK** | Explicit None checks; no `min(None, x)` path. Test coverage thin → M-8. |
| 5a. N-bar fractal determinism | **OK** | Deterministic given fixed input; `_strictly_greater`/`_strictly_less` consistent. |
| 5b. Strength components | **3 of the 4 spec items (displacement, wick, recency); trend-leg omitted.** Spec §5 mentioned displacement, wick, **trend leg**, and recency — the implementation drops the trend-leg component, which is reasonable for a single-bar measure but unstated. Note also M-1 (unused). |
| 5c. Idempotent across calls | **Partial** | Type/price/timestamp/bar_index stable; strength varies (M-2). |
| 6a. ATR-based half-width | **OK** | `max(pip_floor, atr_m5 * 0.25)`. |
| 6b. Weighted merge by TF score | **OK** | H1=3, M15=2, M5=1; tested. |
| 6c. Tight cluster of 5 levels | **OK** for cross-side; **see H-2** for same-side same-TF clusters. |
| 6d. Empty input | **OK** | `merge_zones([]) → []`. |
| 7a. §8 scoring formula | **OK** | All components present; cap at 10. |
| 7b. Weights match spec | **OK** | TF=3/2/1, touch=0.5/1/2, etc. Defaults env-overridable. |
| 7c. Session score=0 with None | **OK** | `_zones_from_session` short-circuits on `session=None`. |
| 7d. Invalidation criterion | **NOT IMPLEMENTED** | `CandidateZone.invalidated` is a field but no code path sets it to `True`. `score_zone` reads it; `_accumulate_touches`/`merge_zones` never write it. The penalty branch is currently unreachable. Treat as MEDIUM if you intended invalidation tracking in v1; leaving as a documented LOW since it doesn't corrupt output, just under-uses the spec. |
| 8a. HTF from H1, local from M5/M15 | **OK** | Separated; M15 preferred for local. |
| 8b. §9 conditions | **OK with M-10 caveat** | Stack + price-vs-EMA + structural + MACD. |
| 8c. Stack check + degradation interaction | **OK** | `_ema_stack_signal` short-circuits to NEUTRAL if ema_8/13/21 are NaN; degradation only affects the long-EMA signal. |
| 9a-d. Mode classifier | **OK with M-3 caveat** | First-match precedence; UNKNOWN fallback only on missing data; TRANSITION when biases disagree. |
| 10a. liquidity_above/below populated | **OK** | |
| 10b. Cluster → session → plain tiers | **OK** | liquidity.py:54–65. |
| 10c. Coexistence (S/R + liquidity) | **OK in mechanism** | `_zone_to_level(level_type=...)` lets the same zone surface in both slots. But H-2 means the *cluster* flag itself is rarely set. |
| 11a. Min candle validation | **OK** | M5 ≥ 50 enforced; M15/H1 absent fall back to empty swings without invalidating. |
| 11b. `is_valid=False` reason | **OK** | `"insufficient_candles_m5"`. |
| 11c. Module order | **OK** | swings → zones (build+merge) → marks → touches → liquidity → reactions → bias → mode. |
| 11d. `session_state=None` graceful | **OK** | `_zones_from_session` returns `[]`. |
| 11e. Deterministic output | **OK by inspection; no test → M-7** | |
| 12a. M15 derivation | **OK; tests gap → M-6** | |
| 12b. Always-on BAR_CLOSE | **OK** | loop.py:621 — called before the `signals_blocked` gate. |
| 12c. structure_state threaded to dispatcher | **OK** | loop.py:650, 1066. |
| 12d. `_log_structure_state` env-gated | **OK** | `STRUCTURE_LOG_ENABLED` default off (L-2 caveat). |
| 13a. 8 reaction builders produce the patterns | **OK** | Builders construct OHLC that satisfies each detector when paired with `_support_zone`/`_resistance_zone`. |
| 13b. Strategy tests exercise gate states | **OK for bb/ema, gap for liq_sweep (H-1).** Tests don't probe the missing structure_mode gate. |
| 13c. Equal-highs edge cases | **GAP** | `test_swing_requires_strict_inequality` covers ties; no test for the same-TF cluster path that surfaced H-2. |
| 13d. End-to-end realistic OHLC | **WEAK → M-5** | |
| 14a-c. Module isolation | **OK** | Only `config.pair_config` imported externally; no `src/structure/` imports. Logging swallows OSError. |
| 15a. Indicator pipeline expansion | **OK** | 960 tests pass — Phase 2 indicator tests still green with the new EMA additions. |
| 15b. Dispatcher signature change | **OK** | All callers (loop.py, all tests) updated. |
| 15c. M15 derivation on bar-close edges | **OK trim logic; M-9 partial-leading-bar gap** | |

---

## Final recommendation

### APPROVE WITH CONDITIONS

**Block-on-merge (must fix before this branch ships):**

1. **H-1** — Add `structure_mode == "VOLATILE_SWEEP_ZONE"` gate to `liquidity_sweep.py` + matching test. ~5 LOC + 1 test.
2. **H-2** — Fix `_mark_equal_hl_clusters` to count member swings rather than deduped source labels + regression test. ~3 LOC + 1 test.
3. **H-3** — Add the failed-reclaim-priority subsection to MODULE.md. Documentation only.

**Tracker-but-OK-to-merge (do not regress; queue as follow-ups):**

- **M-1 / M-2** as a paired decision: wire `swing.strength` into scoring **or** drop it. Either is fine; the current "compute and discard" middle ground rots.
- **M-3** — Tighten `near_liquidity` to actual proximity.
- **M-4** — Reconsider the bearish-body confirmation gate on acceptance breaks.
- **M-5** — Tighten `test_support_rejection_reaches_state`.
- **M-6** — Add a focused test for `_derive_and_enrich_m15`.
- **M-7** — Add an `analyze_structure` determinism test.
- **M-8** — Add direct `_compute_confidence` cases.
- **M-9** — Leading-edge M15 trim.
- **M-10** — Decide if local_bias should walk a shorter EMA priority.
- **Spec §8 invalidation_penalty** is unreachable (no code path sets `invalidated=True`). Decide whether to wire it or remove the field.

**Why APPROVE WITH CONDITIONS rather than RE-WORK:**

The engine's spine is sound. Bias, mode, and 7 of the 8 strategy gate paths are implemented as agreed; the two HIGH issues are surgical fixes, not architectural rework. The test suite at 960/960 passes — the failure modes I called out are gaps in test coverage, not breakages of existing tests. The locked decisions on cadence, alongside-not-replace, regime-vs-mode separation, refinements A & B, and the 3-bar lookback are all implemented as designed.

But: H-1 in particular is exactly the kind of "silent gate looks asymmetric" bug the strategy rewrites were meant to eliminate, and H-2 is exactly the kind of "the spec said cluster but the code counted something else" defect this review existed to find. Both must land before strategies start trading on this engine's output.

---

# Addendum — Session 2 re-review (2026-05-16)

Fix commit `8172289` lands H-1, H-2, H-3. Verified against the original review's reproduction criteria. Test count: **966/966 passed** (was 960; +4 H-1 parametrized + 2 H-2 cases, matches the commit's "+6" claim).

## H-1 verification — `liquidity_sweep` structure_mode gate

- **Placement:** `src/strategies/liquidity_sweep.py:74` — after `is_valid` (line 67), before `_direction_from` (line 77). Matches the canonical ordering used by `bb_reclaim` (regime → is_valid → mode → direction) and `ema_continuation` (same). ✓
- **Comment quality:** Lines 69–73 explain *why* — "VOLATILE regime without VOLATILE_SWEEP_ZONE mode means the H1 classifier called the macro state volatile but the engine doesn't see price near a liquidity pool right now". Good operator-facing rationale. ✓
- **Parametrized test:** `test_liquidity_sweep_rejects_non_volatile_sweep_zone_mode` (`tests/unit/test_strategy_liquidity_sweep.py:189–227`).

  Coverage: `["RANGE_BALANCE", "TREND_CONTINUATION", "TRANSITION", "UNKNOWN"]` — every non-matching value of `StructureMode` literal. ✓

- **"Other gates set to passing" verification:** Traced the test's `StructureState` against every remaining gate:

  | Gate | Test fixture value | Would pass with VOLATILE_SWEEP_ZONE? |
  | --- | --- | --- |
  | regime | `_state()` → "VOLATILE" | ✓ |
  | is_valid | `True` | ✓ |
  | reaction + htf + liquidity_below (`_direction_from`) | `SUPPORT_SWEEP_RECLAIM` + `BULLISH` + level present | ✓ → BULLISH |
  | session (`source_ts` in London) | `_LONDON_NOW=12:00 UTC` | ✓ |
  | nearest_support (`swept_level`) | level present | ✓ |
  | `_sweep_extreme` (≥3 M5 bars, valid lows) | `_m5(ts=_LONDON_NOW)` provides 3 bars with `sweep_low=1.29900` | ✓ |
  | atr_m5 > 0 | `0.0020` | ✓ |
  | entry_price | base=1.30050 | ✓ |

  **Conclusion:** When `structure_mode == "VOLATILE_SWEEP_ZONE"`, every other gate passes and a Signal would emit. The test therefore proves the new gate is the *sole* cause of rejection for each of the 4 wrong-mode inputs. ✓

- **Minor doc drift (LOW):** The module docstring (lines 8–22) still lists the gates without mentioning `structure_mode == VOLATILE_SWEEP_ZONE`. The block was authoritative-looking before this fix; future readers may consult it instead of the function body. Recommend a one-line addition for symmetry with `bb_reclaim.py` / `ema_continuation.py`, neither of which lists the mode gate in their module docstrings either — so it's a pre-existing pattern, not a regression. **Not blocking.**

## H-2 verification — equal-HL cluster count

- **Code change:** `_mark_equal_hl_clusters` (`src/structure_engine/structure_state.py:355–370`) now reads `len(z.swing_strengths)` instead of unique source labels. The function is trivially correct given the invariant that `swing_strengths` accumulates one entry per member swing (verified below). ✓
- **Comment quality:** Lines 361–366 explain the dedup quirk explicitly — "sources is deduped via `sorted(set(...))` in `merge_zones._merge_pair`, so three H1 swings clustering at one price collapse to a single 'swing_h1' source string — but the underlying `swing_strengths` list preserves one entry per member swing". Names the fix's anchor (`H-2 review fix 2026-05-16`) for future archaeology. ✓
- **Invariant check — `swing_strengths` preservation:**

  | Code path | Effect on `swing_strengths` |
  | --- | --- |
  | `make_zone(..., swing_strength=float)` (zone_builder.py:93–94) | appends one entry (unless `None`) |
  | `_zones_from_swings` (structure_state.py:301–318) | always passes `s.strength` (a float) → 1 entry per swing |
  | `_zones_from_session` (structure_state.py:321–352) | passes no `swing_strength` → list stays empty |
  | `_merge_pair` (zone_builder.py:168) | `a.swing_strengths + b.swing_strengths` — preserved through merges |

  Therefore session-only zones cannot be flagged as clusters (correct — session levels are not equal-HL clusters by definition), and N merged swing zones produce `len == N`. ✓

- **Unit test:** `test_merged_zone_preserves_swing_strength_count` (test_zone_builder.py:95–114). Asserts both invariants: (a) `sources == ["swing_h1"]` (dedup occurred), (b) `len(swing_strengths) == 3`. This is the right level to test the underlying invariant. ✓
- **End-to-end test — does it use real swings?** `test_equal_highs_cluster_flagged_as_liquidity` (test_analyze_structure.py:122–178):
  - Builds 3 swing-high patterns at `1.30200`, spaced 6 M5 bars apart (centres 42, 48, 54).
  - Each centre has 3 strictly-lower bars on either side (`(-3, 0.0005), (-2, 0.0010), (-1, 0.0015)` then peak then mirror).
  - Pipes the resulting OHLC through `analyze_structure`, then asserts at least one `state.levels` entry has `level_type == "LIQUIDITY_HIGH"`.

  **Traced through the engine:**
  1. `detect_swings(df, "M5")` with `window=3` confirms peaks at indices 42, 48, 54 — each has 3 strictly-lower neighbours on both sides (verified arithmetic).
  2. `_zones_from_swings` builds 3 `CandidateZone` HIGH-side entries, each at `price=1.30200` with `swing_strengths=[s.strength]` (len 1).
  3. `merge_zones`: all 3 zones at the same price; merge threshold `ZONE_MERGE_MULT * (0.0004 + 0.0004) = 0.0008` ≫ 0 distance → all merge into one zone with `len(swing_strengths) == 3`.
  4. `_mark_equal_hl_clusters`: `3 >= EQUAL_HL_MIN_COUNT(2)` → `is_equal_hl_cluster = True`.
  5. `_wrap_levels`: HIGH-side cluster → `level_type = "LIQUIDITY_HIGH"`.

  This is a genuine end-to-end test, not state injection. ✓

  **Subtle but real:** the test would also fail if `detect_swings` were misconfigured or if `merge_zones` failed to combine same-price same-side zones. It exercises the full pipeline, so it's also a defence against regressions in those modules. Good design.

## H-3 verification — MODULE.md priority documentation

- **Section:** "Reaction priority on collision (H-3, 2026-05-16)" at MODULE.md:100–137. ✓
- **Discoverability:** Sits in the "Design notes" block alongside the EMA degradation and regime-vs-mode sections. Same H-N anchor convention. An operator scanning MODULE.md from the top will encounter it before reaching "Deterministic by construction". ✓
- **Content checklist:**
  - **Priority order documented:** Yes — full 5-tier list at lines 122–129 (failed reclaim → acceptance break → sweep reclaim → rejection → inside-range). Matches the actual order in `reaction_detector.classify_reaction` (file lines 53–144). ✓
  - **Rationale explained:** Yes — three numbered points (lines 110–120): "more specific continuation signal", "superset of acceptance shape", "spec §13 treats both as equivalent for EMA Continuation". ✓
  - **Operator note on absent flags:** Lines 131–136 — "an absent acceptance-break flag does not mean the level wasn't broken — it may mean a more specific failed-reclaim won the tie". This is the practical guidance an ops engineer needs when reading jsonl logs. ✓
- **No drift between MODULE.md and code:** Spot-checked that the documented order (failed_reclaim → acceptance → sweep → rejection → inside-range) is the order in `classify_reaction`. ✓

## Potential new issues introduced by the fixes

### 1. VOLATILE_SWEEP_ZONE gate rejecting valid setups during regime transitions

**Risk:** Low. The mode classifier requires `near_liquidity` AND `atr_now > 1.5 × atr_median` to fire VOLATILE_SWEEP_ZONE. In a genuine sweep setup, both conditions almost certainly hold because the regime classifier itself uses elevated ATR to flag VOLATILE. The mode and regime decisions agree by construction in the common path.

**The asymmetry that *could* cause friction:** `near_liquidity` is defined as "any liquidity zone exists above OR below" (M-3 in the main review, still deferred). If the swing detector hasn't yet identified a liquidity pool — e.g., during the first ~5 hours of live operation when only one side of structure has formed — the mode could be UNKNOWN or TRANSITION even while the regime engine sees VOLATILE. The gate would correctly reject in that case (not enough structure to anchor the sweep), but this is "correct" by intent, not a regression.

**Conclusion:** Acceptable. The fix tightens the contract as designed by spec §13.

### 2. swing_strengths count affecting other scoring paths

Searched: only `_mark_equal_hl_clusters` reads `swing_strengths`. `score_zone` does not. `_merge_pair` is the only writer beyond `make_zone`. No other code path consumes the field. ✓ No side effects.

**Note:** M-1 from the main review ("`swing.strength` value unused in scoring") is now *partially resolved* — the swing **count** is consumed via `swing_strengths`'s length. The strength **value** (the float itself) remains unused. The follow-up decision (wire strength into scoring, or drop the float) still stands but is less urgent.

### 3. Other paths sensitive to the fix

- **`_wrap_levels`** branches on `is_equal_hl_cluster` to assign `LIQUIDITY_HIGH`/`LIQUIDITY_LOW`. With H-2 fixed, **more** zones will now flag as clusters, so **more** zones will surface as liquidity levels in `state.levels`. Strategies that gate on `levels[*].level_type == "RESISTANCE"` would skip these. Audited: no strategy queries `state.levels` by `level_type`; they read `nearest_resistance`/`nearest_support`/`liquidity_above`/`liquidity_below` directly, all of which are computed independently. No regression risk.
- **`liquidity.pick_liquidity_above` / `_below`** prefer `is_equal_hl_cluster=True` zones (liquidity.py:58). With H-2 fixed, the cluster tier will now actually populate. This means liquidity selection improves — exactly the intended effect of the fix. ✓

## Test quality re-assessment

| Concern | Status |
| --- | --- |
| H-1 parametrized test verifies the gate is the *only* cause of rejection | **Verified** — every other gate independently traced as passing. |
| H-2 end-to-end test uses real OHLC, not hand-set state | **Verified** — synthetic candles flow through `detect_swings → merge_zones → _mark_equal_hl_clusters → _wrap_levels`. |
| H-2 unit test pins the underlying invariant | **Verified** — `swing_strengths` count preservation through merge. |

## Deferred items — re-assessment

- **M-1 (swing.strength unused):** Partially closed by H-2 (count now used). Float value still unused. Severity drops to LOW.
- **M-2 (recency non-deterministic):** Unchanged. Still LOW because the value remains unused.
- **M-3 (`near_liquidity` is "exists anywhere"):** Unchanged. Now interacts more visibly with H-1 — see "Potential new issues" item 1. Still MEDIUM, still deferred OK.
- **M-4 (acceptance-break requires directional body):** Unchanged. Still MEDIUM.
- **M-5 (loose test):** Unchanged.
- **M-6 (M15 derivation untested):** Unchanged. Still MEDIUM — this is the deferred item I'd most argue for promoting if a fast-follow window opens, because the M15 path is on every BAR_CLOSE.
- **M-7 (no determinism test):** Unchanged.
- **M-8/M-9/M-10:** Unchanged.
- **Spec §8 `invalidation_penalty` unreachable:** Unchanged — still no code path sets `invalidated=True`. Decide-and-act in a follow-up.

None of the deferred items should have been promoted to block-on-merge. The three HIGHs were the right cut.

## Final recommendation

### APPROVE FOR MERGE

All three HIGH issues from the original review are fixed correctly:

- **H-1** — gate present and parametrized-tested across all 4 wrong modes; trace confirms it's the sole rejection cause.
- **H-2** — count fix correct; unit test pins the invariant; end-to-end test exercises the full pipeline with real swings.
- **H-3** — MODULE.md section is thorough (priority order + rationale + operator note + anchor to fix date).

966 tests pass, zero new warnings. No new defects introduced by the fixes. The deferred MEDIUM/LOW items remain follow-up tickets — none rise to block-on-merge given the engine's current usage pattern.

**Merge recommendation:** `feature/structure-engine` is ready for merge into `develop`.

