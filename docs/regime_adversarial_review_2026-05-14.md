# Adversarial review — `feature/regime` (commit `3880f2c`)

**Reviewer:** Independent Claude Code session (read-only)
**Date:** 2026-05-14
**Scope:** `src/regime/*.py` and `tests/unit/test_regime_*.py` on `feature/regime`, comparing against the v1 regime spec supplied with the review brief.
**Baseline:** `develop` @ `86b7168`.

---

## Summary

39/39 tests pass locally (`.venv/bin/python -m pytest tests/unit/test_regime_*.py`). The code is clean, well-documented and most spec requirements are translated faithfully. However the state machine harbours **two CRITICAL bugs** that the existing tests do not exercise. Both reproduce with one-line scripts (probes in §3 below).

| Severity | Count |
| --- | ---: |
| CRITICAL | 2 |
| HIGH     | 5 |
| MEDIUM   | 7 |
| LOW      | 9 |
| **Total** | **23** |

**Recommendation: APPROVE WITH CONDITIONS.** Issues C1, C2 and H1 must be fixed before this engine processes a live tape; everything else can be follow-up work. See §4 for the conditional checklist.

---

## 1. CRITICAL

### C1 — M5 confirmation counter resets on every H1 close, even when H1 re-emits the same pending regime
- **Location:** `src/regime/engine.py:173-194` (the `same`/regime-change branches in `process_h1_close`)
- **Evidence (reproduced):**
  ```
  After H1 #1 (HH+HL, slope 0.45):   current=TRANSITION, pending=TREND, count=0
  After 2 agreeing M5 closes:        pending=TREND, count=2
  After H1 #2 (identical inputs):    pending=TREND, count=0   ← reset
  ```
- **Why it's wrong:** The spec says *"If H1 changes regime, M5 confirmation resets to 0"*. The implementation resets on **every** H1 bar that arrives while there is an in-flight pending — including bars where the H1 vote has not changed. The `same` test compares `final_label == self.current_regime`, but during M5 confirmation `current_regime` is still the *old* regime (e.g. `TRANSITION`), never the pending one. So a fresh H1 vote for the same pending always falls into the "Regime change: stage as pending" branch, which unconditionally executes `self.m5_confirmation_count = 0`.
- **Impact:** Production. M5s arrive 12 per H1 hour. If the third agreeing M5 close happens after the next H1 prints (i.e. after the 12th M5 of the hour, or just at the boundary), the counter is silently nuked and confirmation must restart from scratch. On noisy data the engine may *never* commit a non-VOLATILE regime, leaving the bot permanently stuck in TRANSITION + pending.
- **Suggested fix:** In `process_h1_close`, only reset `m5_confirmation_count` when the *pending* identity changes:
  ```python
  pending_changed = (
      final_label != self.pending_regime
      or final_dir   != self.pending_direction
  )
  ...
  if pending_changed:
      self.m5_confirmation_count = 0
  self.pending_regime, self.pending_direction = final_label, final_dir
  ```
  …and in the `same` branch keep the existing reset (because there *is* no longer a pending).
- **Test that should have caught this:** there is none. `test_h1_regime_change_resets_m5` only covers the case where H1 *changes* regime. A complementary test should drive H1 twice with the *same* pending output, interleaved with M5 progress, and assert that the counter is preserved.

### C2 — NaN indicators on a committed regime stage `pending=TRANSITION`, which then commits via the no-op M5 gate
- **Location:** `src/regime/classifier.py:167-173` + `src/regime/engine.py:169-198` + `src/regime/engine.py:304-306`.
- **Evidence (reproduced):**
  ```
  After committing TREND/BULLISH:   current=TREND/BULLISH
  Single NaN H1:                    pending=TRANSITION, count=0
  ```
- **Why it's wrong:** The classifier emits `TRANSITION` when `slope` or `bb_width` is NaN (e.g. an indicator-source dropout, a back-fill gap, a daylight-savings discontinuity). In `_apply_hysteresis`, the TREND-sticky exit checks gate on `not math.isnan(slope)`, so the slope-NaN path falls through and the engine returns `(TRANSITION, None)`. The "Regime change" branch then stages `pending=TRANSITION`. `_m5_validates` on `pending_label=TRANSITION` returns **True for every M5 close** (lines 304-306). So three M5 bars later — regardless of price — the engine commits `TRANSITION`, dropping a real trend on the floor because of one missing indicator value.
- **Impact:** Production. Any data gap, vendor outage or warmup window inside a streaming pipeline can demote a live TREND to TRANSITION inside ~15 minutes.
- **Suggested fix:** Two complementary changes:
  1. In `process_h1_close`, when `naive_label == TRANSITION` and `naive_reason == "insufficient_indicator_data"`, **do not** mutate `pending_*` or `current_*`. Treat the bar as a no-op:
     ```python
     if naive_reason == "insufficient_indicator_data":
         self.reason = naive_reason
         return
     ```
  2. Tighten `_m5_validates` to refuse anything other than `TREND`/`RANGE`/`VOLATILE`. Returning `True` for `TRANSITION` (or any future regime) is a foot-gun.
- **Test that should have caught this:** none. The classifier test `test_classify_nan_indicators_emits_transition` proves the classifier emits TRANSITION on NaN, but no engine test feeds a NaN row after a commit and asserts state is preserved.

---

## 2. HIGH

### H1 — Slope sign flip (+0.4 → −0.4) instantly stages a counter-direction TREND with no flat-band intermediate
- **Location:** `src/regime/engine.py:222-274` (`_apply_hysteresis`).
- **Evidence (reproduced):**
  ```
  After commit TREND/BULLISH:         current=TREND/BULLISH
  After slope flip (+0.45 → −0.45):   pending=TREND/BEARISH, count=0
  ```
- **Why it matters:** Literally read against the spec the exit threshold reads "bullish exits when `slope < 0.15`", and −0.45 satisfies that, so the exit fires. However the spirit of asymmetric hysteresis is to require the market to *pass through the flat band* before flipping bias. A direct sign flip implies a data spike, mis-printed bar or volatility event — not a regime change. Staging a counter-direction TREND immediately means the strategy gates can whipsaw from long-bias to short-bias in a single H1 print without going through RANGE/TRANSITION.
- **Suggested fix:** either (a) when the current regime is `TREND` and the naive direction has the opposite sign, classify as `VOLATILE` with reason `slope_sign_flip` and let the VOLATILE state machine recover, or (b) require the slope to spend at least one bar inside `[-0.15, 0.15]` before allowing the opposite-direction TREND.
- **Test gap:** no test exercises a direct sign flip.

### H2 — `is_live()` returns False while the *current* regime is still committed but a downgrade is pending
- **Location:** `src/regime/engine.py:86-101`.
- **Evidence (reproduced):**
  ```
  After committing TREND:                          live=True
  After staging pending=RANGE (m5_count=0):        live=False
  ```
- **Why it matters:** The applier writes `regime_live` per M5 bar. Once H1 emits a transition out of TREND, every downstream sweep strategy sees `regime_live=False` for the entire M5 sequence between this H1 close and the next regime resolution — even though the engine is still in TREND. If the pending fails M5 validation and gets cancelled by a contradicting H1, the bot has refused to trade TREND for an hour for no reason. This will materially affect signal coverage.
- **Suggested fix:** `is_live` should reflect *what is currently in force*, not whether something else is pending. Change the final line to `return True` for any committed regime other than `TRANSITION`. If the team wants a "transition in flight" signal, expose it as a separate boolean (`pending_in_flight`) rather than overloading `regime_live`.

### H3 — RANGE hysteresis suppresses genuine TREND emergence
- **Location:** `src/regime/engine.py:266-272`.
- **Evidence (reproduced):**
  ```
  current=RANGE, then H1 emits HH+HL/slope=0.50/bb_width=1.85 (prev=1.7, ratio 1.09):
     final: current=RANGE, pending=None, reason='classified'
  ```
  A textbook bullish breakout (clean fractal, +0.5 slope, no 20 % BB-expansion) is *silently rejected* because the BB-width hysteresis lower bound (`≤ 2.5`) holds the engine in RANGE. The reason code emitted is `"classified"` — the user has no way to tell from the log that hysteresis ate a TREND signal.
- **Why it matters:** Structure-driven and slope-driven TREND signals should win over BB-width inertia: width is a *secondary* signal in the spec's hierarchy (level 3, below structure and slope), but hysteresis collapses that hierarchy on RANGE exit. The classifier does priority right; the engine then undoes it.
- **Suggested fix:** make the RANGE-sticky branch skip when the naive label is TREND with structure-or-strong-slope backing. Concretely:
  ```python
  if self.current_regime == RegimeLabel.RANGE:
      structural_trend = naive_label == RegimeLabel.TREND and naive_dir is not None \
                         and abs(slope) >= SLOPE_TREND_ENTRY
      if not structural_trend and bb_width <= BB_WIDTH_RANGE_EXIT:
          return RegimeLabel.RANGE, None
  ```

### H4 — Same-timestamp event ordering silently assumes bar-close-anchored timestamps
- **Location:** `src/regime/applier.py:100-106`.
- **Why it matters:** The applier sorts events by `(timestamp, 0 if H1 else 1)` so H1 events fire first at a shared timestamp. This is only correct if H1 timestamps are bar-**close** anchored (i.e. `2025-01-01 00:00` is the bar from 23:00→00:00). For open-anchored data (the default of `pd.date_range(..., freq="1h")` in the test fixtures), the H1 bar tagged `00:00` actually closes at `01:00`, and processing it before the M5 bar at `00:00` is **lookahead bias**.
- **Evidence:** `tests/unit/test_regime_applier.py:49-66` builds both H1 and M5 with `pd.date_range(start="2025-01-01 00:00", ...)` — i.e. open-anchored — and the applier processes H1@00:00 *before* M5@00:00. The test passes because the assertions are coarse (`regime in {TREND, VOLATILE}`), so the lookahead is invisible.
- **Suggested fix:** either (a) require `df_h1` index to be bar-close-anchored (`label="right"` on resample), document it, and reject anything else; or (b) shift the H1 index by `+1h` inside the applier; or (c) add an explicit `h1_index_anchor: Literal["open", "close"]` parameter with no default. Document the choice. Update the test fixtures so they are unambiguous.

### H5 — `_m5_validates` returns `True` for `pending_label == TRANSITION`
- **Location:** `src/regime/engine.py:304-306`.
- **Why it matters:** Compound with C2: any time `TRANSITION` becomes pending, the M5 gate is a no-op, so three M5 bars commit `TRANSITION` regardless of price. Even without C2 in the picture, this is a latent bug ready to bite the next feature.
- **Suggested fix:** return `False` for any `pending_label` that is not `TREND` or `RANGE`. VOLATILE is already short-circuited above.

---

## 3. MEDIUM

### M1 — Compound structural pattern ignores swing recency
- **Location:** `src/regime/classifier.py:56-85` (`compute_structural_pattern`).
- The function pairs *the last two swing highs in the lookback window* with *the last two swing lows in the lookback window* and labels each pair in isolation. There is no constraint that one of those swings be the most recent event — both highs could be 9 bars old while the price has since printed two lows. The label still reads e.g. `HH+HL` because the comparisons within each type are independent.
- The structure module already has `get_structure_state` (`src/structure/state.py:113-130`) which *does* compute the last-event-aware label, so the regime module is duplicating + diverging from a sibling module.
- **Suggested fix:** factor the compound-pattern logic into `src/structure/` (see M2) and consider returning `"INSUFFICIENT_DATA"` when the most recent swing of either type is older than some max-age (e.g. ⌈lookback/2⌉ bars).

### M2 — `compute_structural_pattern` belongs in `src/structure/`, not `src/regime/`
- **Location:** `src/regime/classifier.py:56-117`.
- The function operates purely on swing positions and price arrays — no regime concepts. Placing it under `regime/` violates the module layering implied by the spec (`Structure > EMA slope > BB width > MACD`). Moving it under `src/structure/` (next to `get_structure_state`) lets both consumers share one implementation and one test surface.
- **Suggested fix:** move to `src/structure/patterns.py` (or fold into `state.py`), export from `src/structure/__init__.py`, import from the regime module.

### M3 — VOLATILE direction "sticks" across consecutive VOLATILE bars
- **Location:** `src/regime/engine.py:142-155`.
- When the engine is in VOLATILE and the next H1 is also VOLATILE, the engine updates `current_direction` *only if* `naive_dir is not None`. So a TREND-bullish volatility expansion (which carries `Direction.BULLISH`) followed by a structure-conflict bar (which carries `None`) leaves `current_direction = BULLISH` forever, which is a stale claim about market bias.
- **Suggested fix:** unconditionally assign `self.current_direction = naive_dir` (which may be `None`). Direction-less VOLATILE is a perfectly valid state.

### M4 — `reason` after a VOLATILE→TREND transition reads `"classified"` when the previous reason was hysteresis-mediated
- **Location:** `src/regime/engine.py:177-185`.
- When the naive label matches the current regime, the `same` branch overwrites `self.reason = naive_reason` (e.g. `"classified"`). But the bar arrived because hysteresis chose to *hold* the regime — not because the naive classifier put it there independently. The diagnostic value of the log is lost.
- **Suggested fix:** when `_apply_hysteresis` overrode the naive verdict, surface a distinct reason like `"hysteresis_hold"` (carry it through the return tuple).

### M5 — `RegimeState` TypedDict omits `pending_direction`, `pending_confidence` and `current_confidence`
- **Location:** `src/regime/state.py:36-43`.
- The spec only mandates the fields you've included, but `RegimeState` is used as a serialisation contract for the engine. Omitting `pending_direction` makes warm-restart impossible (you can't reconstruct an in-flight transition's direction from the snapshot). The applier output already exposes `regime_confidence` per M5 bar, sourced from `engine.current_confidence` — which is *not* in `RegimeState`. The contract is inconsistent.
- **Suggested fix:** add `pending_direction`, `pending_confidence`, `current_confidence`, `volatile_quiet_count` to `RegimeState`. They're cheap and they make `to_dict` round-trippable.

### M6 — Tests use monotonic synthetic data with no fractal swings
- **Location:** `tests/unit/test_regime_applier.py:43-67` (`_build_trending_h1`, `_build_trending_m5`).
- `np.linspace(1.30, 1.40, n)` produces a strictly monotonic series; no bar can satisfy `high > high.shift(-1) AND high > high.shift(-2)` (the right-side strict-greater test fails on monotonic data). Result: `structural_pattern == "INSUFFICIENT_DATA"` on every bar, so the classifier exercises *only* the slope branch and *never* the structure branch in the applier integration tests. The compound HH+HL / LH+LL / HH+LL paths are completely uncovered end-to-end.
- **Suggested fix:** add a fixture that injects synthetic swing highs/lows (sawtooth or sinusoidal price) so the structure branch actually runs through the applier.

### M7 — `test_apply_emits_trend_for_strong_uptrend` is tautological
- **Location:** `tests/unit/test_regime_applier.py:102-112`.
- The assertion is `tail["regime"] in {"TREND", "VOLATILE"}`. With four labels in the alphabet, accepting two of them — including the catch-all VOLATILE — provides almost no validation. A broken implementation that always emitted VOLATILE would pass this test.
- **Suggested fix:** assert the *specific* expected label, or add a stronger condition like "majority of post-warmup bars are TREND".

---

## 4. LOW

- **L1 — `test_apply_regime_can_change_within_dataset` is similarly weak.** Asserts only `len(distinct) >= 2`. A noisy implementation that constantly flapped TRANSITION ↔ TREND would pass.
- **L2 — No test for slope sign flip (H1) or repeated-pending-H1 (C1).** Already flagged; recording as test-coverage gap.
- **L3 — No test for `process_m5_close` called with NaN-bearing M5 row.** `_m5_validates` returns `False` (correct), but the behaviour is not asserted.
- **L4 — `applier.py:100-105` builds the event list eagerly.** Fine for v1 sizing (~10⁵ M5 bars per pair-year), worth flagging for the live-replay path. Generator-based merge would be O(1) memory.
- **L5 — Inconsistent column access in `classifier.py`.** Structural pattern uses `h1_row.get(...)` (lines 154-156) whereas indicator columns use `_row_get(...)` (which has slightly different defaulting semantics). Pick one.
- **L6 — `state.py:_format_time` stringifies an integer index** (`str(1)` → `"1"`). Harmless in production where timestamps are `pd.Timestamp`, but it makes the serialised contract awkward in tests.
- **L7 — `engine.py:_safe_float` silently swallows non-numeric.** Useful for robustness, but a malformed row should probably surface a warning rather than silently degrading to NaN.
- **L8 — `compute_structural_pattern` returns a bare string literal** (`"HH+HL"`, etc.) instead of an `Enum`. Cheap to misspell on the consumer side; the classifier's branch comparisons would silently fail.
- **L9 — `MODULE.md` and the `__init__` docstring claim "VOLATILE bypasses the M5 gate (volatility is by definition unstable)"** while the engine *also* contains a defensive branch in `process_m5_close` for `pending_regime == VOLATILE`. The defensive branch is unreachable in current code — keep it if you want, but the docstring is misleading without a "this is a guard for the case where a future code path stages pending VOLATILE without auto-committing" comment.

---

## 5. Conditional checklist (the merge-blockers)

To convert this review to APPROVE, the following must change:

1. **Fix C1** — preserve `m5_confirmation_count` when the pending identity is unchanged.
2. **Fix C2** — refuse to mutate state on `insufficient_indicator_data`, and tighten `_m5_validates` to reject non-{TREND,RANGE,VOLATILE} (H5 is the same change).
3. **Decide H1** — pick a policy on direct slope sign-flips and write a test that pins the behaviour.
4. **Decide H2** — `is_live()` semantics during pending downgrade; write a test that pins the behaviour.
5. **Decide H4** — document & enforce H1 timestamp anchor convention; switch the applier tests to whatever convention is chosen.

H3 (RANGE→TREND hysteresis trap) is *strongly* recommended but defensible if the team explicitly chooses to optimise for fewer false-positive TREND signals at the cost of slower TREND adoption.

---

## 6. Honest production-readiness call

This code will fail in production within the first day of live data. C2 specifically is a "happens whenever the indicator pipeline burps" failure mode — every cold-start, every overnight gap, every Sunday FX open will trigger it. The engine will quietly demote real regimes to TRANSITION and commit that state via the no-op M5 gate. C1 is more subtle but equally dangerous: on busy / choppy data the M5 counter will be reset so often by the next H1 close that the engine may never commit a non-VOLATILE regime at all. The hysteresis suite is mostly right but has clear edge-case holes (H1, H3) that won't show up in synthetic monotonic test fixtures. The tests pass because they don't probe these paths.

The architecture is sound — the classifier/engine/applier split is clean, the pure-function classifier is testable, and the spec is implemented at a high level. The bugs above are localised; none of them require an architectural rework. A focused day's work to fix C1, C2, H5 and add the missing test cases would put this branch in a mergeable state.
