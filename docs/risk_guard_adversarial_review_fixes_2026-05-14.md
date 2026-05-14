# Adversarial follow-up review — fixes for `feature/risk-guard`

**Reviewer:** Independent Claude Code session (read-only)
**Date:** 2026-05-14
**Scope:** commit `8a3581c` ("fix(risk): Phase 4 adversarial review fixes (C1, H1, H3) plus H2 docs + H4 diagnostic") on top of `ae7d67b`.
**Method:** `git diff ae7d67b..8a3581c`, code re-read of all six changed files, ten behavioural probes covering the fix surface and adjacent edge cases, full suite (364 passed, 0 warnings).

---

## TL;DR

The CRITICAL bug (C1) and both HIGH bugs that needed code fixes (H1, H3) are addressed at root-cause level. The H2 docstring expansion correctly captures the spec interpretation and accepts the up-to-one-H1-window opportunity cost. The H4 diagnostic-only change surfaces the locked-decision footgun without changing behavior.

Regression tests are tightly scoped — each fix has at least one test that would fail if the fix were reverted, and the C1 fix added the missing commit-path test that the prior suite lacked. Spec (§6.10) is updated in sync with the implementation: stale "named tuple" / "midnight UTC" claims from the original commit message are now properly reflected in the locked decisions.

**Recommendation: APPROVE FOR MERGE.** One LOW finding (N1, below) is worth a follow-up but does not block the cut.

---

## Verdict per original issue

### C1 — `was_m5_reset` excludes commit-driven drops → **RESOLVED** ✓

- **Fix:** `src/regime/engine.py:476-485`. Adds `committed = ...` calculation **before** `was_m5_reset` and conjuncts `not committed`:
  ```python
  committed = self.current_regime != pre_regime
  was_m5_reset = (
      pre_count > 0
      and self.m5_confirmation_count == 0
      and not committed
  )
  ```
- **Root-cause addressed:** yes. The bug conflated "counter dropped via disagreement" with "counter dropped via commit"; the fix gates on `not committed` so only disagreements register as resets.
- **Probe — agree×2 then commit:**
  ```
  M5#1 agree → count=1, was_m5_reset=False, committed=False
  M5#2 agree → count=2, was_m5_reset=False, committed=False
  M5#3 commits → count=0, was_m5_reset=False, committed=True   ← previously was True/True
  ```
- **Probe — agree×2 then disagree:**
  ```
  M5 disagree → count=0, was_m5_reset=True, committed=False    ← unchanged behavior
  ```
- **Tests pinning the fix:**
  - **New:** `test_emission_was_m5_reset_NOT_on_promotion_to_current` — walks all three M5 closes and asserts `was_m5_reset is False` on each; the third emission additionally asserts `committed is True, regime == TREND`. This is the exact test path that was missing from the prior suite (review L1).
  - **Preserved:** `test_emission_was_m5_reset_only_on_counter_drop_from_nonzero` continues to cover the disagreement path.
- **Spec sync:** `docs/v1_architecture.md` §6.10 updated — "M5 reset" now reads "transitions from `>0` to `0` **without a commit**" with a citation back to review C1.

### H1 — VOLATILE excluded from `regime_live_at_last_h1_close()` → **RESOLVED** ✓

- **Fix:** `src/regime/engine.py:150-155, 224-258, 294`. New `_last_h1_regime` field tracks the committed regime at the most recent H1 close (initial value `TRANSITION`). The helper now returns:
  ```python
  return self._last_h1_is_live and self._last_h1_regime in (
      RegimeLabel.TREND,
      RegimeLabel.RANGE,
  )
  ```
- **Root-cause addressed:** yes. The bug was that `is_live()` returns True for VOLATILE (necessary for sweep strategies), but the instability cooldown extension needs "non-VOLATILE stable" — the helper now encodes that distinction without changing `is_live` semantics.
- **Probe — VOLATILE H1 close:**
  ```
  current=VOLATILE, is_live=True, _last_h1_regime=VOLATILE
  regime_live_at_last_h1_close() = False   ← previously True (the bug)
  ```
- **Probe — TREND H1 close (matches_current path):**
  ```
  After M5 commits TREND → _last_h1_regime=TRANSITION (stale, H2-accepted)
  After next H1 close (TREND matches) → _last_h1_regime=TREND, helper=True
  ```
- **Initial-value safety:** `_last_h1_regime` defaults to `TRANSITION`, which is excluded from the allowed set. So a cold engine returns False before any H1 has been processed. ✓
- **Tests pinning the fix:**
  - **Intentionally flipped:** `test_regime_live_at_last_h1_close_VOLATILE_returns_false` (was `..._after_volatile_commit`, asserted True). Test renamed AND its assertion flipped from True to False. Docstring explicitly explains why (sweep strategies use `is_live`; instability breaker uses this stricter helper). A future reader can understand the flip from the test alone, with no commit-archaeology needed.
  - **New:** `test_regime_live_at_last_h1_close_true_after_committed_trend_h1` — pins the True case from a different angle (TREND committed via M5, then a TREND-matches H1 close, then helper returns True).
  - **Preserved:** `test_regime_live_at_last_h1_close_not_updated_by_m5_only` — pins the H2 accepted-behavior corner.

### H3 — EOD enforcement considers pending state → **RESOLVED** ✓

- **Fix (multi-part):**
  - `src/risk/rules/eod_enforcement.py:99-102, 214-243`. `apply_eod_force_close` gains keyword-only `pending_regime`/`pending_direction` parameters (default `None` for backward-compat). New gate inserted between the existing `current_direction` check and the "survives overnight" path: force-close if `pending_regime is not None` AND not `(pending_regime == TREND AND pending_direction == pos.direction)`.
  - `src/risk/guard.py:185-201`. `RiskGuard.positions_to_force_close` now reads `self.engine.pending_regime` and `self.engine.pending_direction` and forwards them to `apply_eod_force_close`.
- **Root-cause addressed:** yes. The bug was that the EOD rule only consulted `current_regime`, so an engine mid-transition (e.g. committed TREND but pending RANGE) silently rode the position overnight. The fix encodes the spec intent: "no contradicting pending transition" is now a peer of "still TREND, same direction".
- **Probe matrix — committed TREND/BULLISH, profitable position:**
  | `pending_regime` | `pending_direction` | Order? | Reason |
  |---|---|---|---|
  | None              | None    | 0 — survives | (overnight hold) |
  | RANGE             | None    | 1 — force-close | `trend_pending_transition (pending=RANGE, ...)` |
  | TREND             | BULLISH | 0 — survives | (benign reconfirmation) |
  | TREND             | BEARISH | 1 — force-close | `trend_pending_transition (..., pending_dir=BEARISH, entry_dir=BULLISH)` |
  | VOLATILE          | None    | 1 — force-close | `trend_pending_transition (pending=VOLATILE, ...)` |
- **Tests pinning the fix:**
  - `test_trend_closed_when_pending_regime_is_range`
  - `test_trend_closed_when_pending_regime_is_volatile`
  - `test_trend_closed_when_pending_opposite_direction_trend`
  - `test_trend_survives_when_pending_is_aligned_trend`
  - `test_trend_survives_when_pending_is_none`
  - `test_force_close_passes_engine_pending_state_through` — integration test that the guard correctly wires the engine's pending state through to the rule (not just unit-tests the rule).
- **Spec sync:** §6.10 new locked decision "TREND overnight hold gates" enumerates all four required conditions including the pending check.

### H2 — accepted as locked, docstring expanded → **DOCUMENTED** ✓

- **Change:** docstring on `regime_live_at_last_h1_close()` now explicitly explains the two-condition logic (VOLATILE excluded; M5 commits not reflected) with references to reviews H1 and H2 by date.
- **Spec sync:** §6.10 "Pause duration" entry expanded with the same explanation.
- **Why accepted:** the spec at §6.9.3 says "regime held live for a full H1 close". The implementation respects this strictly by only snapshotting inside `process_h1_close`. An M5-driven commit between two H1 prints is not "a full H1 close", so waiting for the next H1 is the correct strict reading. The up-to-one-H1 opportunity cost is documented and bounded.
- **Note:** the docstring is excellent — clearly distinguishes "is_live (sweep strategies)" from "regime_live_at_last_h1_close (instability extension)" and uses parenthetical citations to specific review IDs. Future readers will be able to reconstruct the design rationale.

### H4 — diagnostic note added, locked behavior unchanged → **DOCUMENTED** ✓

- **Change:** `src/risk/rules/eod_enforcement.py:162-175`. When `trend_below_overnight_R` fires AND the entry was inside the pre-EOD buffer (`held_min < PRE_EOD_NO_ENTRY_MIN`), the reason string appends a buffer-note diagnostic:
  > `(entry was 15.0min before close, inside 30min buffer — no path to 1.0R)`
- **Defensive math:** the `0 <= held_min < PRE_EOD_NO_ENTRY_MIN` guard correctly handles clock-skew edge cases (entry timestamp ahead of `now_utc` produces `held_min < 0`, no buffer note).
- **Tests pinning the fix:**
  - `test_trend_below_R_inside_buffer_flags_wasted_entry_in_reason` — positive case.
  - `test_trend_below_R_outside_buffer_omits_wasted_entry_note` — negative case (entry 9 hours earlier, no buffer note).
- **Spec sync:** §6.10 "Pre-EOD suppression" entry expanded to surface the known footgun.
- **Why this is appropriate:** the locked decision (suppress TREND only on Fridays) is preserved, but the wasted-entry pattern is now visible in production logs. Operators reviewing post-mortems can identify the scenario without re-running the strategy log.

---

## Interaction effects checked

### Threshold validity after C1 fix (brief item 4)
With `was_m5_reset` now excluding commits, the M5-reset counter only sums real disagreement-driven drops. The threshold (`> 5`) was set under the assumption that M5 resets meant disagreements — the fix now aligns the metric with that intent. **No threshold change needed.** The previous behavior over-counted (commits inflated the metric); the fix produces a strictly smaller count for the same input, so trips of the breaker now reflect genuine instability rather than legitimate transitions.

### H1 ∩ C2 (NaN H1 closes)
- A NaN H1 close on a committed TREND preserves `current_regime = TREND` (C2 guard) and updates `_last_h1_regime = TREND` (H1 fix). Helper continues to return True. ✓
- A NaN H1 close on a fresh engine leaves `current_regime = TRANSITION` and updates `_last_h1_regime = TRANSITION`. Helper returns False. ✓

### H1 ∩ VOLATILE recovery path (fall-through → M5 commit → next H1)
Probed full sequence:
```
VOLATILE commit            → _last_h1_regime=VOLATILE, helper=False
3 quiet H1 (fall-through)  → _last_h1_regime=VOLATILE (still), helper=False
3 M5s commit TREND         → _last_h1_regime=VOLATILE (stale, H2 accepted), helper=False
next TREND H1 close        → _last_h1_regime=TREND, helper=True
```
Confirms the H2-accepted up-to-one-H1-window opportunity cost. ✓

### H3 rule-order: `trend_below_overnight_R` fires before `trend_pending_transition`
If a TREND position is both below +1R AND has a contradicting pending, the R-check fires first (line 161 vs line 222 in `eod_enforcement.py`). Both close the position; only the reason string differs. **Acceptable** — the more "informative" reason (`trend_pending_transition`) is preempted by the more "stable" one (`trend_below_overnight_R` + H4 buffer note). Operators get useful diagnostics in both cases.

---

## New findings

### N1 — H3 backward-compat default silently disables protection → **LOW**
- **Location:** `src/risk/rules/eod_enforcement.py:99-102`. The new `pending_regime` and `pending_direction` parameters have `Optional[...] = None` defaults to preserve backward-compat with old callers.
- **Behavioural impact:** if `RiskGuard.positions_to_force_close` ever stops wiring the engine's pending state through (e.g. a future refactor breaks the call), `apply_eod_force_close` will silently return to the pre-H3 behavior — TREND with pending RANGE survives overnight again, with no warning logged.
- **Verified:** an old-style call `apply_eod_force_close([pos], now, current_regime=TREND, current_direction=BULLISH)` (no pending kwargs) survives a TREND with profitable pnl_r, exactly as before the fix.
- **Severity rationale:** the test `test_force_close_passes_engine_pending_state_through` does pin the integration — so a wiring regression in `RiskGuard.positions_to_force_close` would be caught immediately. But callers that bypass the guard and call `apply_eod_force_close` directly (e.g. in scripts or future code) get no compile-time signal. This is the kind of "silent backslide" that's worth removing.
- **Suggested fix:** drop the defaults and require the new kwargs:
  ```python
  def apply_eod_force_close(
      positions: list[OpenPosition],
      now_utc: datetime,
      *,
      current_regime: RegimeLabel,
      current_direction: Optional[Direction],
      pending_regime: Optional[RegimeLabel],
      pending_direction: Optional[Direction],
  ) -> list[ForceCloseOrder]:
  ```
  v1 has exactly one caller (`RiskGuard.positions_to_force_close`); the type-check break is contained. Optional → required is a minor API hardening with no real cost. Defer if the team wants to keep the kwarg as additive-only.

### N2 — No new findings worth a severity flag
I traced every fix's interaction with C2 (NaN guard), H7 (is_live semantics from prior review), M3 (VOLATILE direction stickiness from prior review), and the regime engine's sign-flip override. None produced a state combination that violates spec or a previously-pinned invariant.

---

## Test surface

- **Total:** 364 tests pass in 1.77s, zero warnings, zero skips.
- **New regression tests (10 added):**
  - `test_emission_was_m5_reset_NOT_on_promotion_to_current` — C1.
  - `test_regime_live_at_last_h1_close_VOLATILE_returns_false` — H1 (renamed + flipped).
  - `test_regime_live_at_last_h1_close_true_after_committed_trend_h1` — H1 positive case.
  - `test_trend_closed_when_pending_regime_is_range` — H3.
  - `test_trend_closed_when_pending_regime_is_volatile` — H3.
  - `test_trend_closed_when_pending_opposite_direction_trend` — H3.
  - `test_trend_survives_when_pending_is_aligned_trend` — H3.
  - `test_trend_survives_when_pending_is_none` — H3.
  - `test_trend_below_R_inside_buffer_flags_wasted_entry_in_reason` — H4.
  - `test_trend_below_R_outside_buffer_omits_wasted_entry_note` — H4.
  - `test_force_close_passes_engine_pending_state_through` — H3 integration.
- **Slowest:** `test_emission_log_capacity_is_bounded` at 0.26s. Unchanged.
- **Quality observation:** the H1 test docstring (~6 lines) is the clearest explanatory comment in the new suite — explicitly names is_live, the sweep-strategy use case, the breaker use case, and the rationale for excluding VOLATILE. Future-self readability is excellent.

---

## Final recommendation: **APPROVE FOR MERGE**

All four merge-blocking items from the prior review are addressed at root-cause level with regression tests tightly scoped to each fix. The spec doc is updated in sync with the implementation, so future drift between code and §6.10 starts from an aligned baseline. The H2 docstring expansion and H4 diagnostic-note treatment are appropriate non-code resolutions of the borderline cases.

The single new finding (N1 — keyword default silently disables H3) is a code-smell with a one-line hardening fix; it does not block the cut. None of the carry-forwards from the original review (M1–M8, L1–L7) are aggravated by this commit.

**Suggested follow-up backlog (not merge-blocking):**

1. **N1 — drop the keyword defaults on `apply_eod_force_close`.** One-line API hardening that removes a silent-regression possibility for direct callers.
2. **M5 (atomic JSON write)**, **M2/M3 (BE-as-loss + rolling cooldown locked decisions)** from the prior review — all best-effort items that could be picked up alongside Phase 5 wiring.

Ship it.
