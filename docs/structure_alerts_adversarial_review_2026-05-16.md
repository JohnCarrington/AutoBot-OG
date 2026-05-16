# Adversarial review — `feature/structure-alerts` (Phase 12 — Structure Alerting)

**Branch:** `feature/structure-alerts`
**Base:** `develop` (869cb13)
**Stack:** 6 commits (C-1 → C-6); `git diff --shortstat`: 32 files, **+5436 / −13**
**Test suite:** **1146 passed** in 5.79s (matches plan target).
**Review date:** 2026-05-16. Read-only.

---

## Headline

Phase 12 is largely well-engineered. The locked-decision discipline holds:
catalogue (STRUCTURE category + 9 subtypes) is wired, severity table is
locked at `severity_for(kind)` and read at every construction site, the
cold-start contract is enforced in two places (diff layer + processor),
dedupe key shapes match spec §9, and the BotLoop integration places the
dispatch call at the correct point in `_handle_bar_close` (after
`log_structure_state`, before the periodic-tasks and signal-pipeline
blocks) with three concentric layers of failure isolation.

The dedupe-cache invariants (severity-late-bound, blocked-attempt
non-mutation, backwards-clock-skew fires, boundary inclusive at `elapsed
== cooldown`) are correct and pinned in tests. The JSONL persistence is
crash-safe (OSError swallowed + WARNING-logged + outer BotLoop try/except
for non-OSError) and the threading lock is in place even though the
producer is single-threaded today.

**No CRITICAL findings.** One HIGH that is a real, bounded, one-time
alert-spam regression at first post-upgrade restart; the rest is MEDIUM
documentation/test-quality nits and LOW polish.

The HIGH-shaped finding (**H1**) is the gap between the test docstring's
claim ("score defaults to 0 → no spurious NEW_MAJOR_LEVEL") and the
actual diff-layer behaviour. The reasoning underlying the locked
decision doesn't hold because the threshold check tests CURR's score,
not PREV's. The user-facing impact is one bounded burst of INFO alerts
on the first BotLoop restart after this branch lands in production.
Easy to fix and easy to live with as-is if the operator is warned.

---

## Findings

### H1 (HIGH — spurious NEW_MAJOR_LEVEL alert burst on first post-upgrade restart) — diff layer's `prev_keys` ignores `prev.nearest_*`

**Files:**
- `src/structure_alerts/diff.py:262-275` (the actual logic)
- `tests/unit/structure_alerts/test_hydration.py:149-165` (encodes the
  faulty reasoning)
- `src/structure_alerts/hydration.py:184-231` (the cause: rehydrated
  `prev.levels` is empty for pre-refinement-A records)

**The claim under review.** Locked decision #5c says:

> Pre-refinement-A records (no score, no levels) → score defaults to
> 0.0 — no spurious NEW_MAJOR_LEVEL on first post-restart bar.

`test_nearest_score_missing_defaults_to_zero` in `test_hydration.py`
echoes this:

```python
"""Pre-refinement-A record (no nearest_*_score field) doesn't
crash; score defaults to 0. NEW_MAJOR_LEVEL won't fire for the
rehydrated nearest_* (below STRONG_LEVEL_THRESHOLD), which is
the conservative direction."""
```

**Why the reasoning doesn't hold.** Walk the actual diff code at
`diff.py:265-275`:

```python
prev_keys = _level_key_set(curr.pair, prev.levels)
for nearest in (curr.nearest_support, curr.nearest_resistance):
    if nearest is None:
        continue
    if nearest.score < STRONG_LEVEL_THRESHOLD:
        continue
    if (level_side(nearest), quantise_price(curr.pair, nearest.price)) in prev_keys:
        continue
    changes.append(
        StructureChange(kind=ChangeKind.NEW_MAJOR_LEVEL, level=nearest)
    )
```

The threshold check on line 269 reads `nearest.score` — i.e. **curr's**
nearest, not prev's. The rehydrated `prev.nearest_*.score = 0.0`
default doesn't enter the calculation at all. The "score defaults to
0.0 → no fire" causal chain is broken.

What actually matters for the NEW_MAJOR_LEVEL decision: is
`(side, Q(curr.nearest.price))` present in `prev_keys` (i.e.
`prev.levels`)?

**What pre-refinement-A jsonl records look like in hydration.** Old
records (written before this branch) had `nearest_support`,
`nearest_resistance`, `htf_bias`, etc. — but no `nearest_*_score` and
no compact `levels` list. After hydration:

- `prev.nearest_support` = rehydrated synthetic `StructureLevel`
  (`hydrated_fallback=True`, score=0.0, timeframe="H1").
- `prev.nearest_resistance` = same shape.
- **`prev.levels` = `[]`** (because `rec.get("levels")` returns `None`
  → `_rehydrate_levels` returns `[]`).
- `prev.htf_bias`, `prev.structure_mode`, etc. — all preserved.

**The first post-upgrade bar after restart.** Diff runs with
`prev=rehydrated_state, curr=engine_live_state`:

1. HTF / mode / reaction diffs — all behave correctly (those fields
   were always written, hydration recovers them faithfully).
2. NEW_MAJOR_LEVEL: `prev_keys = ∅` (empty). For each
   `curr.nearest_{support,resistance}` that survived
   `STRONG_LEVEL_THRESHOLD` (default 6.0), `(side, Q(price))` is never
   in `prev_keys`. **FIRES.**
3. LEVEL_INVALIDATED: `curr_keys` is populated from `curr.levels`
   (engine emits a real catalogue every bar). The rehydrated
   `prev.nearest_*` is checked against `curr_keys`; if curr still has
   the level, no fire. So this side is symmetrically protected.

**Impact.**

- Up to **2 spurious NEW_MAJOR_LEVEL events per pair** on the first
  bar after the first post-upgrade restart. For 4 pairs, that's a
  burst of up to 8 INFO alerts arriving as one Phase 9 coalesce window.
- INFO severity, not CRITICAL — operator inbox annoyance, not paging.
- **One-time.** After this bar, the next `_handle_bar_close` writes a
  new engine jsonl record with the compact `levels` payload; the
  next restart reads it and `prev.levels` is fully populated.
- Bounded in cardinality (≤ 2 × pair_count). Dedupe is fresh on
  restart, so all events fire; subsequent matches within 2h block.

**Severity rationale.**

- Not CRITICAL — INFO bursts only, no operator paging, no trade impact.
- Not LOW — it's a real alert-spam regression, the user explicitly
  asked the design to prevent it, the test docstring lies about
  preventing it, and the inferred locked-decision reasoning is wrong.
  Categorising lower would hide the false invariant.
- HIGH — the locked decision is violated by the implementation; the
  test that supposedly pins the invariant doesn't. Easy and important
  to fix before this is rebuilt mentally by the next reviewer.

**Suggested fix (small).** Include `prev.nearest_support` and
`prev.nearest_resistance` in `prev_keys`:

```python
prev_keys = _level_key_set(curr.pair, prev.levels)
# Belt-and-braces: rehydrated prev may have empty levels (pre-
# refinement-A jsonl) while still carrying nearest_*. Treat the
# nearest_* as members of prev's catalogue so a level that's
# still the same nearest doesn't fire NEW_MAJOR_LEVEL.
for prev_nearest in (prev.nearest_support, prev.nearest_resistance):
    if prev_nearest is not None:
        prev_keys.add(
            (level_side(prev_nearest), quantise_price(curr.pair, prev_nearest.price))
        )
```

Pin in a test:

```python
def test_no_spurious_new_major_level_when_curr_nearest_matches_rehydrated_prev_nearest() -> None:
    # Simulate post-restart with pre-refinement-A jsonl: prev has
    # nearest_support but empty levels list.
    prev_nearest = make_level(level_type="SUPPORT", price=1.30000, score=0.0)
    prev = make_state(nearest_support=prev_nearest, levels=[])
    curr_nearest = make_level(level_type="SUPPORT", price=1.30000, score=7.5)
    curr = make_state(nearest_support=curr_nearest, levels=[curr_nearest])
    changes = compute_structure_diff(prev=prev, curr=curr)
    assert [c for c in changes if c.kind is ChangeKind.NEW_MAJOR_LEVEL] == []
```

Also fix `test_nearest_score_missing_defaults_to_zero` docstring — its
reasoning ("score defaults to 0 → won't fire") is wrong regardless of
whether the diff layer is patched. The actual reason a spurious fire
is avoided (once the patch lands) is the inclusion of
`prev.nearest_*` in `prev_keys`.

---

### M1 (MEDIUM — test under-specified) — `test_bar_after_bias_flip_produces_htf_bias_change_alert` uses `in` instead of `==`

**File:** `tests/unit/test_bot_loop_structure_alerts.py:178-235`

```python
subtypes = [a.event_subtype for a in structure_alerts]
assert "HTF_BIAS_CHANGE" in subtypes
```

The test asserts that HTF_BIAS_CHANGE is _among_ the dispatched events
but doesn't pin the full set. Under the H1 regression, a spurious
NEW_MAJOR_LEVEL on the same bar (seeded prev has empty `levels`)
would pass this test silently. Multi-event leakage from any future
diff-layer regression slides through the same gap.

**Suggested fix.** Pin the exact set:

```python
assert subtypes == ["HTF_BIAS_CHANGE"]  # or: set(subtypes) == {"HTF_BIAS_CHANGE"}
```

If a spurious NEW_MAJOR_LEVEL fires (it currently can under H1, depending
on what `analyze_structure` produces from flat seed candles), this would
turn the test red, which would have surfaced H1 organically. Same goes
for `test_alerter_send_exception_does_not_crash_bar_close` and
`test_structure_alert_persists_to_jsonl_audit_log` — both seed an empty
`prev.levels` and use loose assertions.

---

### M2 (MEDIUM — hydration ignores `STRUCTURE_LOG_ENABLED` toggle) — stale jsonl re-loaded on restart even when logging is off

**File:** `src/bot/loop.py:333-353`

`hydrate()` calls `load_latest_structure_state_per_pair(structure_log_path)`
regardless of whether `STRUCTURE_LOG_ENABLED` is truthy. The structure
engine's `log_structure_state` is gated on `STRUCTURE_LOG_ENABLED` (read
per-call per the recent L-2 fix), so a config of "logging enabled, then
disabled, then process restarted" produces:

- Engine writes nothing this session (logging disabled).
- Hydration reads the **previous** session's jsonl from disk.
- `_previous_structure` is populated with stale state (possibly hours
  old, possibly from a different market regime).
- First post-restart bar diffs against stale prev → potentially
  many spurious WARNING events (HTF bias / mode have likely moved
  since).

**Impact.** Operator-visible only on the specific config sequence
"enable → disable → restart". Probably never happens in production
because Phase 12 needs `STRUCTURE_LOG_ENABLED=true` to do hydration at
all. But the implicit coupling between the engine's write-gate and the
alerts layer's read-not-gate is fragile.

**Suggested fix.** Either (a) skip hydration when
`STRUCTURE_LOG_ENABLED` is false, with a one-line log explaining
cold-start; or (b) document in `structure_alerts/MODULE.md` that the
two flags must agree.

---

### M3 (MEDIUM — failure-isolation coverage gap) — no BotLoop test for outer-layer persistence catch

**File:** `src/bot/loop.py:1602-1611`

```python
for event in events:
    try:
        append_event_to_jsonl(event, STRUCTURE_ALERTS_LOG_PATH)
    except Exception:
        logger.exception(
            "structure_alerts jsonl write failed (kind=%s pair=%s)",
            event.kind.value, event.pair,
        )
```

`append_event_to_jsonl` swallows `OSError` internally. The outer
`Exception` catch here is for the residual cases (a `TypeError` on
JSON encoding that slipped past `default=str`, a `ValueError` on
malformed datetime, etc.). There's a unit-level test that the inner
function survives `OSError` (`test_persistence.py:140-178`) but no
BotLoop-level test that this _outer_ catch fires when the inner
function raises a non-`OSError`.

Compare with:
- `test_processor_exception_does_not_crash_bar_close` — covers the
  processor catch (`loop.py:1573-1577`).
- `test_alerter_send_exception_does_not_crash_bar_close` — covers
  the alerter catch (`loop.py:1635-1641`).

Symmetric coverage would be a one-test addition that monkeypatches
`bot.loop.append_event_to_jsonl` to raise `TypeError`, fires a bar that
produces an event, and asserts `"structure_alerts jsonl write failed"`
in the log + no propagation. Low effort, completes the failure-isolation
test trio.

---

### M4 (MEDIUM — module-level audit log path captured at import) — `STRUCTURE_ALERTS_LOG_PATH` env override not honoured post-startup

**Files:**
- `src/structure_alerts/constants.py:70-72`
- `src/bot/loop.py:94, 1598, 1641`

```python
STRUCTURE_ALERTS_LOG_PATH: str = os.getenv(
    "STRUCTURE_ALERTS_LOG_PATH", "data/alerts/structure_alerts.jsonl"
)
```

Read once at import. `bot/loop.py` imports the name (line 94) and uses
it as the path argument at lines 1598 and 1641. The recent L-2 review
fix to `structure_engine/logging.py` switched to per-call env reads
(`_log_path()`) so an operator can override the path without restarting;
this module didn't follow the same pattern.

Practical impact is low (operators rarely override paths mid-process)
but inconsistent with the recently-introduced pattern. A follow-up
that wraps the read in a function (mirroring `_log_path()` in
`structure_engine/logging.py`) keeps the two layers in sync.

---

### M5 (MEDIUM — clock called multiple times in one bar) — `_dispatch_structure_alerts` invokes `self._clock()` three times per bar

**File:** `src/bot/loop.py:1575, 1622, 1629`

```python
events = process_structure_alerts(prev=..., curr=..., dedupe=..., now=self._clock())
...
summary = build_hourly_summary(structure_state, now=self._clock())
...
if not self._structure_dedupe.should_fire(
    summary.dedupe_key, summary.severity, now=self._clock(),
):
```

Each call generates a fresh datetime. The summary's dedupe-check `now`
is a few μs ahead of the summary's `timestamp`. Inconsistent timestamps
within one bar's pipeline.

Visible impact in production: zero (the dedupe key for HOURLY_SUMMARY
is `_HOURLY_SUMMARY_{hour_bucket}`, hour-bucketed from
`state.timestamp`, so the clock drift doesn't matter for the dedupe
outcome). The translator's `Alert.timestamp` falls back to
`event.timestamp` (per `_dispatch_structure_event`'s deliberate choice
of "no clock override"), so the operator-visible timeline reads
consistently. But the locked decision was to use one shared `now` per
bar; the implementation doesn't.

**Suggested fix.** Capture `now = self._clock()` once at the top of
`_dispatch_structure_alerts` and thread it everywhere:

```python
def _dispatch_structure_alerts(self, pair, candle, structure_state):
    now = self._clock()
    try:
        events = process_structure_alerts(
            prev=..., curr=structure_state, dedupe=..., now=now,
        )
    ...
    if candle.close_time.minute == 0:
        self._dispatch_hourly_summary(pair, structure_state, now=now)
    ...
```

---

### M6 (MEDIUM — DedupeCache grows unbounded for level-anchored keys) — no eviction

**File:** `src/structure_alerts/dedupe.py:86`

```python
self._last_fired: dict[str, datetime] = {}
```

Keys accumulate monotonically. The cardinality of
`{pair}_NEW_LEVEL_{side}_{Q(price)}` is bounded by the set of
quantised-pip prices the market visits — for GBPUSD over a
multi-month run, that's plausibly thousands. Similarly
`LEVEL_INVALIDATED`, `SWEEP_RECLAIM`, `FAILED_RECLAIM` per side per
quantised price.

For four pairs over a six-month run, the upper bound is in the
low-tens-of-thousands of entries — small in memory terms (dict of
str→datetime, ~150 bytes/entry, ~1.5MB worst case). Not a leak in any
practical sense; flagged for completeness.

**Suggested fix.** Either (a) document the unbounded growth as
acceptable in MODULE.md, or (b) add a periodic eviction (drop entries
older than `max(COOLDOWN_BY_SEVERITY.values()) * 2` = 4h) on each
`should_fire` call. (a) is fine.

The audit jsonl shares the same concern — append-only, no rotation.
Phase 11's structure-engine jsonl has the identical property; if
either should rotate, both should, and the convention belongs in the
data-engineering layer rather than per-feature.

---

### L1 (LOW — HTF_BIAS_CHANGE dedupe key doesn't include `prev_bias`) — flip-flop transitions to the same target bias are silently suppressed

**File:** `src/structure_alerts/triggers.py:139`

```python
dedupe_key=f"{pair}_HTF_BIAS_{curr_v}"
```

If bias goes BULLISH → BEARISH (fires with key `..._BEARISH`), then
BEARISH → NEUTRAL (fires with key `..._NEUTRAL`), then NEUTRAL →
BEARISH within an hour, the second BEARISH-targeted transition is
blocked by the WARNING 1h cooldown on `..._BEARISH`. Operator
misses the second transition.

This matches the locked dedupe-key spec table in
`structure_alerts/MODULE.md:103` and is consistent with how
`STRUCTURE_MODE_CHANGE` keys work — so it's intentional. Flagged as
LOW because the locked spec accepts it, but worth a one-line note in
the heartbeat-semantics doc that operators see at-most-one bias-target
alert per hour, not at-most-one bias-transition.

---

### L2 (LOW — `level_side` silently fallthrough) — unknown level types misclassify as RESISTANCE

**File:** `src/structure_alerts/diff.py:148-151`

```python
def level_side(level: StructureLevel) -> str:
    if level.level_type in ("SUPPORT", "LIQUIDITY_LOW"):
        return "SUPPORT"
    return "RESISTANCE"
```

Engine `LevelType` is `Literal["SUPPORT", "RESISTANCE", "LIQUIDITY_HIGH", "LIQUIDITY_LOW"]`,
so the four-way switch is exhaustive today. A future engine addition
(e.g., "BREAKAWAY_HIGH") would silently land on the RESISTANCE branch.
Hydration's `_VALID_LEVEL_TYPES` does fail-fast on unknown types
(line 208), so the asymmetry is "engine: yes / hydration: yes /
trigger-layer: no".

**Suggested fix.** Mirror hydration's explicit handling:

```python
if level.level_type in ("SUPPORT", "LIQUIDITY_LOW"):
    return "SUPPORT"
if level.level_type in ("RESISTANCE", "LIQUIDITY_HIGH"):
    return "RESISTANCE"
raise ValueError(f"Unknown level_type: {level.level_type!r}")
```

---

### L3 (LOW — reaction triggers don't handle NaN score) — `f"{score:.1f}"` renders "nan" in operator-visible body

**File:** `src/structure_alerts/triggers.py:181, 188`

```python
full_text = (
    f"Support broken at {formatted} "
    f"({level.timeframe}, score was {level.score:.1f})"
)
```

If `level.score` is NaN, the body reads "Support broken at 1.33400
(H1, score was nan)". The summary builder is defensive about this
(`_format_level_token` omits the `(s=X.X)` token when NaN); triggers
isn't. Symmetric defensiveness would match.

Engine doesn't produce NaN scores in normal operation, so this is
LOW. Could mirror `summary._format_level_token`'s NaN check.

---

### L4 (LOW — `_hour_bucket` empty-timestamp collapses dedupe keys) — degenerate state.timestamp produces shared key across hours

**File:** `src/structure_alerts/summary.py:207-208`

```python
if not timestamp_iso:
    return timestamp_iso
```

Empty `state.timestamp` falls through verbatim. Dedupe key becomes
`{pair}_HOURLY_SUMMARY_` (trailing underscore, no hour). Two
consecutive top-of-hour bars with empty timestamps would share this
key — the second blocked by the INFO 2h cooldown.

Engine writes a timestamp on every state. The defensive fallback
case is for pathological records; if it ever fires, the operator
sees one summary per 2h instead of one per hour. LOW.

---

### L5 (LOW — multi-pair STRUCTURE coalesce key concern) — note for MODULE.md completeness

**File:** `src/structure_alerts/MODULE.md:153-155` (informational)

The Phase 9 coalescer key is `(category, event_subtype, pair, severity)`.
At top-of-hour all four pairs produce a HOURLY_SUMMARY simultaneously;
each pair has a distinct key, so they don't coalesce into one bullet
list — operator gets 4 separate INFO messages within the 30s window
(each a 4-line body). This is the documented behaviour and is
intentional, but a one-line "expect N messages per top-of-hour for N
pairs" anchor in the heartbeat section would prevent operator
confusion the first time it happens.

---

### L6 (LOW — `_RecordingAlerter` doesn't exercise the Phase 9 coalescer) — integration tests bypass the CRITICAL-bypass path

**File:** `tests/unit/test_bot_loop_structure_alerts.py:115`, via
`tests/unit/test_bot_loop.py:979-1027`

The recording alerter captures `send(alert)` directly without going
through `AlertCoalescer.add`. So the CRITICAL-bypass behaviour of
SUPPORT_ACCEPTANCE_BREAK / RESISTANCE_ACCEPTANCE_BREAK is not
exercised end-to-end in the C-6 suite. That path is well-covered in
`tests/unit/test_alerts_coalescer.py` and is catalogue-agnostic
(the coalescer treats STRUCTURE no differently from any other
category), so the gap is intentional. Flagged for completeness only.

---

### L7 (LOW — informational) — bar-close gate uses `candle.close_time.minute == 0`

**File:** `src/bot/loop.py:1614`

```python
if candle.close_time.minute == 0:
    self._dispatch_hourly_summary(pair, structure_state)
```

`candle.close_time` is the bar's close timestamp (per `feed/types.py:65`).
At 14:00:00 UTC, the bar covering 13:55:00–14:00:00 has
`close_time.minute == 0`, so the summary fires once at top-of-hour.
Correct.

What's not tested explicitly: a bar at 14:00:00.500 (microsecond skew)
— `.minute` is still 0 because Python datetimes truncate sub-second
on the minute attribute. Fine.

What's tested implicitly only: the gap-fill case where a feed gap
straddles 14:00 and no 14:00 bar is emitted. Per the heartbeat-semantics
doc in MODULE.md, that hour's summary is correctly missed and is
expected to read as "missing summary alone is not a definitive
outage". Locked behaviour, no fix needed.

---

## Catalogue-extension audit (Phase 9 surface)

`src/alerts/types.py` changes:
- ✓ `STRUCTURE` added to `AlertCategory` enum (line 81).
- ✓ Nine new entries appended to `EVENT_SUBTYPES` (lines 109-118), in
  producer-order (spec §7 A–H then §11), with a documentation comment
  pointing reviewers at `severity_for`.
- ✓ `test_event_subtypes_includes_locked_set` extended to the
  full 25-entry set; `test_category_enum_values` extended to four
  categories.
- ✓ Coalescer / alerter / formatter not touched (catalogue-agnostic,
  as required).

## Phase 11 payload-extension audit (refinement A)

`src/structure_engine/logging.py` changes:
- ✓ `_to_payload` adds `nearest_support_score`, `nearest_resistance_score`,
  and compact `levels` list `{p, s, sc, tf}` per entry.
- ✓ Sort by score descending; cap at `STRUCTURE_LEVELS_MAX_PER_RECORD` (default 30).
- ✓ `_level_price` / `_level_score` handle `None`.
- ✓ Test `test_payload_compact_levels_capped_at_max_per_record` pins the
  50→30 cap with highest-scores-retained semantics.
- ✓ No caller of `log_structure_state` requires changes — payload is
  additive.

## Locked-decision compliance matrix

| # | Decision | Implementation | Status |
|---|---|---|---|
| 1 | Email dispatcher deferred to Phase 13 | No new dispatcher introduced — only Phase 9 TelegramAlerter is used | ✓ |
| 2 | Hourly summary scheduler in-process in BotLoop | `_handle_bar_close` checks `candle.close_time.minute == 0`, no systemd timer | ✓ |
| 3 | Diff state: in-memory + startup hydration | `_previous_structure` dict in BotLoop, hydrated via `hydrate()` | ✓ |
| 4 | Reuses Phase 9 TelegramAlerter / coalescer / Alert | `translate_to_phase9_alert` returns Phase 9 Alert, fed to existing alerter | ✓ |
| 5 | New AlertCategory STRUCTURE + 9 event subtypes | Added in `alerts/types.py` | ✓ |
| 6 | Diff emits no events when `prev=None` or `curr.is_valid=False` | `diff.py:217-220`, also pinned at processor level | ✓ |
| 7 | Mode transitions INTO UNKNOWN suppressed; OUT OF UNKNOWN fires | `diff.py:238`, tested both ways | ✓ |
| 8 | Reactions fire on every bar (no prev comparison); dedupe handles | `diff.py:247-260` doesn't read `prev.current_reaction` | ✓ |
| 9 | NEW_MAJOR_LEVEL scoped to `curr.nearest_*` only | Loop iterates `(curr.nearest_support, curr.nearest_resistance)` | ✓ but see **H1** for the "is-new" comparison flaw |
| 10 | LEVEL_INVALIDATED against `curr.levels`, not `curr.nearest_*` | `diff.py:282`, tested with promoted-but-still-in-levels case | ✓ |
| 11 | Side-aware dedupe keys | `_level_key_set` uses `(side, Q)` tuple; tests cover same-quantised-different-side | ✓ |
| 12 | Reaction dedupe key includes reaction_type AND quantised price | Trigger layer produces `..._SWEEP_RECLAIM_SUPPORT_<Q>` / `..._FAILED_RECLAIM_SUPPORT_<Q>` etc. | ✓ |
| 13 | Refinement A jsonl extension | Compact levels list `{p, s, sc, tf}`, capped 30 by score desc | ✓ |
| 14 | Quantise via `int(round(price / pip_size_for(pair)))` | `quantise_price` in constants.py | ✓ |
| 15 | Hour bucket from `state.timestamp`, NOT now | `summary.py:75`, `_hour_bucket(state.timestamp)` | ✓ |
| 16 | NaN confidence → None in debug; NaN score → omit `(s=X.X)` token | Both handled in `summary.py`; not handled in triggers' reaction body (see **L3**) | partial |
| 17 | `translate_to_phase9_alert` fresh debug dict | `{**event.debug, "dedupe_key": ...}` — new dict, doesn't mutate source | ✓ |
| 18 | Dispatch BEFORE persist | Two separate loops in `_dispatch_structure_alerts`, dispatch first | ✓ |
| 19 | `_previous_structure[pair]` updated AFTER dispatch (even on processor crash) | Final line of `_dispatch_structure_alerts`, after all try/except guards | ✓ |
| 20 | Hourly summary runs through dedupe | `_dispatch_hourly_summary` calls `should_fire` before send | ✓ |
| 21 | Heartbeat framing in MODULE.md: multi-stream | `MODULE.md:116-139` covers this | ✓ |

---

## Recommendation

**APPROVE WITH CONDITIONS.**

The branch is structurally sound, all 21 locked decisions are honoured
or near-honoured, and the test suite (1146) passes cleanly. The
review found no CRITICAL or HIGH issues that block merge — H1 is HIGH
on the false-invariant axis but its operational impact is one bounded
burst of INFO alerts on the first post-upgrade restart only.

Two conditions for merge:

1. **(blocking H1)** Patch `compute_structure_diff` to include
   `prev.nearest_support` / `prev.nearest_resistance` in `prev_keys`
   for the NEW_MAJOR_LEVEL check (the 4-line fix sketched in H1). Add
   the regression test from H1's "Suggested fix" section. Update or
   delete the misleading docstring on
   `test_nearest_score_missing_defaults_to_zero` to remove the
   "score defaults to 0 → no fire" reasoning, which doesn't hold.

2. **(non-blocking, ship as a follow-up)** Tighten M1's three
   integration assertions (`in` → `==`) so a future spurious-event
   regression surfaces. This is mechanical and small.

M2–M6 and L1–L7 are reviewer notes — none are merge-blockers. The
unbounded-growth concerns (M6) and the env-toggle / per-call-read
inconsistencies (M2, M4) could land as a single small follow-up
later this phase or be deferred to a Phase 12.x polish commit.

Phase 12 ships an observability layer; the post-merge invariant the
operator most cares about is "alerts I receive are real, accurate, and
not spammy". With H1's patch landed, that invariant holds.
