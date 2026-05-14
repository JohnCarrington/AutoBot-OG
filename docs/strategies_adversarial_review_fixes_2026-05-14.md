# Adversarial follow-up review — fixes for `feature/strategies`

**Reviewer:** Independent Claude Code session (read-only)
**Date:** 2026-05-14
**Scope:** commit `7c0c083` ("fix(strategies): H1 session gate uses bar timestamp; pin M1/M2/M5 v1 decisions") on top of `2abc76f`.
**Method:** `git diff 2abc76f..7c0c083`, code re-read of all four changed source files and the new test cases, four behavioural probes covering the fix surface + the brief's RangeIndex / `df_h1`-unused concerns, full suite (445 passed, 0 warnings, 1.86s).

---

## TL;DR

The one HIGH item from the prior review (H1 — Liquidity Sweep session uses `current_time` not bar timestamp) is RESOLVED at root-cause level. Both bug directions are pinned by new regression tests. The three documentation items (M1 single-bar pullback, M2 BB midline source, M5 boundary inclusivity) are now first-class locked decisions in `docs/v1_architecture.md` §5.5 with review attribution, and each fixed source file carries an in-place docstring expansion that's clear enough to stand alone.

Two minor concerns surface from the fix's structure (silent RangeIndex rejection and the session check moving deeper into the function), but neither blocks the merge.

**Recommendation: APPROVE FOR MERGE.** Ship it.

---

## Verdict per original issue

### H1 — Liquidity Sweep session gate uses bar timestamp → **RESOLVED** ✓

- **Fix:** `src/strategies/liquidity_sweep.py:48-90`. The early-in-function `if not (london_session(current_time) or ...)` check is removed. After unpacking `sweep / reclaim / confirmation = df_m5.iloc[-3:-1]`, the new gate reads:
  ```python
  confirmation_ts = confirmation.name
  if not isinstance(confirmation_ts, datetime):
      return None
  if not (london_session(confirmation_ts) or ny_session(confirmation_ts)):
      return None
  ```
  `current_time` is preserved in the signature with `# noqa: ARG001` for dispatcher uniformity but no longer drives the gate.
- **Root cause addressed:** yes. The session check is now anchored to the bar that triggered the setup, not to whatever wall-clock the caller threads through. Live and backtest behaviour converge.
- **Verified (probe — both directions):**
  ```
  bar=Asia, current_time=NY:   signal=None   (was Signal — the original bug)
  bar=NY,   current_time=Asia: signal=Signal (was None — the mirror bug)
  ```
- **Tests pinning the fix:**
  - `test_session_gate_uses_bar_timestamp_not_caller_time` — Asia bars + NY current_time, asserts `None`.
  - `test_session_gate_accepts_when_bar_in_session_but_caller_off_session` — the mirror. The pre-existing `test_london_session_accepted` and `test_asia_session_rejected` already covered the case where both timestamps agree.
- **Quality observation:** the new test docstrings are excellent. They explicitly identify which is the buggy direction (bar=Asia/caller=NY) vs the mirror (bar=NY/caller=Asia) and explain why anchoring to the bar is correct.

### M1, M2, M5 — pinned as locked decisions → **DOCUMENTED** ✓

The §5.5 ambiguity-resolutions block in `docs/v1_architecture.md` now carries four new entries (the H1 fix + M1 + M2 + M5), each with a one-line citation back to "review 2026-05-14". Each affected source file also gained an in-place docstring section that explains the decision in context.

- **M1 (`ema_continuation.py:18-37`)** — "v1 inspects only `df_m5.iloc[-3]` as the pullback bar". The docstring explicitly explains the trade-off: a 2-bar retrace where the touch is on `iloc[-3]` qualifies; a 3+ bar retrace where the touch is older is missed. The structure check (`recent_pattern in {HH, HL}`) partially compensates by requiring consistent retrace structure over the 10-bar lookback. v2 backlog item.
- **M2 (`bb_reclaim.py:17-26`)** — TP anchored to `pierce.bb_mid_20_2`, not `confirmation.bb_mid_20_2`. Rationale spelled out: the rejection thesis references the pierce bar's mean; using the confirmation bar's mean would let the target drift toward entry, distorting R-multiples.
- **M5 (`bb_reclaim.py:28-36`)** — "**strict** on bars that drive the directional thesis (pierce `<`, confirmation `>`); **inclusive** on the bar that merely says 'we are no longer outside the band' (rejection `<= close <=`)". Clear convention; future revisions can re-litigate either direction.

The spec doc's wording is mirrored verbatim across source docstrings, so a reader landing in any one of the three places sees the same locked decision. No drift risk.

---

## Verification of the brief's specific concerns

### Q1: `df_m5.iloc[-1].name` access — DatetimeIndex assumption?
- **Behaviour verified:** with a default `RangeIndex`, `df_m5.iloc[-1].name` is an `int` (e.g. `14`). The defensive `if not isinstance(confirmation_ts, datetime): return None` causes the strategy to silently return `None`.
- **Failure mode:** silent rejection, no log line. A caller passing integer-indexed M5 data sees "no signal" with no diagnostic.
- **Severity rationale:** in production the dispatcher receives DatetimeIndex-ed frames from `apply_regime_to_candles` (Phase 3 contract). The only realistic risk is a test or backtest helper that builds raw `pd.DataFrame(rows)` without an index. Tests in this repo all use `pd.DatetimeIndex` explicitly (verified in `tests/unit/test_strategy_liquidity_sweep.py`). So this is **LOW** — a footgun for future code paths, not an active bug.
- **Suggested follow-up:** add a `logger.warning("[strategies.liquidity_sweep] confirmation bar timestamp not a datetime — skipping")` so the silent rejection becomes visible in logs. Or, more invasively, raise on contract violation.

### Q2: `# noqa: ARG001` annotations — are `df_h1` and `current_time` truly unused?
- **Probe:** passed a `Bomb()` object (raises `AttributeError` on every attribute access) as `df_h1`. The strategy returned a valid Signal — confirming `df_h1` is never read.
- **`current_time`:** same; only referenced in the signature.
- **Historical accuracy:** even *before* the fix, `df_h1` was unused — the original docstring claim "df_h1 is consulted only for the MACD confidence bump" was inaccurate (the original `_confidence` function for liquidity_sweep took `swing_level`, `sweep_extreme`, `atr_m5` only — no H1 reference). The fix corrects this misleading docstring while preserving the (correct, sweep-magnitude-driven) confidence calculation.
- **Suggested follow-up (LOW):** consider whether to keep both args for dispatcher uniformity or drop them. v1 takes the conservative path (keep + `# noqa`). Acceptable.

### Q3: M1 — single-bar pullback alignment with real markets?
- The locked decision is honest about its limitation: "A 3+ bar retrace where the EMA touch happened earlier than `iloc[-3]` is intentionally skipped". The structure check provides partial compensation by requiring HH/HL (bullish) over the 10-bar lookback — so a clean uptrend that retraces deep but then resumes will still trigger as long as the retrace's deepest point is the immediate prior bar.
- **Realistic miss case:** a 3-bar retrace where bar -5 wick-touched EMA50, bar -4 closed above EMA50, bar -3 was a small consolidation, and bar -2/-1 reclaim + confirm. The v1 strategy would not fire (because `iloc[-3]` did not touch the EMA). Defensible — the design says the touch must be the *most recent* M5 bar's wick.
- **Severity:** documented v2 backlog item, not a bug.

---

## New findings

### N1 — Silent rejection on integer-indexed `df_m5` → **LOW**
See Q1 above. The defensive `isinstance(confirmation_ts, datetime)` check is correct, but its failure mode (return `None` with no log) is opaque. A future user who builds `pd.DataFrame(rows)` directly in a test or backtest harness will see "no signal" without a diagnostic. Add a `logger.warning` or upgrade to a contract assertion.

### N2 — Session check moved deeper into the function → **LOW (perf nit)**
The original code rejected Asia-session bars *before* any DataFrame work. The fix moves the session check below `get_structure_state(df_m5)` and the `df_m5.iloc[-3:]` unpacking. For Asia-session bars, the function now does ~3 extra reads + one structure-state computation before rejecting. Pure functions, no side effects, negligible cost. But: if profile-driven optimisation ever becomes a concern, the session check can be hoisted back to the top by reading `df_m5.index[-1]` directly (instead of via `confirmation.name`):
```python
confirmation_ts = df_m5.index[-1]  # cheap; no bar materialisation
if not isinstance(confirmation_ts, datetime):
    return None
if not (london_session(confirmation_ts) or ny_session(confirmation_ts)):
    return None
# ... rest of function
```

### N3 — Redundant `source_ts = confirmation_ts` alias → **LOW (cosmetic)**
`src/strategies/liquidity_sweep.py:122`. After the H1 fix, `source_ts` is just a renaming of `confirmation_ts` that was already validated as a `datetime`. The pre-fix code had `source_ts = confirmation.name; if not isinstance(source_ts, datetime): return None` — that block became redundant after the session-check fix moved the isinstance check up. The author left the assignment in place for "consistency with the BB Reclaim / EMA Continuation pattern". Defensible.

---

## Carry-forwards from the prior review still open

| ID | Severity | Status |
|---|---|---|
| M3 — `regime_live` gate implicit | MEDIUM | STILL OPEN (no code change; H7 invariant from prior reviews holds) |
| M4 — Single-bar wick-only sweep variant | MEDIUM | STILL OPEN (documented v2 backlog) |
| L1-L7 from prior review | LOW | STILL OPEN; housekeeping |

None block merge.

---

## Probe summary (four scenarios, all green)

| # | Scenario | Result |
|---|---|---|
| 1 | Bar=Asia, current_time=NY (H1 bug case) | `None` ✓ |
| 2 | Bar=NY, current_time=Asia (mirror) | `Signal` ✓ |
| 3 | Integer-indexed df_m5 | `None` (silent) — LOW |
| 4 | `df_h1 = Bomb()` (raises on any attribute access) | `Signal` ✓ — confirms genuinely unused |

---

## Final recommendation: **APPROVE FOR MERGE**

The H1 fix is correct, well-tested, and well-documented. Both regression tests target the bug direction *and* its mirror, so a future revert would fail loudly. The three locked-decision documents (M1, M2, M5) are clear, cite the review by date, and live in both the spec and the source docstrings — future drift between them is unlikely.

The two new findings (N1 silent RangeIndex rejection, N2 session check moved deeper) are LOW-severity housekeeping that don't affect production correctness. The carry-forwards from the prior review are also LOW or documented v2 backlog.

**Suggested follow-up backlog (not merge-blocking):**

1. **N1 — log on RangeIndex rejection.** One `logger.warning` line; makes the silent failure mode visible.
2. **N2 — hoist session check to top of function via `df_m5.index[-1]`.** Cheap perf nit; defer until a profiler says so.
3. **M3 from prior review — make `regime_live` explicit** before any v2 work that loosens `is_live()` semantics.

Ship it.
