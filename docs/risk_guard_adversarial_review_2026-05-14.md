# Adversarial review — `feature/risk-guard`

**Reviewer:** Independent Claude Code session (read-only)
**Date:** 2026-05-14
**Scope:** commit `ae7d67b` ("feat(risk): Phase 4 risk-guard with rules, circuit breakers, and regime engine emission log").
**Method:** code re-read across all 24 changed files, six behavioural probes, full suite (354 passed, 0 warnings, 1.77s).

---

## Summary

Phase 4 is well-organised — one orchestrator, five rules each in their own module, persisted state behind a clean dataclass, integration tests cover the rule-ordering pipeline. The locked design decisions in the spec are traceable to specific files. The "drift items" the author flagged in the brief check out against the code (RegimeEmission *is* a frozen dataclass, daily DD *does* reset at NY close — the commit message's "named tuple" / "midnight UTC" wording is stale, the code is correct).

That said the review found **one CRITICAL** bug that directly corrupts the regime-instability metric, **four HIGH** issues that materially deviate from the spec or create production risk, and a handful of MEDIUM/LOW items. Two of the four HIGH items revolve around the same root cause: `is_live()` returning `True` for VOLATILE — the spec wants "stable" but the code accepts "live".

| Severity | Count |
| --- | ---: |
| CRITICAL | 1 |
| HIGH     | 4 |
| MEDIUM   | 8 |
| LOW      | 7 |
| **Total** | **20** |

**Recommendation: APPROVE WITH CONDITIONS.** The CRITICAL bug (C1) and two of the HIGH items (H1 + H2 — both VOLATILE-treated-as-stable) must be fixed before this lands on `develop`. The other HIGH items are spec-ambiguity-but-defensible; landing them on a follow-up PR is acceptable.

---

## CRITICAL

### C1 — `was_m5_reset` fires on M5 commits, not just disagreements
- **Location:** `src/regime/engine.py:441` (the `was_m5_reset` calculation inside `process_m5_close`).
- **Spec contract (`docs/v1_architecture.md` §6.10, locked):**
  > "What counts as an 'M5 reset' — `m5_confirmation_count` transitions from `>0` to `0` (a **disagreeing M5 closed against an in-flight pending**)."
- **Implementation:**
  ```python
  pre_count = self.m5_confirmation_count
  ...
  self._process_m5_close_inner(m5_row)
  was_m5_reset = pre_count > 0 and self.m5_confirmation_count == 0
  ```
  This fires on BOTH transitions:
  - **Disagreement:** counter goes 1→0 via the `else` branch in `_process_m5_close_inner`. ✓ Spec-aligned.
  - **Commit:** counter goes 2→3, then `_commit_pending` resets it to 0. ✗ Not a "disagreeing M5".
- **Verified (probe):**
  ```
  After H1 stages pending TREND:
  M5 #1 agree → count=1, was_m5_reset=False
  M5 #2 agree → count=2, was_m5_reset=False
  M5 #3 agree (commits)  → count=0, was_m5_reset=True, committed=True   ← BUG
  ```
- **Impact (real, in production):** the risk layer's instability counter sums `was_m5_reset` over a 60-minute window and trips at `> 5`. Every successful M5 commit silently inflates the metric. A series of 6 legitimate regime commits in 60 minutes is over the threshold even with zero real disagreements. The breaker fires, the bot pauses for 1+ hour for no reason.
- **Test gap:** `test_emission_was_m5_reset_only_on_counter_drop_from_nonzero` covers disagreement (1→0) and not-a-reset (0→0, 0→1) but does **not** test the commit path (2→3→0). The bug therefore reaches main untested.
- **Suggested fix:** exclude commit-driven resets from `was_m5_reset`:
  ```python
  committed = self.current_regime != pre_regime
  was_m5_reset = (
      pre_count > 0
      and self.m5_confirmation_count == 0
      and not committed
  )
  ```
  Add a test that walks the engine through a full 3-M5 commit and asserts `was_m5_reset == False` on the third emission.

---

## HIGH

### H1 — `regime_live_at_last_h1_close()` returns True for VOLATILE → cooldown clears during volatility
- **Location:** `src/regime/engine.py:148-161, 216-225, 259`. The helper just returns `is_live()`, and `is_live()` returns `True` whenever `current_regime != TRANSITION`.
- **Spec contract (`docs/v1_architecture.md` §6.9.3):**
  > "Pause new entries for the affected pair for 1 hour, **or until a single regime holds `regime_live = True` for a full H1 close — whichever is longer.**"
  
  And §6.10:
  > "Pause duration — `max(1h, time-until-next-regime-live-H1-close)`."
- **Why it's wrong:** VOLATILE is "live" by the engine's definition (the H7 fix from the news-calendar review made VOLATILE executable for sweep strategies). But for the **instability cooldown**, the intent is the opposite — we are pausing precisely because the regime is unstable, and VOLATILE is the textbook unstable state. The cooldown extension should require the regime to be in a non-VOLATILE committed state.
- **Verified (probe):**
  ```
  After VOLATILE commit: current=VOLATILE, is_live=True
    regime_live_at_last_h1_close() = True   ← cooldown extension would pass here
  ```
- **Compound with C1:** if C1 trips the instability cooldown, H1 lets it clear after 1 hour even though the regime is still VOLATILE. The bot resumes trading mid-volatility, exactly the scenario the breaker exists to prevent.
- **Suggested fix:** in `regime_live_at_last_h1_close`, additionally require the current regime to be one of `{TREND, RANGE}` (i.e. a non-VOLATILE confirmed state):
  ```python
  def regime_live_at_last_h1_close(self) -> bool:
      return (
          self._last_h1_is_live
          and self._last_h1_regime not in (RegimeLabel.VOLATILE, RegimeLabel.TRANSITION)
      )
  ```
  Capture `self._last_h1_regime = self.current_regime` alongside the existing `_last_h1_is_live` snapshot. Add tests for the VOLATILE-after-cooldown scenario.

### H2 — `_last_h1_is_live` not updated when M5 commits — bot can be blocked an extra hour
- **Location:** `src/regime/engine.py:259` only updates `_last_h1_is_live` inside `process_h1_close`. The M5-driven commit path (`process_m5_close` → `_commit_pending`) does not touch the snapshot.
- **Verified (probe):**
  ```
  TREND committed via 3 M5s: current=TREND, is_live()=True
  After NaN H1:               current=TREND, _last_h1_is_live=True   (NaN bar refreshed it)
  ```
  But before the next H1 close lands, `_last_h1_is_live` remains False from the H1 close that staged the pending. So:
- **Production timeline:**
  - t=0: instability cooldown armed (1h primary).
  - t=10min: H1 close. Stages pending=TREND, current=TRANSITION → `_last_h1_is_live=False`.
  - t=15-25min: three M5s commit TREND. is_live()=True. But `_last_h1_is_live` stays False.
  - t=60min: primary cooldown elapses. Risk layer checks `regime_live_at_last_h1_close()` → False → extension blocks.
  - t=70min: next H1 close. `_last_h1_is_live` finally updates to True. Bot unblocks.
- **Impact:** up to ~60 min of *extra* blocked time after the regime has actually committed. Matches the spec literal reading of "next H1 close" (max(1h, next H1)) so it is defensible — but the extra hour of opportunity cost is real, and the test `test_regime_live_at_last_h1_close_not_updated_by_m5_only` explicitly asserts this behavior as if it were intentional.
- **Suggested fix:** also update `_last_h1_is_live` (and `_last_h1_regime` per H1's suggested fix) inside `_commit_pending` so a successful M5 commit immediately reflects on the helper. Add a test verifying that `regime_live_at_last_h1_close()` becomes True the moment the M5 commit lands.

### H3 — `apply_eod_force_close` ignores pending regime — TREND with pending RANGE survives overnight
- **Location:** `src/risk/rules/eod_enforcement.py:166-180`. The TREND-overnight check is:
  ```python
  if current_regime != RegimeLabel.TREND:
      orders.append(close)
  if current_direction != pos.direction:
      orders.append(close)
  # else: position survives overnight
  ```
- **Why it's wrong (subtle):** if at NY close the engine has *committed* TREND but has a *pending* RANGE awaiting M5 confirmation, `current_regime == TREND` (literal spec match) so the position survives overnight. But the regime is actively transitioning away from TREND — the locked decision "H1 regime still TREND" was likely written assuming a stable committed state.
- **Compound with H1 / H2:** in the post-instability extension window where `_last_h1_is_live` doesn't reflect the latest M5 commit, this becomes additionally surprising — the EOD check uses `current_regime` (which IS updated by M5 commits) while the instability extension uses a stale H1 snapshot.
- **Suggested fix:** additionally require `pending_regime is None` (or that `pending_regime == TREND` with the same direction) for TREND overnight hold. The spec brief explicitly asks about this case ("What if regime_live is False at EOD?") so the team has thought about it.

### H4 — Pre-EOD suppression allows TREND entry 25 min before close on Thu (Mon-Thu)
- **Location:** `src/risk/rules/eod_enforcement.py:80-82`.
- **Behaviour:** at Thursday 16:35 ET (25 min to NY close), a TREND candidate is allowed. The trade opens, fails to reach +1R in 25 min, gets force-closed at 17:00 ET by `apply_eod_force_close` with reason `trend_below_overnight_R`.
- **Verified (probe):**
  ```
  Thursday 16:35 ET, intended TREND: allow=True, reason=ok
  ```
- **Why HIGH:** the spec §6.10 locked decision is "suppress TREND only on Fridays (TREND can hold overnight Mon-Thu)". The implementation matches the literal spec — but the locked spec contains this footgun: a TREND opened at 16:35 Thursday has no path to honour its "hold overnight" promise because it cannot reach +1R in 25 min. The entry is then guaranteed to be a wasted spread + commission cost.
- **Suggested fix (one of):**
  1. Also suppress TREND inside the buffer (any weekday) unless the strategy layer can produce signals with `pnl_r` already realistically attainable. The spec's locked decision can be re-litigated — the author flagged "Spec-ambiguity resolutions" exactly so future revisions can.
  2. Or, in `apply_eod_force_close`, give TREND positions opened within the buffer a "grace bar" before force-closing. Less clean.
  3. Or accept the loss and document it as a known cost; the trade is statistically expected to be rare.

---

## MEDIUM

### M1 — Per-pair scope NOT enforced in cooldown lookup
- **Location:** `src/risk/rules/circuit_breakers.py:131-156` (regime-instability check). State has `regime_instability_pair` but the lookup never filters on `candidate.pair`.
- **Verified (probe):** an EURUSD candidate is blocked by a GBPUSD-armed cooldown.
- **Spec status:** §6.10 explicitly documents this as deferred to v2: *"v1 single-pair collapses this to global, but the state model carries `regime_instability_pair`."* So this is *acknowledged* incomplete forward-compat, not a bug per se. Flagging because the brief asked about per-pair scope and the implementation will need real work before v2.
- **Suggested fix:** in `check_circuit_breakers`, gate the cooldown check on `state.regime_instability_pair in (None, candidate.pair)`. Add tests.

### M2 — `record_trade_outcome` treats break-even (PnL=0) as a loss
- **Location:** `src/risk/rules/circuit_breakers.py:198-205`. `if pnl_r > 0:` resets; everything else (including 0) increments.
- **Spec status:** §6.9.2 says "4 consecutive losing trades". A 0R close is "not a win" but is it "a loss"? The spec is silent. The conservative interpretation (current code) is defensible.
- **Test pinning the behavior:** `test_record_trade_outcome_treats_zero_as_loss` — so it's a deliberate design choice. Worth surfacing in spec §6.10 as a locked decision so it doesn't drift.

### M3 — Loss-streak cooldown is rolling, not fixed-from-first-trigger
- **Location:** `src/risk/rules/circuit_breakers.py:206-210`. Each post-threshold loss re-arms `consecutive_loss_cooldown_until_utc = closed_at + 4h`.
- **Verified (probe):** loss #4 sets cooldown to t+4h. Loss #5 (2h later) extends cooldown to t+6h.
- **Spec status:** §6.9.2 says "pause new entries for 4 hours" — silent on subsequent losses. The current code is *more conservative* (cooldown rolls forward) than the simpler "fixed 4h from first trigger" reading. Defensible.
- **Suggested fix:** document the rolling-vs-fixed behavior as a §6.10 locked decision.

### M4 — `get_recent_emissions` silently drops events on tz-mismatch
- **Location:** `src/regime/engine.py:201-213`. If `now_utc` is naive and timestamps are tz-aware (or vice versa), the comparison raises TypeError and the entry is silently skipped.
- **Why MEDIUM:** the rule then sees an artificially-empty emission list, so the instability check passes when it shouldn't. The risk layer always passes `datetime.now(tz=timezone.utc)`, so production calls are naturally consistent. But the silent-skip pattern is fragile.
- **Suggested fix:** raise loudly on mixed-aware inputs (or normalise both to UTC before comparison) and add a test that fails on tz-mismatch rather than silently passing.

### M5 — JSON state writer is not atomic (acknowledged in docstring)
- **Location:** `src/risk/state/circuit_breaker_state.py:174-197`. `open("w")` truncates then writes. A crash mid-write or a concurrent writer corrupts the JSON.
- **Status:** docstring explicitly says "v1 has no multi-writer requirement; the JSON write is a plain truncate-and-write. If multi-process writes become a need in v2, switch to `tempfile` + `os.replace` for atomicity."
- **Suggested fix:** the documented fix is one line — write to `.tmp`, then `os.replace`. Cheap to do now; defers a future incident class.

### M6 — Daily DD cooldown end-time computed via mutation, not via `_next_ny_close`
- **Location:** `src/risk/rules/circuit_breakers.py:84-100`. The DD-trigger branch reimplements its own "next NY close" logic instead of calling `_next_ny_close` from `eod_enforcement.py`.
- **Why MEDIUM:** two copies of "compute next NY close" can drift. The reimplementation is similar but not identical (uses `time().hour >=` instead of `time() >= time(NY_CLOSE_HOUR_LOCAL, 0)` — equivalent for whole hours but not for sub-hour close times).
- **Suggested fix:** extract a single shared helper in a `risk/time_utils.py` and have both callers use it.

### M7 — `_FF_TO_FINNHUB` / `_PAIR_CURRENCIES` redundancy between news-calendar and risk-rules
- **Location:** `src/risk/rules/news_blackout.py:26-32` defines `_PAIR_CURRENCIES`. No single source of truth — the equivalent currency-set knowledge lives in `src/risk/news_calendar/matcher.py::_COUNTRIES_FOR_CURRENCY`. Adding a new pair requires touching both.
- **Suggested fix:** factor into a single canonical mapping or have the news_blackout rule derive the pair→currency tuples from the calendar's currency→country map.

### M8 — Commit message contradicts the implementation
- **Location:** commit `ae7d67b` message says "RegimeEmission named tuple" (it's a `@dataclass(frozen=True)`) and "daily reset at midnight UTC" (the implementation resets at NY close via `current_session_date_ny`).
- **Why MEDIUM (not LOW):** stale commit messages are the source of confusion in future incident reviews. Both contradictions exist in the *same* commit and the brief explicitly flagged them as drift items. Worth fixing in a follow-up commit (or via `git commit --amend` if the branch is rebased before merging).

---

## LOW

- **L1** — `test_emission_was_m5_reset_only_on_counter_drop_from_nonzero` does not cover the commit path. The C1 bug is therefore invisible to the suite. Strengthen by adding a third assertion that walks the engine to a 3-M5 commit and asserts `was_m5_reset == False` on the third emission.
- **L2** — DST test fixtures use only one date (winter Wed 2025-01-15). No test pins the **spring-forward** DST transition (March), which is the harder direction for zoneinfo (clocks jump 2:00 → 3:00, no ambiguity, but `.replace(hour=...)` could surprise).
- **L3** — Spread-filter rejection reason mixes formats: `"abs={abs_cap:.2f}p, atr_mult×ATR_M5={atr_cap if use_atr else 'n/a'}"` — when `use_atr` is True, `atr_cap` is interpolated as a raw float (no `.2f`). Cosmetic.
- **L4** — `record_trade_outcome` in `circuit_breakers.py:196` has a per-call import (`from ..constants import CONSECUTIVE_LOSS_THRESHOLD`). Cosmetic — pull to top.
- **L5** — `circuit_breaker_state.py:243` has `_ = timezone` placeholder for an unused-but-imported symbol. Replace with explicit `# noqa: F401` if intentional or remove the import.
- **L6** — `RiskDecision` and `RuleResult` both carry a string `rule` field. Could be `Literal["circuit_breakers", "position_caps", ...]` for static safety. Minor.
- **L7** — `news_blackout._PAIR_CURRENCIES` is private. Phase 5's execution layer may need to know which currencies a pair maps to (e.g. for HIGH-impact stop-modification gating). Consider exposing.

---

## Per-question response (review brief items 1-12)

| # | Question | Answer |
|---|---|---|
| 1 | Rule order short-circuits / warnings preservation | First reject returns immediately. `debug["pipeline"]` accumulates everything attempted. No "warning"-style accumulation across rules — a deliberate KISS choice, not in the spec. ✓ |
| 2 | Circuit breaker state | a) Reset at NY close handled via `current_session_date_ny`. ✓ b) JSON corruption → fail-open with warning log (per docstring). c) Truncate-write is not atomic, acknowledged (M5). |
| 3 | Regime instability | a) Commits = `current_regime` label change (label-only, not direction). b) **C1 — `was_m5_reset` fires on commits too.** c) Pause extension works correctly when last H1 was during pause, modulo VOLATILE (H1). d) Per-pair scope not enforced — global lookup (M1). |
| 4 | EOD enforcement | a) DST handled via zoneinfo; tested for winter only (L2). b) Friday boundary correct. c) **H3 — does not check pending state.** d) Pre-EOD uses DST-aware time. |
| 5 | News blackout | a) Pair → both currencies, verified by parameterised test. b) Stale cache fails closed via news_calendar's BlackoutResult. c) ±15 min window applied via news_calendar's `is_blackout(..., lookback_min=15, lookahead_min=15)`. ✓ |
| 6 | Spread filter NaN | NaN ATR → ATR cap ignored, abs cap applies, reason format awkward (L3) but functional. ✓ |
| 7 | Position caps | Per-regime cap applies across all pairs, so a TREND on GBPUSD blocks a TREND on EURUSD (v2 behavior). Correct per spec. ✓ |
| 8 | RegimeEmission log | a) `maxlen=2000` deque — adequate for the 60-min query window. b) NaN H1 closes correctly NOT marked committed (C2 guard preserves `pre_regime == self.current_regime`). c) ~6 days at H1+M5 cadence — no unbounded growth. ✓ |
| 9 | RiskDecision composition | No soft warnings, no cumulative cooldowns. Short-circuit on first reject. Spec doesn't require accumulation. ✓ |
| 10 | Tests | All under 5ms except `test_emission_log_capacity_is_bounded` (0.27s — pushes 2050 events). No clock-dependent flakiness. Fixtures use fixed historical dates. Coverage gap: L1, L2. |
| 11 | Spec adherence | C1 violates §6.10 locked decision on "M5 reset" definition. H1+H2 violate §6.9.3 "regime_live = True for a full H1 close" in the VOLATILE-is-stable interpretation. H3 stretches §6.5 in the pending-state edge case. H4 matches the spec literally but the locked decision has a footgun. M1 documented as deferred. |
| 12 | Type safety | All input types are `@dataclass(frozen=True)`. `RegimeEmission` is also frozen. ✓ Mutations of `dict`/`list` fields can technically still slip through because Python's frozen dataclass only prohibits attribute reassignment, not mutation of mutable values — but no rule does this. |

---

## Final recommendation: **APPROVE WITH CONDITIONS**

The architecture is sound and the spec is faithfully represented for ~90 % of the locked decisions. But the CRITICAL bug (C1) directly corrupts the instability metric, and the two HIGH issues (H1 + H2) compound the same VOLATILE-is-stable misinterpretation. Together they create a real-world failure mode: a series of legitimate regime commits trips the instability cooldown, which then clears mid-VOLATILE because `regime_live_at_last_h1_close()` accepts VOLATILE. The bot resumes trading exactly when it shouldn't.

**Conditions for merge:**

1. **Fix C1 (was_m5_reset on commits).** Exclude commit-driven resets. Add the missing test case. One-line code change.

2. **Fix H1 (VOLATILE treated as stable).** `regime_live_at_last_h1_close()` should additionally require the committed regime to be non-VOLATILE. Add a test for VOLATILE-after-cooldown.

3. **Decide H3 (pending state at EOD).** Either require `pending_regime is None` (or aligned-with-TREND) for overnight hold, OR document the carry-forward in §6.10 as a locked decision. Whichever — pick a side and pin a test.

**Recommended follow-ups (not merge-blocking):**

- H2 — update `_last_h1_is_live` from `_commit_pending` so the bot doesn't waste an hour after an M5-driven TREND commit. Borderline: matches a strict reading of the spec.
- H4 — reconsider TREND entry inside the EOD buffer on Mon-Thu. The current behavior burns spread on entries that cannot survive to +1R.
- M1, M2, M3 — three spec ambiguities worth pinning explicitly in §6.10 before someone else has to re-discover them.
- M5 — atomic JSON write is a one-line fix. Cheap defence in depth.
- M8 — amend the commit message to match the implementation (frozen dataclass, NY-close reset).
- L1, L2 — strengthen test coverage of the C1 commit path and the spring-forward DST edge.

After C1, H1, and H3 fixes the branch is in a good state to merge. The remaining items are best-effort improvements that don't block the v1 cut.
