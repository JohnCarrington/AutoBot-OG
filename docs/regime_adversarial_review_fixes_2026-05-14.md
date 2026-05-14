# Adversarial review — fixes for `feature/regime`

**Reviewer:** Claude Code (follow-up self-review, working-tree state)
**Date:** 2026-05-14
**Scope:** uncommitted fixes on top of `3880f2c` for issues C1, C2, H1, H2, H3, H4, H5 from `regime_adversarial_review_2026-05-14.md`.
**Method:** code re-read + targeted behavioural probes (`/tmp/regime_probes.py`, six scenarios) + full pytest suite (97 passed).

---

## Verdict per original issue

### C1 — M5 counter reset on every H1 close → **RESOLVED**

The fix splits `process_h1_close` into a three-way disambiguation: `matches_current` (clear pending, count = 0), `matches_pending` (refresh metadata, **count preserved**), and the fresh-transition path (stage pending, count = 0).

Probe A (5×H1 emitting `TREND_BULLISH` with M5s alternating agree/disagree/...) walks through the state machine:

```
H1 #1 → pending=TREND/BULLISH, count=0
M5 agree → count=1
M5 disagree → count=0          (counter reset on bad M5 — correct)
H1 #2 (same) → pending unchanged, count=0   (matches_pending; preserved)
M5 agree → count=1
H1 #3 (same) → pending unchanged, count=1   (matches_pending; preserved)
M5 agree → count=2
H1 #4 (same) → pending unchanged, count=2   (matches_pending; preserved)
M5 agree → count=3 → COMMIT → current=TREND/BULLISH
```

`test_m5_counter_survives_repeated_h1_emits_same_regime` exercises the critical "H1 re-emit doesn't reset" assertion. Root cause addressed, not a symptom-patch.

### C2 — NaN indicator demotes committed regime → **RESOLVED**

Early-return at the top of `process_h1_close` on `naive_reason == "insufficient_indicator_data"`. State preserved bit-for-bit (verified by Probe E: committed `TREND/BULLISH` survives a NaN-slope H1 close). Diagnostic `reason` is updated so the no-op is visible.

`test_nan_indicator_preserves_current_regime` and `test_nan_indicator_on_fresh_engine_stays_transition` pin both the committed and fresh-engine paths.

**Caveat (see N3 below):** the C2 guard only fires for slope-OR-bb_width NaN. MACD NaN currently falls through to the classifier's regular logic, which silently produces `Confidence.MEDIUM` (because NaN comparisons evaluate False). Defensible but worth documenting.

### H1 — Slope sign-flip routes through VOLATILE → **RESOLVED**

Pre-hysteresis override in `process_h1_close` rewrites `naive` to `(VOLATILE, None, "slope_sign_flip", LOW)` whenever a committed `TREND` sees a counter-direction naive emission. The override is correctly ordered **after** the C2 guard (so NaN-during-flip degenerates to a no-op, per Probe E) and **before** the VOLATILE state machine (so the new VOLATILE auto-commits via the explicit-VOLATILE branch).

`test_slope_sign_flip_exits_trend` and `test_slope_sign_flip_then_quiet_eventually_accepts_opposite_trend` pin both the immediate demotion and the eventual recovery to the opposite TREND.

**Caveat (see N1 below):** the *eventual recovery* path is brittle in production-realistic interleaved data; see the new finding.

### H2 — `is_live()` suppressed during pending downgrade → **RESOLVED**

`is_live() = (current_regime != TRANSITION)`. Pending transitions no longer suppress liveness on the committed regime. `test_is_live_during_pending_downgrade` and `test_is_live_initial_state_remains_false` pin both sides.

### H3 — RANGE hysteresis blocks TREND emergence → **RESOLVED**

The RANGE-sticky branch in `_apply_hysteresis` now defers to a `naive_label == TREND` with non-None direction. Structure-backed or strong-slope breakouts escape the BB-width lock immediately. `test_range_hysteresis_breaks_for_trend_emergence` pins this.

### H4 — Open-anchored timestamps cause lookahead bias → **PARTIALLY RESOLVED**

The fix adds a keyword-only `h1_anchor: Literal["close"] = "close"` and raises `NotImplementedError` for anything else. This satisfies "explicitly document and test the close-anchored assumption" (the docstring spells out the convention; the test exercises the rejection path).

The original review's stronger request was "fails loudly if open-anchored timestamps are *detected*". The fix does not auto-detect — it only fails loudly when the caller explicitly declares `h1_anchor="open"`. A caller who feeds open-anchored data without overriding the default will still get silently lookahead-biased output.

**Why partial:** no reliable detection heuristic exists from index data alone — close-anchored and open-anchored DataFrames are byte-identical at the index level. The fix is the strongest declarative remedy available; absent heuristics, it relies on caller hygiene. Documenting this gap is the only honest path.

### H5 — `_m5_validates` returning True for TRANSITION → **RESOLVED**

The final `return True` in `_m5_validates` is now `return False`. Combined with C2, this makes pending=TRANSITION unreachable through the normal H1 path. `test_m5_validates_rejects_transition` exercises the defensive path by directly injecting `pending_regime = TRANSITION` and verifying the counter stays at zero.

---

## New findings

### N1 — Cooldown branch wipes pending after fall-through → **HIGH** (newly exposed by H1 fix; pre-existing bug in Phase 3 engine)

Reproduced by Probe B:

```
[VOLATILE committed]      pending=None, _volatile_quiet=0
[quiet H1 #1]             pending=None, _volatile_quiet=1, reason='volatile_cooldown'
[quiet H1 #2]             pending=None, _volatile_quiet=2, reason='volatile_cooldown'
[quiet H1 #3 FALL THROUGH] pending=TREND/BULLISH, _volatile_quiet=0
[quiet H1 #4]             pending=None,           _volatile_quiet=1, reason='volatile_cooldown'   ← WIPED
```

**Root cause:** `engine.py:202-209`. The VOLATILE cooldown branch unconditionally sets `self.pending_regime = None` and `self.m5_confirmation_count = 0` on every non-fall-through H1 close. After a fall-through stages `pending = TREND/X`, the very next H1 close re-enters the VOLATILE block (because `current_regime` is still VOLATILE — the commit only happens via M5 validation), the cooldown branch fires, and the just-staged pending is destroyed before M5 has had a chance to confirm.

**Production impact (real, on choppy data):** the VOLATILE → TREND recovery only succeeds if **three consecutive M5 confirmations occur within the same H1 window after fall-through** (≤ ~55 minutes of M5 data, but in practice ≤ 15 minutes since they must be consecutive). On choppy data where M5 alternates, the pending is wiped before commit and the engine restarts cooldown from `_volatile_quiet_count = 0`. The H1 fix's whole point was to use VOLATILE as a recovery buffer; N1 makes that buffer hard to exit.

**Why the H1 fix exposes a pre-existing bug**: under the old code, the only path into VOLATILE was an explicit volatility-expansion or structure-conflict event, and the recovery path was rarely exercised in tests. The new sign-flip-to-VOLATILE path routes far more H1 events through that recovery, increasing the surface area where N1 matters.

**Why the test suite passes anyway:** `test_volatile_exit_requires_three_quiet_h1_closes` stops at the fall-through bar. `test_slope_sign_flip_then_quiet_eventually_accepts_opposite_trend` processes its three confirming M5s consecutively with no interleaved H1 — i.e. the test pretends the M5 stream is dense enough to commit before the next H1 fires, which is the only sequencing where N1's destruction doesn't bite.

**Suggested fix (one-line, narrow scope):** in the cooldown branch (`engine.py:203-209`), only clear pending when there is no existing non-VOLATILE pending. Roughly:

```python
self._volatile_quiet_count += 1
if self._volatile_quiet_count < VOLATILE_EXIT_QUIET_H1_BARS:
    self.reason = "volatile_cooldown"
    # Do NOT wipe a non-VOLATILE pending — once fall-through has staged
    # a transition, M5 needs the chance to confirm it. Only clear pending
    # if it is somehow VOLATILE (defensive — shouldn't normally happen).
    if self.pending_regime == RegimeLabel.VOLATILE:
        self.pending_regime = None
        self.pending_direction = None
        self.m5_confirmation_count = 0
    return
```

Combined with the C1 fix's `matches_pending` branch, this lets a pending stage by fall-through survive across multiple subsequent cooldown bars while M5 accumulates confirmations.

**Suggested new test:** `test_volatile_fall_through_pending_survives_subsequent_h1_close` — drive `VOLATILE`, three quiet H1s (stage pending), then one M5 confirmation, then one more quiet H1, then two more M5 confirmations, assert that final commit succeeds.

### N2 — Oscillating sign-flip data permanently sticks the engine in VOLATILE → **MEDIUM**

Reproduced by Probe D (slope alternating +0.4 / −0.4 every H1):

```
H1 #1 (bear from TREND/BULLISH) → current=VOLATILE (sign_flip)
H1 #2..#3 (bull, bear) → cooldown, count=1, 2
H1 #4 (bull) → fall-through, pending=TREND/BULLISH
H1 #5 (bear) → cooldown wipes pending, count=1   ← N1 in action
H1 #6..#7 (bull, bear) → count=2, fall-through, pending=TREND/BEARISH
H1 #8 (bull) → cooldown wipes pending, count=1
... and so on, forever
```

This is the *combination* of N1 (pending wiping) and the VOLATILE state machine's directional-agnostic cooldown counter. Each fall-through stages a pending in whatever direction the third quiet bar happened to vote, and the next H1 wipes it. The engine is essentially stuck.

**Severity assessment:** if you take the spec literally — "VOLATILE: exit when expansion stops AND 3 H1 closes without re-trigger" — oscillating slope arguably *is* re-triggering volatility, and staying VOLATILE is correct. But the engine doesn't model "oscillation = re-trigger" explicitly; it just inherits the behaviour from N1.

If N1 is fixed, N2 becomes much less severe: a pending will survive long enough for at least *some* M5 confirmation to either commit it or invalidate it via disagreement.

### N3 — MACD NaN silently degrades confidence without surfacing → **LOW**

Reproduced by Probe C: with `macd_hist = NaN` and clean structure + slope, `classify_h1` returns `(TREND, BULLISH, MEDIUM, "classified")`. The MEDIUM is because `macd_agrees = (BULLISH and NaN > 0)` evaluates False, not because MACD actively disagrees.

The C2 guard checks slope and BB-width only; MACD NaN slips past. The engine still classifies and stages pending, just at a lower confidence tier.

**Defensible** because MACD is the lowest-priority signal (confidence-only) and the spec doesn't require it for classification. But the diagnostic surface is misleading: `reason="classified"` and `confidence=MEDIUM` look identical to a real MACD-disagrees signal.

**Suggested fix (cosmetic):** when `macd_hist` is NaN, emit a distinct reason code like `"classified_no_macd"` or set `confidence` to a new tier (e.g. `LOW`), or expand the C2 guard to include MACD. Pick one and document.

### N4 — Test gap: `test_slope_sign_flip_then_quiet_eventually_accepts_opposite_trend` is tightly coupled → **LOW**

The test happens to process its three confirming M5 closes consecutively with no interleaved H1 — which is the *only* sequencing under which N1 doesn't bite. A test that interleaves an H1 close between the M5 confirmations would have exposed N1 immediately.

This is the same shape of issue the original review flagged for the Phase 3 tests: monotonic / synthetic data structure papering over real-world edge cases. Worth strengthening regardless of whether N1 is fixed.

---

## Cross-cutting interactions verified

| Interaction | Probe | Behaviour |
|---|---|---|
| C2 NaN + would-be sign flip | E | C2 guard returns first; sign-flip override never sees the bar. ✓ |
| C1 same-pending preservation across many H1s | A | Counter accumulates correctly through 4× H1 re-emits + 5× M5 mix. ✓ |
| Sign-flip during committed VOLATILE | D | Sign-flip override condition requires `current == TREND`, so does not fire from VOLATILE. The cooldown counter advances normally. ✓ |
| MACD NaN + valid slope/bb_width | C | TREND classification proceeds with `MEDIUM` confidence. Acceptable. |
| Happy path: VOLATILE → 3 quiet H1 → 3 consecutive M5 → TREND | F | Works. ✓ (This is the path test_slope_sign_flip_then_quiet exercises.) |

---

## Final recommendation: **APPROVE WITH MINOR CONDITIONS**

The seven explicit issues from the original review are addressed at the root-cause level (six fully RESOLVED, H4 partially resolved with documented rationale). The fix design is coherent — pre-hysteresis override for sign flips, three-way disambiguation for the C1 counter, declarative anchor parameter for H4 — and the regression tests pin each fix at exactly the right level.

**Conditions for merge into `develop`:**

1. **N1 must be either fixed or explicitly logged as a follow-up TODO with severity and remediation.** The H1 sign-flip-to-VOLATILE recovery is materially weakened by N1; merging without acknowledgement would leave a documented bug in the engine's recovery path.

2. **Strengthen the sign-flip recovery test (N4).** Either modify `test_slope_sign_flip_then_quiet_eventually_accepts_opposite_trend` to interleave at least one H1 close between M5 confirmations, or add a new `test_volatile_recovery_survives_interleaved_h1` test. The current test gives false confidence.

3. **N3 (MACD NaN diagnostic) is optional cosmetic** — defer to future iteration unless the team wants tighter diagnostic separation now.

4. **N2 (oscillation stuckness) is a downstream consequence of N1** and resolves itself if N1 is fixed. No separate action needed.

If N1 is patched per the suggested one-line fix above, I would upgrade this to a straight **APPROVE FOR MERGE**.
