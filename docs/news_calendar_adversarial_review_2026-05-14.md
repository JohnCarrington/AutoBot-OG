# Adversarial review — `feature/news-calendar-finnhub`

**Reviewer:** Independent Claude Code session (read-only)
**Date:** 2026-05-14
**Scope:** commit `f714ec1` ("feat(news-calendar): Finnhub-backed economic news calendar ported from te_calendar.py with country-filter regression fix") against `develop` (`00dca3b`).
**Files reviewed:** `src/risk/news_calendar/{__init__,finnhub_client,impact,matcher,calendar}.py`, `tests/unit/test_news_calendar_{matcher,impact}.py`.
**Method:** code re-read, five behavioural probes, full test suite (62 passed in 0.18s).

---

## Summary

The headline regression fix (country filter BEFORE fuzzy scoring) is **correctly implemented** at `matcher.py:186`, **single-pathed** (there is no legacy TE fallback that could bypass it), and **tightly pinned** by `test_regression_gb_preflight_does_not_match_de_release` — that test would fail loudly if the fix were reverted. Verified by tracing the call graph from `calendar.get_actual_for_event` → `matcher.match_event`: the country filter is the first thing the inner loop does on each candidate, no alternative match path exists.

That said, the module has seven HIGH-severity issues and five MEDIUM-severity issues that should be addressed before Phase 4 builds blackout logic on top of this. Most are not in the *country filter* itself — they are in the surrounding fetch / cache / classification surface, several of which were carried forward from the legacy code without re-evaluation against the v1 spec.

| Severity | Count |
| --- | ---: |
| CRITICAL | 0 |
| HIGH     | 7 |
| MEDIUM   | 5 |
| LOW      | 8 |
| **Total** | **20** |

**Recommendation: APPROVE WITH MINOR CONDITIONS.** Country-filter fix is sound and tested. Fix issues H1–H4 (security + matcher correctness) before merge; H5–H7 (semantic inconsistencies + API gaps) can be follow-up if Phase 4 is more than a week away, but must land before the risk layer ships.

---

## HIGH

### H1 — Finnhub API key leaks in error logs (security)
- **Location:** `finnhub_client.py:86-88`.
- **Root cause:** the API token is appended to the URL as a query parameter (`?token={FINNHUB_API_KEY}`). On any `requests.RequestException` (timeout, DNS failure, connection reset, SSL error), the exception's `__str__` includes the full URL — `requests` exception strings always contain the URL that failed.
- **Verified (probe):**
  ```
  ConnectionError: HTTPSConnectionPool(host='nonexistent.finnhub.io.invalid', port=443):
    Max retries exceeded with url: /api/v1/calendar/economic?from=...&token=dummy-key-for-test
    (Caused by ...)
  ```
- **Impact:** any network blip writes the production API key into application logs (and, depending on log forwarding, into Splunk / Datadog / Cloudwatch / etc.). Keys leak to whoever has log access.
- **Suggested fix:** send the token via the `X-Finnhub-Token` header instead of a query parameter (Finnhub supports this — it is in fact their recommended method). Failing that, redact the URL before logging:
  ```python
  except Exception as e:
      logger.warning("[NEWS-CAL] Finnhub fetch failed: %s",
                     str(e).replace(FINNHUB_API_KEY, "***REDACTED***"))
  ```

### H2 — Block-pair logic is one-directional: `NFP request → ADP candidate` is not blocked
- **Location:** `matcher.py:98-111`.
- **Root cause:** `_BLOCK_PAIRS` lists only `("adp", "non farm payrolls")` and `("adp", "nonfarm payrolls")`. The check `block_word in t_lower and block_event in c_lower` requires the *request* to mention "adp" and the *candidate event* to mention "non farm payrolls" — i.e. it only protects the ADP→NFP direction. The reverse (request title = "Non-Farm Payrolls", candidate = "ADP Employment Change") is unblocked.
- **Verified (probe):**
  ```
  events = [{country: US, event: "ADP Employment Change", actual: 175000, ...}]
  match_event("Non-Farm Employment Change", currency="USD", events=events)
  → {country: US, event: "ADP Employment Change", actual: 175000, ...}
  ```
  `is_blocked_match("Non-Farm Employment Change", "ADP Employment Change")` returns `False` and `fuzzy_score` produces 2.67 (matches on `employment` and `change`, shorter side has 3 words, pct=0.67 > 0.5 threshold).
- **Impact:** scheduled-event window for NFP polls might match a stale ADP event still in the cache (ADP releases ~2 days before NFP; both linger in the today+tomorrow window). The strategy then trades NFP on ADP's actual/forecast — a wrong-release lookup with material directional consequences. Same shape of bug as the 2026-04-23 incident, just for a different signal.
- **Suggested fix:** make `_BLOCK_PAIRS` symmetric, or invert the check to also catch the reverse direction:
  ```python
  _BLOCK_PAIRS: set[tuple[str, str]] = {
      ("adp", "non farm payrolls"),
      ("adp", "nonfarm payrolls"),
      ("non farm payrolls", "adp"),       # reverse — NFP request, ADP candidate
      ("nonfarm payrolls",  "adp"),
      ("non-farm payrolls", "adp"),
  }
  ```
- **Test gap:** add `test_block_pair_nfp_does_not_match_adp` — the inverse of `test_block_pair_adp_does_not_match_nfp`.

### H3 — Same-currency, different-country matches are order-dependent
- **Location:** `matcher.py:191` (`if score > best_score:` — strict greater-than, no tiebreaker).
- **Root cause:** EUR maps to `{EU, DE, FR, IT, ES, NL}`. When DE and FR both publish their flash PMI at the same instant with identical event names, they tie on `fuzzy_score`. The strict-`>` comparison locks in whichever appears first in Finnhub's response. Finnhub does not document a stable ordering on its `/calendar/economic` endpoint.
- **Verified (probe):**
  ```
  events=[DE PMI 51.2, FR PMI 48.9] → returns DE (51.2)
  events=[FR PMI 48.9, DE PMI 51.2] → returns FR (48.9)   ← different result, same input
  ```
- **Impact:** for EUR-pair strategies (EUR/USD, EUR/JPY, EUR/GBP), `beat_miss` and `actual` differ between polls depending on payload order. Two consecutive polls could return different direction hints.
- **Suggested fix:** define a deterministic tiebreaker. Reasonable orderings, in order of preference:
  1. Prefer the EU composite (`country == "EU"`) when it exists — it's the aggregate the FX market actually reacts to.
  2. Otherwise prefer the event with the largest `|actual - estimate|` (strongest signal).
  3. Otherwise sort countries alphabetically and pick first.
- **Test gap:** the existing `test_multiple_same_country_same_title_resolves_deterministically` asserts `result["actual"] in (53.6, 54.0)` — i.e. it accepts a *non-deterministic* result while claiming determinism in its name. Replace with an actual order-invariant assertion.

### H4 — Single-failure cache wipe leaves the risk layer blind for `POLL_INTERVAL` seconds
- **Location:** `calendar.py:64-67`. `events = fetch_calendar()` returns `[]` on any error; the caller unconditionally writes `_cache["events"] = events`.
- **Verified (probe):** pre-fill cache with NFP event, force a fetch failure, observe cache empty.
  ```
  After inject:        cache has 1 events
  After failed fetch:  cache has 0 events
  ```
- **Impact:** a single DNS hiccup, single 502 from Finnhub, single SSL handshake glitch wipes good data. The cache then serves empty for ≥`POLL_INTERVAL` seconds (default 10s). Any strategy that reads the calendar in that window sees "no scheduled events" — if the risk layer is wired to fail-open on empty (rather than fail-closed), this is a real blackout-bypass during transient outages. If it's wired fail-closed, the bot stops trading for 10s on every blip.

  This is a regression vs. the v1 spec's stated "fail closed". The module docstring claims "Failing closed (no Finnhub → no match) is the v1-spec-aligned behaviour", but the *implementation* fails by *wiping good cached data* on a single transient error, not by surfacing the failure to the caller.

- **Suggested fix:** distinguish "fetch returned nothing" from "fetch failed". `fetch_calendar` should signal failure (return `None` instead of `[]`, or raise a typed exception that `poll_for_actual` catches). On failure, `poll_for_actual` should **not** overwrite `_cache["events"]` — keep the last successful snapshot and only update `_cache["last_fetch_attempt"]` so the next retry waits POLL_INTERVAL.

  Additionally, return a richer status: `_cache["last_successful_fetch"]` should be stale-checked by `get_actual_for_event`. If it's > some staleness threshold (e.g. 5 minutes), the lookup should explicitly fail closed with a different reason code than "no match" so the caller can distinguish "no event scheduled" from "calendar is stale, treat everything as a blackout".
- **Test gap:** no test exercises `fetch_calendar` returning `[]` after the cache already had data. Add one.

### H5 — `beat_miss` semantics differ between Finnhub-provided and locally-computed branches
- **Location:** `calendar.py:122-150`.
- **Root cause:** the Finnhub-`surprise` branch sets `beat_miss = fh_beat_miss or ...`, where `fh_beat_miss` comes from `compute_surprise` (a coarse `actual > estimate ? "BEAT" : ...`). The fallback branch calls `compute_deviation`, which is **threshold-gated** — anything inside ±`DEVIATION_THRESHOLD` (default 5 %) is classified `IN_LINE` regardless of sign.
- **Verified (probe):** same input, two different `beat_miss` labels.
  ```
  actual=102, estimate=100  (2% surprise)
  Computed branch  → beat_miss=IN_LINE,   direction_hint=REVERSAL
  Finnhub  branch  → beat_miss=BEAT,      direction_hint=REVERSAL
  ```
- **Impact:** any downstream consumer (strategy gates, journalling, slack alerts) that branches on `beat_miss == "BEAT"` will see different verdicts for the same input depending on whether the Finnhub plan is Enterprise-tier. This is exactly the kind of "subtle inconsistency surfaces under load" bug that a future incident review will trace back to here.
- **Suggested fix:** pick one definition of `beat_miss` and apply it in both branches. The threshold-gated version (current `compute_deviation`) is the more spec-aligned choice because the v1 risk layer differentiates BEAT/MISS/IN_LINE for direction-hint selection; collapsing 2 % to IN_LINE matches the documented `CONTINUATION/REVERSAL` taxonomy.

### H6 — Finnhub `surprise` field is consumed without unit validation
- **Location:** `calendar.py:124-138`.
- **Root cause:** the code reads `finnhub_surprise = best.get("surprise")` and compares its magnitude directly against `DEVIATION_THRESHOLD` (0.05 fraction). Finnhub's documentation for the Enterprise-tier `surprise` field does not pin the unit — it could be a fractional surprise (`0.05` for "5% above forecast") or an absolute-unit surprise (`5.0` for "5 above forecast in the natural units of the indicator").
- **Verified (probe):** with `surprise = 5.0` (absolute units), the code reports `direction_hint=CONTINUATION` and the diagnostic log line emits `+500.00%` because `result["deviation"] = 5.0` is multiplied by 100 for formatting.
- **Impact:** if Finnhub's actual semantics differ from the code's implicit assumption, every release tagged with a Finnhub-provided surprise gets the wrong `direction_hint`. The fallback (computed) path is unaffected, so the bug only manifests once the user upgrades to Enterprise tier — a silent regression at exactly the moment the team adds a "premium" data source.
- **Suggested fix:** (a) pin the Finnhub `surprise` unit definitively in the docstring (a comment-only fix); (b) reject the Finnhub-provided value if `abs(finnhub_surprise) > 1.0` — almost no real-world fractional surprise exceeds 100 % — and fall back to `compute_deviation`; (c) add a test that pins both interpretations and document which one the code expects.

### H7 — Public API has no surface for "is there a blackout window for currency X near time T?"
- **Location:** `calendar.py` public API: `poll_for_actual`, `get_actual_for_event`.
- **Why this is HIGH:** the module's purpose statement (`__init__.py:13-20`) says this is on the critical path for Phase 4's risk-layer blackout logic. But the public API only answers *post-hoc* "did this scheduled event publish yet?" — it does not answer the question Phase 4 will actually ask, which is "should I block a new entry right now because a HIGH-impact event is within ±15 min?"

  Concretely missing:
  1. No `next_events(currency, within_minutes)` function returning upcoming HIGH/MEDIUM events.
  2. No `is_blackout(currency, instant)` predicate.
  3. The event `time` field from Finnhub is not preserved in the dict returned by `get_actual_for_event` — even if Phase 4 calls `get_actual_for_event`, it cannot inspect publication time.
- **Suggested fix:** before Phase 4 lands, add a separate public function:
  ```python
  def upcoming_events_for_currency(
      currency: str,
      *,
      within_minutes: int,
      min_impact: Impact = Impact.MEDIUM,
      now: Optional[datetime] = None,   # injectable for tests
  ) -> list[dict]:
      ...
  ```
  Preserve event `time` (parsed to `datetime` with UTC tzinfo) inside the returned dict.

---

## MEDIUM

### M1 — Fuzzy matcher is blind to release-frequency markers (m/m vs q/q, MoM vs QoQ)
- **Location:** `matcher.py:127-149`.
- **Verified (probe):** request `"Core CPI m/m"` matches candidate `"Core CPI"` with score 3.0 (common = {core, cpi} = 2, shorter = 2, pct = 1.0). A monthly CPI request and a quarterly CPI release in the events list cannot be distinguished if the Finnhub-side event name omits the frequency.
- **Impact:** low-probability but real. Most Finnhub event names include `MoM`/`YoY`/`QoQ` so the alias map catches them. But for any release where Finnhub returns a generic name, the matcher silently picks up the wrong frequency. Acceptable as v1 best-effort but worth documenting.
- **Suggested fix:** when the request title contains an m/m, y/y or q/q marker, require the candidate to either contain the same marker token OR map to it via `_FF_TO_FINNHUB`. Reject otherwise.

### M2 — `get_actual_for_event` does not return event publication time
- **Location:** `calendar.py:108-116` constructs the result dict; `time` field from `best` is discarded.
- **Why MEDIUM:** Phase 4 will want this for blackout windowing; without it, the risk layer either has to call `match_event` itself (bypassing the public API) or peek at the module-private `_cache`. Either is a coupling problem.
- **Suggested fix:** add `"time": best.get("time")` (or better, a parsed `datetime` with UTC tzinfo) to the returned dict.

### M3 — UTC date-rollover drops late-day events
- **Location:** `finnhub_client.py:80-85`. `from = today_utc` and `to = today_utc + 1d`. At `00:01 UTC`, today's window starts at the new day; yesterday's 23:00 UTC release (e.g. FOMC at 18:00 ET) drops out of the next fetched window.
- **Why MEDIUM:** the legacy AutoBot may have handled this; the v1 spec doesn't address it directly. Most relevant for US market events around late evening UTC.
- **Suggested fix:** fetch `yesterday..tomorrow` or `today-1..today+1`. Cache is cheap; double the window cost is negligible.

### M4 — Three core code paths are untested
- **Locations:**
  - `finnhub_client.fetch_calendar` (HTTP, JSON parse, country filter, impact filter)
  - `calendar.poll_for_actual` (rate limiting, fail-closed-on-fetch-empty, threading)
  - `calendar.get_actual_for_event` (cache copy, surprise-source branching, log formatting)
- **Why MEDIUM:** the matcher is well-tested (the high-value surface), but the integration glue is not. A regression in `poll_for_actual` could quietly disable the calendar without any test failing. The `_inject_events_for_tests` and `_reset_cache_for_tests` seams already exist — they're just not used by any test.
- **Suggested fix:** add at least one happy-path test per function using `requests_mock` (or `unittest.mock.patch`) for `fetch_calendar` and direct cache injection for the other two. The Finnhub branch vs computed branch in `get_actual_for_event` should each get a test (this also pins the H5 fix).

### M5 — `get_actual_for_event` returning `None` is ambiguous
- **Location:** `calendar.py:96-104`. `None` is returned for: (a) missing currency, (b) unknown currency, (c) no Finnhub events at all, (d) no same-country candidate, (e) candidate matched but `actual` not yet published.
- **Why MEDIUM:** Phase 4's blackout logic almost certainly wants to treat (a)-(d) and (e) differently. For (e) it should block (release pending). For (a)-(d) the right answer depends on whether the *scheduled-event watchlist* knows of this event.
- **Suggested fix:** return a small `enum` for the failure reason, or split the API into separate calls.

---

## LOW

### L1 — `test_multiple_same_country_same_title_resolves_deterministically` is misleadingly named
The assertion is `result["actual"] in (53.6, 54.0)`, which accepts *either* result — the opposite of deterministic. Either rename (e.g. `test_multiple_same_country_returns_one_of_the_candidates`) or strengthen the assertion to pin a specific result once H3 is fixed.

### L2 — Block-pair test names imply both directions are covered
`test_block_pair_adp_does_not_match_nfp` tests one direction; there is no `test_block_pair_nfp_does_not_match_adp`. Casual readers of the test names will assume bidirectional coverage. See H2.

### L3 — Two tests assert on module-private internals
`test_countries_for_currency_map_intact` and `test_eur_country_set_includes_composite` reach into `M._COUNTRIES_FOR_CURRENCY` directly. Defensible as guard tests but couple the test suite to a private structure. Could be reformulated as behavioural checks (e.g. "EUR matches EU-coded events" — which the eurozone-member parameterised test already does).

### L4 — `_DEFAULT_TRADED_COUNTRIES` in `finnhub_client.py` duplicates the EUR country set from `matcher.py`
Two sources of truth for "which countries count as EUR". Adding Austria (AT) tomorrow would require two coordinated edits or one silent drop-out. Pull the canonical set into one module (probably `matcher`) and have `finnhub_client` re-export the union of all currency country sets.

### L5 — `float(best["actual"])` and `float(best.get("estimate"))` raise on non-numeric strings
If Finnhub ever returns "5.25%" or "53.6 (revised)" instead of a number, the lookup raises. There is no try/except. Realistically this is rare for the indicators on the alias map, but the v1 spec's emphasis on fail-closed suggests degrading gracefully.

### L6 — `previous` field is not converted to `float` while `actual` and `forecast` are
Inconsistent typing in the returned dict. Either convert all three or document.

### L7 — Env-var parsing happens at import and raises on bad values
`float(os.getenv(...))` for `FETCH_TIMEOUT`, `POLL_INTERVAL`, `DEVIATION_THRESHOLD`. A typo in the deployment config takes the whole module down at import. Wrap with try/except + warn-and-default, or document the contract clearly.

### L8 — Minor double-fetch race in `poll_for_actual`
If two threads both pass the rate-limit check during a single 10-s window (because they raced before `last_fetch` was updated), both will call `fetch_calendar`. The "last writer wins" is benign — no correctness issue — but it's a wasted HTTP round-trip. Wrap the fetch in a `_fetch_in_progress` flag if it ever matters; today, it doesn't.

---

## Per-question response (review brief items 1-12)

| # | Question | Answer |
|---|---|---|
| 1 | Country filter placement | **Correct.** `matcher.py:186` — applied before scoring. No alternative match path (no TE fallback). Regression test pinned. ✓ |
| 2 | Same-currency cross-country (EUR DE vs FR) | **Order-dependent.** See H3. No deterministic tiebreaker. |
| 3 | Time zone handling | Fetcher uses UTC; matcher doesn't compare on time; date rollover drops late-day events. See M3. Caller-supplied query time not part of the matcher signature. |
| 4 | Fuzzy score thresholds | Min: 2 overlapping words, 50 % of shorter side. Loose enough to allow m/m vs q/q ambiguity (M1) but high-confidence by alias map for known events. |
| 5 | Block pairs (ADP/NFP) | **One-directional only.** ADP→NFP blocked; NFP→ADP NOT blocked. See H2. |
| 6 | Cache invalidation | Rate-limited to POLL_INTERVAL (10 s default). Single failure WIPES cache. See H4. |
| 7 | Currency normalisation | `strip().upper()` covers `"eur"`, `"EUR "`, `" gbp "`, `None`, `""`. Test coverage is good. ✓ |
| 8 | Impact widening (HIGH+MEDIUM) | Cache size remains small; no downstream consumer in this branch assumes HIGH-only. Impact field preserved in event dict. ✓ |
| 9 | Fail-closed behaviour | **Partially.** Matcher returns `None` on no match, and `get_actual_for_event` returns `None`. Cache-wipe-on-failure (H4) creates a fail-open window. Risk layer must explicitly interpret `None` as "block" (not visible from this branch). |
| 10 | Test quality | Regression test is rigorous and would fail without the fix. Some weak names (L1, L2). Three core paths are untested (M4). |
| 11 | requests / credential leak | **YES, leaks.** `requests` exception strings contain the full URL with the token. See H1. |
| 12 | Implementation-detail tests | Two found: L3. Defensible but improvable. |

---

## Final recommendation: **APPROVE WITH MINOR CONDITIONS**

The country-filter regression fix at the heart of this port is correct, tested, and tightly scoped. The 2026-04-23 DE-vs-GB PMI incident cannot recur through this code path.

**Conditions for merge:**

1. **Fix H1 (credential leak).** Five-minute change. Either move the token to the `X-Finnhub-Token` header or redact the URL in the log. This is a security gate.

2. **Fix H2 (NFP→ADP block-pair direction).** Same shape of bug as the original incident; missing-by-omission. Add the reverse entries to `_BLOCK_PAIRS` and pin with a regression test.

3. **Fix H4 (cache-wipe-on-failure).** Distinguish "fetch failed" from "fetch returned empty". Preserve last good snapshot on transient failures. The current behaviour is a fail-open regression vs. the stated fail-closed posture.

These three are quick, scoped, and unblock Phase 4 from being built on a flawed substrate. Recommended to land in the same PR.

**Recommended follow-ups (not merge-blocking, but should land before Phase 4 ships):**

- H3 (deterministic EUR cross-country tiebreaker)
- H5 (unify `beat_miss` semantics)
- H6 (validate Finnhub `surprise` unit assumption)
- H7 (add `upcoming_events_for_currency` for blackout windowing; preserve event time)
- M4 (test the three untested core paths)

LOW items are housekeeping.
