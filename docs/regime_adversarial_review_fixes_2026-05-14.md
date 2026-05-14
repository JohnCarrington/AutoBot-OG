# Adversarial follow-up review — fixes for `feature/regime`

**Reviewer:** Independent Claude Code session (read-only, working tree clean)
**Date:** 2026-05-14
**Scope:** commit `bf80827` ("fix(regime): adversarial review fixes (C1, C2, H1-H5, N1, N3) plus review docs") against `feature/regime`.
**Method:** `git diff 3880f2c..bf80827`, code re-read, six targeted state-machine probes, full test suite (`100 passed`).

> **Note on the prior fixes doc.** A previous follow-up review at this same path (now superseded by this version) was written against an earlier *working-tree* snapshot where N1 had not yet been patched. That doc lists N1 as "STILL OPEN / HIGH"; in the final commit, N1 is fixed (engine.py:202-217 contains the explicit pending-preserve branch). This independent re-review confirms N1 resolved and verifies the rest of the fix set against the actual committed code.

---

## TL;DR

All seven originally-flagged issues (C1, C2, H1-H5) are addressed at root-cause level; the two follow-up findings (N1, N3) raised during the previous self-review are also addressed. Regression tests are well-scoped and would fail loudly if any of the fixes were reverted. 100/100 tests pass with no warnings.

A handful of LOW-severity carry-forwards from the *original* review (M1, M2, M6, M7, L1 from `regime_adversarial_review_2026-05-14.md`) and one MEDIUM-severity carry-forward (M3 — stale VOLATILE direction across bars, **verified still present**, see below) are unaddressed. None of these blocks merge.

**Recommendation: APPROVE FOR MERGE.**

---

## Verdict per original issue

### C1 — M5 counter reset on every H1 close → **RESOLVED** ✓
- **Fix:** `engine.py:228-254` introduces three-way disambiguation: `matches_current` (clear pending, count=0), `matches_pending` (refresh metadata, **counter preserved**), and fresh-transition fallback (stage pending, count=0).
- **Root-cause addressed:** yes — the old `same` test conflated "no transition" with "in-flight transition unchanged", which is exactly why the counter was incinerated. The new disambiguation makes the distinction explicit.
- **Probe A — 5×H1 same-pending + alternating M5:**
  ```
  H1#1 → pending=TREND, count=0
  M5 agree    → count=1
  M5 disagree → count=0                (M5-driven reset — correct)
  H1#2 same   → count=0                (matches_pending; no reset because was already 0)
  M5 agree    → count=1
  H1#3 same   → count=1                (matches_pending; preserved)
  M5 agree    → count=2
  H1#4 same   → count=2                (matches_pending; preserved)
  M5 agree    → COMMIT TREND, count=0
  ```
- **Test:** `test_m5_counter_survives_repeated_h1_emits_same_regime` — directly asserts `count == 2` after the second H1 re-emit. Would catch reintroduction.

### C2 — NaN indicator demotes committed regime → **RESOLVED** ✓
- **Fix:** `engine.py:151-155` — early return at the top of `process_h1_close` when `naive_reason == "insufficient_indicator_data"`. State preserved bit-for-bit; only `self.reason` is updated so the no-op is visible in diagnostics.
- **Root-cause addressed:** yes — refuses to mutate before any pending/current state can be touched.
- **Tests:**
  - `test_nan_indicator_preserves_current_regime` — committed TREND survives a NaN-slope H1.
  - `test_nan_indicator_on_fresh_engine_stays_transition` — fresh engine + NaN stays in TRANSITION.
  - **Scope check (NaN combinatorics):** `slope=NaN` OR `bb_width=NaN` → classifier returns `insufficient_indicator_data` → C2 fires. `macd_hist=NaN` alone → classifier proceeds with `classified_no_macd` (N3 path). Verified by inspection of `classifier.py:166-173` and probe.
  - **Interaction with H1 sign-flip:** the C2 guard runs *before* the sign-flip override (`engine.py:151-178` is correctly ordered), so a NaN-slope can never trigger sign-flip-to-VOLATILE. Verified: a TREND-committed engine surviving a NaN bar shows `current=TREND/BULLISH, pending=None`.
- Defensive in-depth: H5 (below) makes `pending=TRANSITION` unreachable through normal H1 paths *and* unconfirmable if it ever got staged.

### H1 — Slope sign flip in committed TREND → **RESOLVED** ✓
- **Fix:** `engine.py:157-178` — pre-hysteresis override rewrites a TREND-→-opposite-TREND naive emission to `(VOLATILE, None, "slope_sign_flip", LOW)`. Routed through the explicit-VOLATILE branch in `_apply_hysteresis` so it auto-commits.
- **Root-cause addressed:** yes — encodes the principle "sign-flip is more likely a whipsaw than a regime change" at the engine layer (the classifier remains pure and direction-agnostic).
- **Edge probed:**
  - Sign-flip while in VOLATILE → override condition requires `current==TREND`, so does not fire from VOLATILE. Verified.
  - Sign-flip while in RANGE → override skips (current!=TREND). The H3 fix handles this case via the "TREND with direction breaks RANGE-stick" hysteresis branch. Verified.
  - Sign-flip while `current=TREND/BULL, pending=RANGE` *with bb_width expansion*: the classifier short-circuits to `volatility_expansion` (VOLATILE/BEAR) before the sign-flip override fires. End-state: `current=VOLATILE/BEARISH`. This is correct — both pathways converge on VOLATILE.
- **Tests:**
  - `test_slope_sign_flip_exits_trend` — single-bar sign flip pins immediate VOLATILE.
  - `test_slope_sign_flip_then_quiet_eventually_accepts_opposite_trend` — pins the cooldown + re-entry recovery path.
  - `test_oscillating_sign_flip_does_not_stick_in_volatile` — pins the N1 interaction (see below).

### H2 — `is_live()` suppressed during pending downgrade → **RESOLVED** ✓
- **Fix:** `engine.py:101` — `is_live()` simplifies to `current_regime != TRANSITION`. Pending state no longer suppresses liveness.
- **Root-cause addressed:** yes — the predicate now reflects what is *committed and in force*, which is the only thing the strategy layer cares about.
- **Tests:** `test_is_live_during_pending_downgrade` (TREND stays live with pending=RANGE), `test_is_live_initial_state_remains_false` (fresh engine with pending=TREND is still not-live because nothing is committed). Both edges pinned.

### H3 — RANGE hysteresis blocks TREND emergence → **RESOLVED** ✓
- **Fix:** `engine.py:355-364` — the RANGE-sticky branch defers to a `naive_label == TREND` with non-None direction. A clean HH+HL or strong-slope TREND breakout escapes the BB-width lock immediately.
- **Root-cause addressed:** yes — preserves the spec's signal hierarchy (Structure > slope > BB width) inside the hysteresis layer that previously inverted it.
- **Test:** `test_range_hysteresis_breaks_for_trend_emergence` — RANGE committed, then HH+HL/slope=0.50/bb_width=1.85, asserts pending=TREND/BULL.

### H4 — Open-anchored timestamps cause lookahead bias → **PARTIALLY RESOLVED** ⚠
- **Fix:** `applier.py:75-78, 110-117` — keyword-only `h1_anchor: Literal["close"] = "close"`. Any non-`"close"` value raises `NotImplementedError` with a `resample(label="right")` remediation hint. Module docstring documents the convention explicitly.
- **Root-cause addressed:** declaratively yes, programmatically no. The fix forces the caller to *say* they have close-anchored data, but does not *detect* open-anchored data from the index alone (because there is no general detection heuristic — both conventions look identical at the index level).
- **Caller exposure:** anyone who passes open-anchored data without naming the keyword still gets silent lookahead. The documentation now makes this an explicit caller responsibility.
- **Test:** `test_apply_rejects_unsupported_h1_anchor` — pins the rejection path with `h1_anchor="open"`.
- **Assessment:** this is the strongest declarative remedy available. Upgrading to RESOLVED would require either pipeline-wide enforcement of the convention or runtime heuristics (e.g. comparing H1 timestamps against the closing tick of the underlying M5 series). Acceptable as-is for v1.

### H5 — `_m5_validates` returning True for TRANSITION → **RESOLVED** ✓
- **Fix:** `engine.py:399` — final `return True` is now `return False`. Anything that isn't TREND/RANGE/VOLATILE is rejected.
- **Root-cause addressed:** yes — removes the no-op gate that compounded C2 in the unfixed state.
- **Test:** `test_m5_validates_rejects_transition` — directly stages `pending_regime=TRANSITION` (bypassing C2) and asserts the counter never advances across three M5 ticks. Defensive but valuable as a backstop against future regressions.

### N1 (follow-up) — Cooldown branch wiped pending after fall-through → **RESOLVED** ✓
- **Fix:** `engine.py:202-217` — the cooldown branch now only wipes pending if `pending_regime == VOLATILE` (defensive). Non-VOLATILE pendings staged by a prior fall-through survive across subsequent cooldown bars while M5 accumulates confirmations.
- **Root-cause addressed:** yes — preserves the recovery semantics implied by the H1 sign-flip path.
- **Probe B — fall-through + interleaved cooldown H1 + M5 confirm:**
  ```
  fall-through stages pending=TREND/BULL, _vq=0
  M5 agree → count=1
  M5 disagree → count=0    (M5-driven reset; correct)
  interleaved H1 → current=VOLATILE, pending=TREND/BULL preserved, count=0, _vq=1
  M5 agree x3 → COMMIT TREND/BULLISH
  ```
- **Test:** `test_oscillating_sign_flip_does_not_stick_in_volatile` — interleaves an H1 close (`bull`, same direction as pending) between M5 confirmations and asserts both that pending survives and that count is preserved. This addresses the N4 weakness the prior review flagged in `test_slope_sign_flip_then_quiet_eventually_accepts_opposite_trend`.
- **Edge probed:** NaN bar during cooldown → C2 guard fires before VOLATILE branch, so `_vq` does **not** advance. The cooldown clock pauses on data gaps. Defensible and probably desirable.

### N3 (follow-up) — MACD NaN diagnostic collision → **RESOLVED** ✓
- **Fix:** `classifier.py:251-271` — TREND classifications with NaN MACD now return `reason="classified_no_macd"`, confidence MEDIUM. RANGE classifications with NaN MACD keep `reason="classified"` (MACD is irrelevant there).
- **Root-cause addressed:** yes — separates "MACD disagreed" from "MACD unavailable" in the diagnostic surface.
- **Tests:** `test_classify_macd_nan_uses_no_macd_reason`, `test_classify_macd_nan_for_range_keeps_classified_reason`.

---

## New findings introduced by the fixes

### New issue 1 — None. ✓
The fixes are self-contained and ordered correctly:
1. C2 NaN guard (return early)
2. H1 sign-flip override (rewrite naive)
3. VOLATILE state machine (stay-or-cooldown; N1 preserve)
4. Hysteresis (with H3 deference to TREND-with-direction)
5. C1 three-way disambiguation (matches_current / matches_pending / fresh)
6. Commit (VOLATILE auto-commits)

I traced every cross-interaction listed in the brief (C2+H1, C1 + sign-flip + repeated H1, N1 + M5 disagree + interleaved H1, NaN during cooldown) and could not produce a state that violates the spec or a previously-pinned invariant.

### New issue 2 — N2 (oscillation stuckness) resolves itself once N1 is fixed → **NO ACTION**
Confirmed by `test_oscillating_sign_flip_does_not_stick_in_volatile`. Oscillating slope still spends time in VOLATILE — which is arguably the correct behaviour (oscillation *is* volatility) — but the engine is no longer permanently trapped: once slope settles to one direction, the engine recovers via the N1-preserved pending.

A *truly* unending oscillation (every bar exactly +0.4 / −0.4) will keep the engine in VOLATILE indefinitely. This is by design: the spec says VOLATILE exits when expansion stops AND three H1 closes are non-VOLATILE in a row. Oscillating slope arguably is "re-triggering volatility". Not a bug; not worth a separate gate.

---

## Carry-forwards from the *original* review that remain unaddressed

| ID | Severity | Status | Notes |
|---|---|---|---|
| M3 (stale VOLATILE direction) | **MEDIUM** | **STILL OPEN — verified** | `engine.py:189-200` updates `current_direction` only when `naive_dir is not None`. Trace: TREND/BULL → volatility_expansion (VOLATILE/BULL inherited) → structure_conflict bar (`naive_dir=None`) → engine reports `VOLATILE/BULLISH`. The BULLISH bias is now stale. Fix is one line (`self.current_direction = naive_dir` unconditionally). |
| M1 (compound structural pattern ignores recency) | LOW | STILL OPEN | `classifier.py:74-85` still pairs last-two-highs with last-two-lows without enforcing recency of the most recent event. |
| M2 (`compute_structural_pattern` placement) | LOW | STILL OPEN | Architectural smell. Function still lives in `src/regime/classifier.py` rather than `src/structure/`. |
| M6 (synthetic monotonic data in applier tests) | LOW | STILL OPEN | `_build_trending_h1` produces no fractal swings; structure branch of the classifier is never exercised end-to-end through the applier. |
| M7 / L1 (tautological applier assertions) | LOW | STILL OPEN | `test_apply_emits_trend_for_strong_uptrend` still accepts `TREND OR VOLATILE`. `test_apply_regime_can_change_within_dataset` still asserts only `>=2 distinct`. Both would pass a buggy implementation. |
| L4-L9 (engine internals: `_safe_float`, dtype handling, `_format_time`, defensive branches, MODULE.md drift) | LOW | STILL OPEN | None block production correctness. |

None of these affect the merge gate; they are explicitly out of scope for this fix wave.

---

## Probe summary (six scenarios)

| # | Scenario | Result |
|---|---|---|
| A | 4× H1 re-emit same pending with alternating M5 (agree/disagree/agree×3) | Counter accumulates correctly (`0→1→0→0→1→1→2→COMMIT`). ✓ |
| B | VOLATILE → 3 quiet H1 → 1 M5 agree → interleaved H1 → 2 more M5 agree | Pending preserved across interleaved H1; final commit succeeds. ✓ |
| C | Oscillating slope `+0.4/−0.4` × 12 H1 bars after committed TREND | Engine bounces between cooldown and fall-through, pending direction tracks last fall-through vote. Not stuck if direction eventually settles. ✓ |
| D | NaN slope during committed TREND | Committed regime preserved; pending=None; `reason="insufficient_indicator_data"`. ✓ |
| E | NaN slope during VOLATILE cooldown with pending=TREND from fall-through | Pending preserved; `_vq` does not advance (cooldown clock pauses on NaN). ✓ |
| F | TREND-BULL committed, then bb_width grows 1.4→2.0 and slope flips to −0.45 | Classifier short-circuits via `volatility_expansion`; engine commits VOLATILE/BEAR. Sign-flip override does not fire (classifier already promoted). ✓ |

---

## Final recommendation: **APPROVE FOR MERGE**

The seven explicit fixes (C1, C2, H1-H5) plus the two follow-up fixes (N1, N3) are all addressed at root-cause level, with regression tests scoped tightly enough to catch reintroduction. No new bugs were introduced by the fixes; the one new pre-existing finding (M3 — stale VOLATILE direction) is MEDIUM severity and orthogonal to the merge gate. Test suite is green (100/100, fastest 0.95s end-to-end).

The remaining LOW-severity carry-forwards from the original review (test quality, architectural smell, code style) are correctly deferred. M3 should be addressed in a follow-up commit (one-line fix at `engine.py:196-197`) but does not block this merge.

If the team has appetite for one more round of polish, the highest-leverage follow-up items, in order:
1. **M3 — one-line direction fix** (immediate; eliminates a stale-state edge that will eventually mislead the strategy layer).
2. **M6 — replace monotonic test fixtures with sawtoothed price** (one helper function; unlocks real end-to-end coverage of the structure branch).
3. **M7 — tighten the tautological applier assertions** (cosmetic test hygiene; defends against accidental regressions in classifier behaviour).

None of those are conditions for merge — they're follow-up backlog. **Ship it.**
