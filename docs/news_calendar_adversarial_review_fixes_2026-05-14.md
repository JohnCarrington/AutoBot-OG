# Adversarial follow-up review — fixes for `feature/news-calendar-finnhub`

**Reviewer:** Independent Claude Code session (read-only)
**Date:** 2026-05-14
**Scope:** commit `3a24292` ("fix(news-calendar): adversarial review fixes (H1, H2, H3, H4, H5, H6, H7)") on `feature/news-calendar-finnhub`.
**Method:** `git diff f714ec1..3a24292`, code re-read of all five source files and three test files, eight targeted behavioural probes, full test suite (205 passed, 0 warnings).

---

## TL;DR

All seven flagged HIGH issues (H1-H7) are addressed at root cause. Regression tests are well-scoped and would fail if any of the fixes were reverted. 205/205 tests pass. The H2 fix is particularly notable — the author correctly identified that my suggested literal symmetric set-of-tuples patch would NOT have caught the probe (because the reverse-direction request title doesn't contain "payrolls") and went with a smarter classifier-based approach.

One new MEDIUM finding (N1 — brittle event-time parser) and a small set of LOW finishing items. None blocks merge.

**Recommendation: APPROVE FOR MERGE.**

---

## Verdict per original issue

### H1 — Finnhub API key leaks in error logs → **RESOLVED** ✓

- **Fix:** `finnhub_client.py:91-105` — token moved from URL query parameter to `X-Finnhub-Token` header. The URL handed to `requests.get` no longer contains the secret.
- **Root-cause addressed:** yes. The leak mechanism was `requests`'s exception strings including the URL verbatim. With the token in a header, the URL is benign.
- **Verified (real ConnectionError, not mocked):**
  ```
  log contents: "[NEWS-CAL] Finnhub fetch failed: HTTPSConnectionPool(...
                 Max retries exceeded with url: /api/v1/calendar/economic?from=...&to=...
                 (Caused by NameResolutionError(...))"
  SENTINEL_TOKEN_PROBE_xyz present in log? False
  ```
- **Tests pinning the fix:**
  - `test_h1_token_sent_via_header_not_query_string` — direct wire-format assertion: token in header, NOT in URL.
  - `test_h1_token_does_not_leak_in_exception_log` — `caplog`-based assertion that even when the exception text mentions a URL, the sentinel token is absent.
  - `test_h1_token_does_not_leak_when_request_object_str_includes_it` — additional SSL-error variant.
- **Quality:** all three tests would fail on regression (e.g. if someone reverted the header-based auth and put the token back in the URL).

### H2 — Block-pair is bidirectional → **RESOLVED** ✓ (with extra credit)

- **Fix:** `matcher.py:127-153` — replaced `_BLOCK_PAIRS = {("adp", "non farm payrolls"), ...}` with a `_classify_adp_or_nfp` function that maps each title to `{"adp", "nfp", None}` and blocks when the request and candidate classify into *different* non-None groups.
- **Why the literal review fix was wrong:** my original suggestion was to add `("non farm payrolls", "adp")` to the tuple set. The author correctly noticed this would not catch the probe — the reverse-direction request title is `"Non-Farm Employment Change"`, which contains no `"payrolls"` substring. The classifier approach catches all four NFP spellings (`"Non-Farm Payrolls"`, `"Non Farm Payrolls"`, `"Nonfarm Payrolls"`, `"Nonfarm Employment Change"`) without enumerating cross-products.
- **Verified:**
  ```
  match_event("Non-Farm Employment Change", "USD", [adp_event]) → None ✓
  _classify_adp_or_nfp("non-farm employment change") → "nfp"
  _classify_adp_or_nfp("adp employment change") → "adp"
  ```
- **Subtle correctness point — ambiguous ADP title:** `"ADP Non-Farm Employment Change"` (the actual ForexFactory title) contains both vocabularies. The classifier prefers `"adp"` (more specific token) so an ADP request still matches an ADP candidate. Test `test_block_pair_classifier_handles_ambiguous_adp_title` pins this. ✓
- **Tests pinning the fix:**
  - `test_block_pair_nfp_request_does_not_match_adp_candidate` — the exact probe from my review.
  - `test_block_pair_alt_nfp_spellings_also_blocked` — parameterised over four spellings.
  - `test_block_pair_classifier_handles_ambiguous_adp_title` — directly tests the classifier function for the ambiguous case.

### H3 — Same-currency cross-country tiebreaker is deterministic → **RESOLVED** ✓

- **Fix:** `matcher.py:227-256` — `match_event` now collects all positive-scoring candidates and sorts by a four-key tuple:
  1. `-score` (higher score first)
  2. `0 if is_original else 1` (original-title-match wins over alias-match)
  3. `-_IMPACT_RANK[parse_impact(...)]` (HIGH > MEDIUM > LOW)
  4. `country` alphabetical ascending (deterministic last-resort)
  Python's stable sort means payload order is the final implicit tiebreaker — same payload → same answer.
- **Verified:**
  ```
  Order [DE, FR] → DE (51.2)
  Order [FR, DE] → DE (51.2)
  ```
- **Tests pinning the fix:**
  - `test_eur_de_vs_fr_tiebreaker_is_deterministic_either_payload_order` — the exact probe.
  - `test_high_impact_beats_medium_impact_on_score_tie` — pins the impact dimension.
  - `test_original_title_beats_alias_on_score_tie` — pins the alias-vs-original dimension.
- **Minor design note:** my original review suggested "prefer the EU composite". The author went with alphabetical (`"DE" < "EU"` so DE wins over EU). Defensible — the EU composite is not always present, and a deterministic rule beats one with conditional behaviour. Acceptable.
- **Bonus:** the old `test_multiple_same_country_same_title_resolves_deterministically` (L1 from the prior review — name claimed determinism, assertion was `in (53.6, 54.0)`) was renamed to `test_multiple_same_country_same_title_is_deterministic_per_payload` and now actually pins a single deterministic answer. ✓

### H4 — Single-failure cache wipe → **RESOLVED** ✓

- **Fix (multi-part):**
  - `finnhub_client.fetch_calendar` now returns `Optional[list[dict]]`: `None` on failure, `list[dict]` (possibly empty) on success. The empty-list case is "fetch worked, no in-window events" and is distinct from failure.
  - `calendar.poll_for_actual` distinguishes: on `None`, log + return (cache preserved); on `list`, replace cache and advance `last_successful_fetch`.
  - Cache shape extended: `last_fetch_attempt` (advances on every attempt) vs `last_successful_fetch` (advances only on success). New `cache_staleness_seconds()` helper.
  - New `CACHE_STALENESS_THRESHOLD_SECS` (default 300s = 5 min): `is_blackout` fails closed beyond this.
- **Root-cause addressed:** yes — the conflation of "fetch failed" with "fetch returned empty" is gone. Both the storage shape and the call-site logic distinguish them.
- **Verified:**
  ```
  Before poll: cache has 1 events
  After failed poll: cache has 1 events (preserved!)
  [NEWS-CAL] Finnhub fetch failed — preserving 1 cached events
  ```
- **Tests pinning the fix:**
  - `test_h4_finnhub_failure_preserves_cache` — the exact probe.
  - `test_h4_finnhub_success_replaces_cache` — pins the inverse (legitimate empty success DOES clear cache).
  - `test_h4_failure_does_not_advance_last_successful_fetch` — pins the staleness clock.
  - `test_h4_is_blackout_stale_cache_fails_closed` — pins the downstream fail-closed contract.
  - `test_h4_is_blackout_never_fetched_fails_closed` — cold-start edge.
  - Plus six tests in `test_news_calendar_finnhub_client.py` covering each `None`/`[]` distinction (connection error, HTTP non-200, JSON decode, disabled, empty success, non-empty success).
- **Quality:** comprehensive. This is the most thoroughly-pinned fix of the seven.

### H5 — `beat_miss` semantic inconsistency → **RESOLVED** ✓

- **Fix:** `impact.py:62-83` extracts two helpers — `classify_beat_miss(deviation)` (threshold-gated BEAT/MISS/IN_LINE) and `classify_direction(deviation)` (CONTINUATION/REVERSAL). Both branches in `calendar.get_actual_for_event` (Finnhub-surprise and locally-computed) now call these helpers.
- **Root-cause addressed:** yes. The semantic divergence between coarse (Finnhub-supplied) and threshold-gated (computed) classification is collapsed into a single function.
- **Verified:**
  ```
  Finnhub branch (surprise=0.02): beat_miss=IN_LINE
  Computed branch (no surprise):  beat_miss=IN_LINE
  ```
  Before the fix the same input produced `BEAT` and `IN_LINE` respectively.
- **Tests pinning the fix:**
  - `test_h5_finnhub_branch_classification` — parameterised over 5 deviations on the Finnhub path.
  - `test_h5_computed_branch_classification` — same 4 deviations on the computed path.
  - `test_h5_same_input_same_output_across_branches` — the exact probe from my review.

### H6 — Finnhub `surprise` field unit validation → **RESOLVED** ✓

- **Fix:** `calendar.py:201-218` — if `abs(finnhub_surprise) > 1.0` the value is rejected (with a warning log) and the code falls back to the locally-computed deviation. Fractional surprises in `[-1.0, 1.0]` are trusted.
- **Root-cause addressed:** yes for the absolute-units footgun. Doesn't pin Finnhub's actual semantics (which the docs don't pin either), but rejects values that are *almost certainly* the wrong unit and degrades safely.
- **Verified:**
  ```
  surprise=5.0  → source=computed, deviation=0.05 (correct local computation)
  surprise=1.0  → source=finnhub (boundary trusted)
  surprise=1.0001 → source=computed (just over → rejected)
  ```
- **Tests pinning the fix:**
  - `test_h6_finnhub_surprise_above_one_falls_back_to_computed`
  - `test_h6_finnhub_surprise_below_minus_one_falls_back`
  - `test_h6_finnhub_surprise_within_bounds_is_trusted`
- **Minor design note:** a legitimate 100 %+ fractional surprise (rare: emerging-market hyperinflation prints) would also be rejected. The fallback uses local deviation, which is still correct. Acceptable.

### H7 — `is_blackout` public API → **RESOLVED** ✓

- **Fix:** `calendar.py:281-393` — new `BlackoutResult` dataclass plus `is_blackout(currency, query_time, *, lookback_min=15, lookahead_min=15)` function. Public on `risk.news_calendar` namespace.
- **Semantics verified:**
  - HIGH event in default ±15-min window → `is_blocked=True, confidence="high"`. ✓
  - MEDIUM event in window → `is_blocked=True, confidence="medium"` (caller decides hard vs soft). ✓
  - Unknown currency → fail-closed `is_blocked=True, reason="unknown-currency"`. ✓
  - Stale cache → fail-closed `is_blocked=True, reason="cache-stale"`. ✓
  - HIGH wins over MEDIUM in same window regardless of order. ✓
  - Wrong-country events ignored. ✓
  - Naive `query_time` interpreted as UTC. ✓
  - `tzinfo`-aware non-UTC `query_time` compared correctly (BST → UTC). ✓
  - Window edges: `T0 + lookahead_min` exactly is **inclusive** (blocks); 1 second beyond is **exclusive** (clear). ✓
  - Malformed `time` field → event silently skipped (does not crash). ✓
  - Pre-release events (`actual=None`) still trigger blackout when in window. ✓ (verified)
- **Tests pinning the fix:** ten dedicated `test_h7_*` tests cover each of the above paths.

---

## New findings introduced by the fixes

### N1 — Brittle Finnhub event-time parser → **MEDIUM**
- **Location:** `calendar.py:296-307` (`_parse_event_time`).
- **Behaviour:** `datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")` only. Any other format → `None` → event silently skipped → blackout missed.
- **Verified (probe):**
  ```
  "2026-05-14 12:00:00":          parses to 2026-05-14 12:00:00+00:00  ✓
  "2026-05-14T12:00:00"  (ISO T): None  → skip
  "2026-05-14T12:00:00Z" (ISO Z): None  → skip
  1747224000  (Unix epoch):       None  → skip
  ```
- **Why MEDIUM:** Finnhub's current `/calendar/economic` response *does* use `"YYYY-MM-DD HH:MM:SS"`, so the parser works today. But: (a) the test fixtures use that format, so the tests don't exercise drift; (b) Finnhub has occasionally drifted to ISO 8601 in other endpoints; (c) the failure mode is *exactly* the same shape as the 2026-04-23 incident — a HIGH-impact event silently dropped from the result, with no logged warning. This module's whole reason for existing is to avoid that shape of bug.
- **Suggested fix:** broaden `_parse_event_time` to try multiple formats and log a WARNING (not skip silently) when none match:
  ```python
  _ACCEPTED_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ")
  for fmt in _ACCEPTED_FORMATS:
      try:
          return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
      except ValueError:
          continue
  if isinstance(time_str, (int, float)):
      try:
          return datetime.fromtimestamp(float(time_str), tz=timezone.utc)
      except (OSError, OverflowError, ValueError):
          pass
  logger.warning("[NEWS-CAL] unparseable event time %r — event excluded from blackout", time_str)
  return None
  ```
- **Severity rationale:** I'm rating MEDIUM (not HIGH) because Finnhub's current format matches and the failure is silent-skip rather than wrong-match. But it's the closest thing to a regression-risk in this fix wave.

### N2 — `is_blackout` event_summary is order-dependent for same-impact events → **LOW**
- **Location:** `calendar.py:357-377` (loop body picks first encountered, only upgrades on impact promotion).
- **Behaviour verified:** two HIGH events in window → `event_summary` reports whichever appeared first in the payload.
- **Impact:** the `is_blocked` decision is unaffected. Only the diagnostic string varies.
- **Suggested fix:** sort events by impact descending then time ascending before the scan, or apply the same H3-style stable-tiebreaker. Trivial.

### N3 — `BlackoutResult.reason` and `.confidence` are stringly-typed with inconsistent formatting → **LOW**
- **Location:** `calendar.py:281-298` (dataclass) and `347-393` (callsites).
- **Issue:** downstream callers will branch on these strings; they're effectively part of the API contract:
  ```
  reason: "unknown-currency", "cache-stale", "no event in window",
          "high-impact event in window", "medium-impact event in window"
  confidence: "high", "medium", "low"
  ```
  Hyphen-separated for some, space-separated for others, formatted in two different styles. No test pins them as a contract — a refactor could change them silently.
- **Suggested fix:** define `Reason` and `Confidence` (or reuse `Impact`) enums, return those. At minimum, add a test that asserts the exact reason strings for each branch so a downstream consumer can rely on them.

### N4 — Concurrent `poll_for_actual` calls all fetch when `min_interval=0` → **LOW (test artifact)**
- **Location:** `calendar.py:97-111`.
- **Verified (probe):** five concurrent calls with `min_interval=0` → fetch invoked 5 times.
- **Impact:** **production-safe.** With the default `min_interval=10`, the first thread to grab the lock updates `last_fetch_attempt`, and subsequent threads see `now - last_fetch_attempt = ε < 10` → early return. The only case where five fetches actually happen is `min_interval=0`, which is a test-only setting.
- **Suggested fix:** an `_in_flight` flag could eliminate the race for `min_interval=0` too, but it's not worth the added complexity for a test-only edge.

---

## Carry-forwards from the prior review still unaddressed

| ID | Severity | Status |
|---|---|---|
| **M1** — fuzzy matcher blind to m/m vs q/q | MEDIUM | **STILL OPEN — verified** (`Core CPI m/m` still matches `Core CPI` candidate with score 3.0) |
| **M2** — `get_actual_for_event` result missing `time` field | MEDIUM | **STILL OPEN — verified** (result keys: `actual, actual_str, beat_miss, deviation, direction_hint, forecast, forecast_str, previous, source, surprise_source, te_event` — no `time`). Partially mitigated by `is_blackout` being a separate API. |
| **M3** — UTC date rollover loses late-day events | MEDIUM | **STILL OPEN** (fetcher still uses `today` and `today+1` only) |
| **L3** — implementation-detail tests | LOW | STILL OPEN |
| **L4** — `_DEFAULT_TRADED_COUNTRIES` duplicates EUR knowledge | LOW | STILL OPEN |
| **L5** — `float()` on `actual`/`estimate` raises on non-numeric | LOW | STILL OPEN |
| **L6** — `previous` not converted to float | LOW | STILL OPEN |
| **L7** — env-var parsing raises at import | LOW | STILL OPEN |
| **L8** — minor `poll_for_actual` race | LOW | Improved by H4 fix (lock-internal `last_fetch_attempt` update) but see N4 |

None of these is a merge blocker. M1-M3 should land before Phase 4 ships if possible.

---

## Probe summary (eight scenarios, all green)

| # | Scenario | Result |
|---|---|---|
| 1 | Real `ConnectionError` from non-existent host with token set | Token absent from log ✓ |
| 2 | NFP request, ADP-only events | Returns `None` ✓ |
| 3 | DE/FR PMI both payload orderings | Returns DE both times ✓ |
| 4 | Pre-populate cache, force `fetch_calendar() → None` | Cache intact ✓ |
| 5 | 2 % surprise via Finnhub-supplied and computed branches | Both `IN_LINE` ✓ |
| 6 | Finnhub `surprise=5.0` | Falls back to computed, `deviation=0.05` ✓ |
| 7 | HIGH event 5 min after T0; stale cache | Blocked / blocked ✓ |
| 8 | Pre-release event (`actual=None`) in window | Blocks correctly ✓ |

Plus an extra probe pair surfacing N1 (ISO 8601 / Unix timestamps silently skipped).

---

## Final recommendation: **APPROVE FOR MERGE**

All seven HIGH issues from the prior review are addressed at root-cause level. The H2 fix in particular shows good judgement — the author noticed my suggested patch wouldn't have caught the probe and went deeper, refactoring `_BLOCK_PAIRS` into a per-side classifier. The regression-test surface for H1 (token in header + caplog hygiene), H4 (six finnhub-client tests + four cache-state tests), and H7 (ten dedicated tests covering windowing, fail-closed, impact precedence, and tz-aware inputs) is exemplary.

205/205 tests pass with no warnings. No source code in the fix wave introduces a CRITICAL or HIGH regression.

**Suggested follow-up backlog (not merge-blocking):**

1. **N1 — broaden `_parse_event_time` to handle ISO 8601 and Unix timestamps; warn on unparseable.** This is the only new finding from this re-review and is in a defensible spot (current Finnhub format matches), but its failure mode is exactly the silent-skip pattern this module exists to prevent.

2. **N3 — replace `BlackoutResult.reason` and `.confidence` strings with enums** (or at minimum pin the exact strings in a contract test) before Phase 4 wires consumers to them.

3. **M1-M3 carry-forwards** before Phase 4 ships.

Ship it.
