# Phase 7 (feature/feed) — adversarial review

- **Branch reviewed:** `feature/feed` (uncommitted working tree on top of `develop` @ `c7c4a17`)
- **Base:** `develop`
- **Date:** 2026-05-15
- **Reviewer:** AutoBot-OG (read-only audit pass)
- **Scope:** Phase 7 feed layer — `src/feed/types.py`, `constants.py`, `rolling_buffer.py`, `archive.py`, `hydration.py`, `feed_manager.py`, `lightstreamer/client.py`, `lightstreamer/parsers.py`, `pyproject.toml` deltas, and all `tests/unit/test_feed_*.py`.
- **Test status:** `72 feed tests passed in 1.29s`; full project suite `645 passed in 3.14s`.

---

## Headline

**Two CRITICAL issues, four HIGH, plus the usual scattering of MEDIUM/LOW.**

The two critical findings sit on the live-data hot path and could each on
their own corrupt every M5 candle the bot stores, with downstream blast
radius across indicators, strategies, and the risk layer:

1. **UTM is treated as bar OPEN time** in `parse_chart_payload`, but the
   IG Lightstreamer convention for consolidated CHART feeds is that UTM
   is the *update* time (the time of the most recent tick within the
   in-progress bar). If UTM advances mid-bar, every payload yields a
   different `close_time`, and the buffer/archive collect hundreds of
   spurious bars per real 5-minute window. The single-sample probe
   (UTM exactly on a 5-min boundary, `CONS_END=0`) cannot disambiguate.
2. **Price scaling is unhandled.** The probe payload (verbatim ground
   truth) shows `BID_OPEN = "13339.2"` for GBPUSD spreadbet — i.e.
   prices in spreadbet *points* (×10000 of decimal). The test fixture
   named `PROBE_PAYLOAD` silently divides by 10000 (`"1.33392"`),
   masking the issue. The parser averages bid+ofr as-is; downstream
   `pip_size_for("GBPUSD") = 0.0001` math would interpret the resulting
   `~13339.65` as ~133 million pips. If production points at a CFD
   account the scaling is moot; if it points at a spreadbet account
   (which is what the probe used, `CS.D.GBPUSD.TODAY.IP`), the feed is
   instantly wrong.

The HIGH issues include a hydration fallback that drops the cache on
REST failure even when cache alone would suffice, a `snapshotTime`
timezone assumption that may be off by an hour during BST, and a
gap-fill that emits a single `GAP_FILLED` event instead of replaying
intermediate `BAR_CLOSE`s — silently starving the strategy layer of
closes during outages.

The test suite is clean and well-structured for the cases it covers,
but a handful of fixtures use synthesised payloads that diverge from
the real probe / IG wire shapes, which is what hid both criticals.

**Recommendation: RE-WORK.** Both C-class issues need verification
against a live multi-tick probe and a fix before this branch is safe
to merge into `develop`. The HIGH items should be addressed in the
same pass; MEDIUM/LOW can land as follow-ups.

---

## Findings

### C1 (CRITICAL — candle-correctness) — `parse_chart_payload` treats UTM as bar open time, but IG/LS convention is UTM = update time

**File:** `src/feed/lightstreamer/parsers.py:91-98`

```python
open_time = datetime.fromtimestamp(utm_ms / 1000.0, tz=timezone.utc)
close_time = open_time.replace(microsecond=0) + _five_minutes()
```

The docstring asserts:

> IG sends bar OPEN time in UTM. The Candle convention is
> close_time = open_time + 5min.

This claim is not supported by the probe and contradicts the
Lightstreamer protocol. `UTM` is documented as **Update Time in
Milliseconds** — for consolidated CHART subscriptions in MERGE mode,
UTM is the timestamp of the latest tick consolidated into the bar, not
the bar's anchor. The probe captures a single payload (UTM
`1778837400000` = 2026-05-15 09:30:00 UTC, `CONS_END=0`) which happens
to fall exactly on the 5-minute boundary; with only one sample we
cannot distinguish "UTM is constant per bar = open time" from "UTM is
the latest-tick time, currently equal to the boundary because this is
the first tick of the bar."

**Failure mode if UTM advances within a bar (the LS-protocol
interpretation):**

Within a single real 5-min bar (e.g. 09:30:00→09:35:00 UTC) the LS
session might fire dozens of MERGE updates. With UTM advancing each
time, the parser produces:

| update | UTM            | close_time                    |
|--------|----------------|-------------------------------|
| 1      | 09:30:00.000   | 09:35:00                      |
| 2      | 09:30:42.117   | 09:35:42.117                  |
| 3      | 09:31:15.880   | 09:36:15.880                  |
| ...    | ...            | ...                           |
| N      | 09:34:59.997   | 09:39:59.997                  |

`RollingBuffer.push` accepts any candle whose `close_time` is
strictly greater than the latest stored bar — so every mid-bar update
becomes a *new* bar, the deque fills with hundreds of fictitious bars
per real bar, indicators see ramp-shaped sequences, and the boundary
crossing path in `FeedManager._on_ls_update` fires on every tick (it
checks `candle.close_time > prev_close`, which is true every time UTM
advances). The archive then accumulates the same junk on every
`CONS_END=1` we miss because the bar got "closed" implicitly by
boundary crossing first.

**Why the test suite missed this:**

`test_parse_full_payload_yields_mid_ohlc` uses one payload with UTM on
a 5-min boundary. `test_first_update_emits_bar_update_only`,
`test_cons_end_flip_emits_bar_close`, and the boundary-crossing test
all construct candles directly via `_candle("GBPUSD", offset_min=N)`
where `offset_min` is an integer multiple of 5 — so no in-bar tick
sequence is ever exercised.

**Suggested fix:**

The defensive interpretation is to derive the bar anchor from UTM by
rounding down to the bar period:

```python
open_time = datetime.fromtimestamp(utm_ms / 1000.0, tz=timezone.utc).replace(microsecond=0)
# Round down to 5-minute boundary regardless of where in the bar UTM points.
mins = (open_time.minute // 5) * 5
open_time = open_time.replace(minute=mins, second=0)
close_time = open_time + timedelta(minutes=5)
```

But before committing to a fix, **re-run the probe** so it captures
the full update stream of a single bar (15-30 seconds of one item
update flow, recording every `UTM` value alongside `CONS_END`). That
is the only way to verify what UTM actually does. The legacy
`native_5m_source.py` path sidesteps the question by only acting on
`CONS_END=1` — Phase 7 explicitly wants intra-bar `BAR_UPDATE` events,
so the parser cannot punt the way the legacy did.

---

### C2 (CRITICAL — wire-format) — Probe payload (ground truth) is scaled ×10000 relative to the test fixture; parser assumes decimal prices

**Files:**
- `scripts/probe_lightstreamer_output_2026-05-15.json` — real demo payload
- `tests/unit/test_feed_lightstreamer_parsers.py:15-29` — fixture named `PROBE_PAYLOAD`
- `src/feed/lightstreamer/parsers.py:106-116` — bid/ofr averaging without scaling

The probe captured (verbatim) on `CS.D.GBPUSD.TODAY.IP` (spreadbet) at
`demo-apd.marketdatasystems.com`:

```json
"BID_OPEN": "13339.2",
"BID_CLOSE": "13340.8",
"OFR_OPEN": "13340.1",
"OFR_CLOSE": "13341.7"
```

The test fixture, headed "Probe payload (2026-05-15)":

```python
"BID_OPEN": "1.33392",
"BID_CLOSE": "1.33408",
"OFR_OPEN": "1.33401",
"OFR_CLOSE": "1.33417"
```

The test values are the probe values divided by 10000. IG spreadbet
markets quote in "points" where 1 point = 0.0001 of the decimal price
unit — so 13339.2 points *is* 1.33392 GBP/USD, but the wire format
emits the integer-points value.

`parse_chart_payload` performs `(bid + ofr) / 2.0` without scaling, so
in production against the probed account it would emit candles with
`open=13339.65, high=…, low=…, close=…`. Downstream:

- `config.pair_config.pip_size_for("GBPUSD") = 0.0001` — `price_to_pips(13339.65 - 13339.6)` = 0.5 pips (this part still works because differences scale linearly), **but**
- Strategy code comparing `candle.close` to absolute price levels (entry, SL, TP — see `execution/executor.py` and risk-rule price floors) would be 10000× off.
- The REST history parser in `hydration.parse_ig_history` reads `openPrice.bid` / `openPrice.ask` — if IG returns those in points for spreadbet, the same break exists on the cached side; the buffer would then mix-and-match scaled REST bars with scaled LS bars and *look* consistent until a CFD account is plugged in.

If production is a CFD account, both REST and LS will quote in decimal
and Phase 7 happens to work. The locked design comments don't say
which.

**Why the test suite missed this:** the only fixture that claims to
mirror the probe was hand-edited to be decimal; nothing in the suite
ever consumes the actual JSON the probe wrote. The probe JSON is
checked in (`scripts/probe_lightstreamer_output_2026-05-15.json`) but
nothing references it from a test.

**Suggested fix:**

1. Decide explicitly which account type Phase 7 targets. Document it
   in `feed/constants.py` and (ideally) bake the scale factor into a
   per-pair constant in `config.pair_config`. Reading it from the
   environment would mirror the existing `IG_ACC_TYPE` pattern.
2. Add a test that loads
   `scripts/probe_lightstreamer_output_2026-05-15.json` from disk and
   feeds the verbatim `sample_payload` through `parse_chart_payload`,
   then asserts the candle prices are in the expected decimal range
   for GBPUSD (e.g., `0.5 < candle.open < 3.0`). That single assertion
   would have caught this.
3. Rename the `PROBE_PAYLOAD` constant or annotate the comment to
   make clear it is not the probe-verbatim payload.

---

### H1 (HIGH — degraded availability) — `hydrate_pair` returns `"failed"` and drops the cache when REST top-up errors, even though cache alone would seed the buffer

**File:** `src/feed/hydration.py:331-352`

```python
elif cached:
    try:
        raw = fetcher(epic, resolution, backfill_bars)
        ...
    except Exception as exc:
        logger.error("hydrate_pair(%s): REST top-up failed: %s — using cache only", pair, exc)
        return PairHydrationReport(
            pair=pair,
            mode="failed",
            cached_bars=len(cached),
            rest_bars=0,
            final_buffer_size=0,
            newest_close_time=None,
            error=f"REST top-up failed: {exc}",
        )
```

The log says "using cache only" but the return-path:

- skips `buffer.bulk_append(combined)` entirely (the merge block is below
  the `elif`/`except`),
- sets `final_buffer_size=0` and `newest_close_time=None`,
- stamps `mode="failed"`.

So in the realistic recovery scenario — IG REST is briefly down, but
we have 100 cached bars from the last session that are merely stale —
the buffer stays empty and Phase 8 cannot trade. The cache is the
whole point of the cache-first design; collapsing to "failed" on a
single transient REST hiccup defeats it.

`test_hydrate_rest_failure_with_cache_returns_failed` asserts this
broken behaviour (`assert report.mode == "failed"`), so a fix must
update both the implementation and the test.

**Suggested fix:** on REST failure with a non-empty cache, fall
through to the merge block with `rest_bars=[]` and mark the mode
something distinct (`"cache_only_degraded"` or
`"cache_plus_rest_failed"`) so the caller can decide whether to alert
ops. Hydration overall should be `ok=True` if every pair has *some*
data, and `ok=False` only when at least one pair has neither cache
nor REST.

---

### H2 (HIGH — silent BAR_CLOSE loss on reconnect gap-fill) — only the last gap-filled bar gets a `GAP_FILLED` event; strategies that listen on `BAR_CLOSE` miss intermediate bars

**File:** `src/feed/feed_manager.py:441-483`

```python
state.buffer.bulk_append(missing)
state.archive.append_many(missing)
state.last_emitted_close = missing[-1].close_time
state.last_emitted_was_closed = True
self._dispatch(
    FeedEventKind.GAP_FILLED,
    pair=state.pair,
    candle=missing[-1],
    ...
)
```

When `missing` is e.g. 3 bars (a ~15-minute gap), the buffer + archive
get all three, but only the latest is delivered to event callbacks.
The `GAP_FILLED` event documentation in `types.py` is explicit that
`candle` is the latest backfilled bar, but a strategy that subscribes
to `BAR_CLOSE` (the natural pattern in Phase 8 — "on every closed bar
re-run the decision pipeline") will silently *not* re-run for the 2
older gap-filled bars.

The documented behaviour is well-intentioned (callers should look at
`latest_candle` after `GAP_FILLED`), but it shifts a responsibility
onto every consumer. The blast radius is that on every reconnect with
`gap > 5 min`, intermediate bars enter the buffer without strategies
seeing them — strategies that re-evaluate the closed-bar set on every
`BAR_CLOSE` may end up disagreeing about position state from one
restart to the next.

**Suggested fix:** dispatch one `BAR_CLOSE` per gap-filled bar in
order, followed by a single `GAP_FILLED` summary event (or merge the
two — `GAP_FILLED` with `candle=missing[i]` per bar, and a final
no-candle envelope with bars-filled metadata). Phase 8 should be able
to write idiomatic "on every close, re-evaluate" code without
worrying about the gap-fill path.

---

### H3 (HIGH — timezone correctness for REST history) — `_parse_ig_timestamp` assumes naive `snapshotTime` is UTC, but IG's v1 REST endpoint frequently returns broker-local time (London)

**File:** `src/feed/hydration.py:230-252`

```python
for fmt in candidates:
    try:
        dt = datetime.strptime(s, fmt)
    except ValueError:
        continue
    return dt.replace(tzinfo=timezone.utc)
```

The hydration parser tries v2's `snapshotTimeUTC` first (good), but
falls back to v1's `snapshotTime`, which IG's `/prices` endpoint
returns in broker-local time without an explicit offset. During BST
(late March → late October) that's UTC+1, so every parsed candle's
`close_time` would be off by an hour relative to the LS feed (which
*is* UTC via UTM). Mixing the two via `bulk_append` would produce
duplicate-looking bars one hour apart and likely confuse the dedup
logic.

The docstring claims "IG's spreadbet feed publishes in UTC", but
behaviour is account / endpoint dependent. The legacy
`autobot.py:_normalize_hist_to_df` sidesteps the question by routing
through `trading_ig.format_prices`, which normalises to UTC via its
own DateTimeIndex handling. Phase 7's `parse_ig_history` bypasses
`trading_ig` and parses the raw dict directly.

**Suggested fix:** prefer `snapshotTimeUTC` (already done), and if
falling back to `snapshotTime`, parse via `trading_ig.format_prices`
or apply an explicit `pytz.timezone("Europe/London")` localisation
followed by `astimezone(timezone.utc)`. Alternatively, refuse to use
`snapshotTime` as a UTC source and raise instead — better a loud
error on cold-start than silently shifted candles.

---

### H4 (HIGH — observability) — out-of-order LS payloads are dropped with a `DEBUG` log; no event, no metric, no operator signal

**File:** `src/feed/feed_manager.py:404-407`

```python
logger.debug(
    "LS %s: stale payload close_time=%s < last_emitted=%s — dropped",
    pair, candle.close_time.isoformat(), prev_close.isoformat(),
)
```

The third path of `_on_ls_update` swallows out-of-order updates with a
`DEBUG` log. If IG ever ships an out-of-order payload due to a network
re-order or a bug in the LS SDK, ops sees nothing in normal logging
(production runs at INFO) and no `FEED_*` event surfaces. This kind
of failure is *exactly* what the FeedEvent envelope was designed for —
silent drops will compound and only surface as "the strategy
disagrees with the visible price action on the chart" days later.

**Suggested fix:** emit a new `FEED_OUT_OF_ORDER` event (or fold into
`debug` on a `BAR_UPDATE` event with `dropped=True`), and bump the log
to `WARNING`. This is cheap to do and protects against the silent
slow-corruption failure mode.

---

### M1 (MEDIUM — race window) — Buffer is written before archive in hydration; an archive `OSError` (disk full, perms) corrupts the report

**File:** `src/feed/hydration.py:373-384`

```python
if combined:
    buffer.bulk_append(combined)
    archive.append_many(rest_bars)  # may raise OSError
```

If `archive.append_many` raises, the function returns up to
`hydrate_pairs`, which catches and stamps `mode="failed"` —
**but the buffer is already populated.** Phase 8 starts trading on a
buffer that disagrees with what disk says was written. On the next
restart the cache is short → REST top-up → may duplicate state in the
buffer if the archive write only partially succeeded.

**Suggested fix:** swap the order — archive first, then buffer. Or
wrap both in a try/except that explicitly notes the buffer-archive
divergence and refuses to mark `ok` until reconciled.

---

### M2 (MEDIUM — archive concurrency) — `CandleArchive.append` is not thread-safe; only safe by accident given the current call graph

**File:** `src/feed/archive.py:112-153`

Two threads calling `append` for the same pair would:

1. both pass the `ts_ms <= self._last_ts` check (no lock),
2. both open the file in `"a"` mode,
3. interleave bytes — CSV row corruption.

In practice the only writers are:
- The hydration thread pool (one thread per pair, distinct archives),
- The LS reader thread (one for all pairs, sequential `_on_ls_update`),
- The same LS reader thread again, via `_gap_fill_on_resume`.

So in the current call graph there's never a concurrent write. But
this is brittle — if Phase 8 ever runs a parallel
"flush-pending-bars" watchdog or a side-channel REST poll, it would
silently corrupt.

**Suggested fix:** add a `threading.Lock` to `CandleArchive` and hold
it for both the dedup check and the file write. Cost is negligible
(one bar per 5 minutes per pair); defence-in-depth is worth it.

---

### M3 (MEDIUM — boundary missed for first-ever live update) — boundary-crossing path requires `prev_close is not None`, but the *first* LS update after a fresh start with no cache writes only a `BAR_UPDATE`

**File:** `src/feed/feed_manager.py:339-401`

If hydration finds no cache and REST fails (per H1 above the failure
behaviour is wrong anyway, but assume H1 is fixed so this is the
"truly first cold start, REST is up, but the first bar pulled is
already in-progress") then the first LS update lands with
`prev_close=None`. Path 2 catches that, emits `BAR_UPDATE`. So far so
good.

But if that first bar is *already past its boundary* (rare, but
plausible if the LS subscription catches the trailing CONS_END=1 of
the previous bar), the manager:

- sees `prev_close=None`,
- enters Path 2 (treated as in-progress),
- pushes the candle, emits `BAR_UPDATE`,
- if `cons_end=True`, emits `BAR_CLOSE`.

That's correct for *that* bar. The next LS update for the *new* bar
gets Path 1, but `prev_was_closed=True`, so the close-emission step
is skipped. Also correct.

The narrow edge: a LIVE update arriving during hydration (start_live
hasn't been called, the LS subscription doesn't exist). The
`_on_ls_update` callback is wired only after `start_live`, so this
window isn't possible. ✓

I'm calling this MEDIUM because it's not currently broken, but the
documentation in the `_PairState` dataclass + the boundary-crossing
docstring should explicitly note this dance — Phase 8 will read this
code expecting a clean lifecycle and the first-update edge cases
deserve a comment.

---

### M4 (MEDIUM — connect timeout silently swallows `STALLED`) — `connect()` only returns on `CONNECTED:*` but the SDK can wedge in `STALLED` for the entire timeout window

**File:** `src/feed/lightstreamer/client.py:159-172`

```python
while time.time() < deadline:
    status = self._safe_status()
    if status.upper().startswith("CONNECTED:") or status.upper() == "CONNECTED":
        ...
        return status
    time.sleep(0.1)
raise TimeoutError(...)
```

If the SDK reports `STALLED` for the entire window, we raise
`TimeoutError` after 15 seconds. The status is in the error message,
but `_on_status` was never invoked because `STALLED` is not a
transition the listener saw (the listener only fires on
`onStatusChange`, which the SDK only emits when state changes — if
`STALLED` is the *initial* state, no callback fires).

Result: a wedged session gets a generic `TimeoutError` rather than a
`FEED_STALE` event, ops sees a startup crash, and the manager has no
opportunity to fall back to "stay on cache, retry later."

**Suggested fix:** treat `STALLED` (and anything not `CONNECTED:*`)
that persists for half the timeout as a `FEED_STALE` event before
giving up. Either fire the status callback synchronously from the
poll loop, or let `connect()` raise a more specific
`LightstreamerStartupError` that the manager translates into a
`FEED_STALE` envelope.

---

### M5 (MEDIUM — fixture fidelity) — `_ig_price` test fixture uses `"%Y-%m-%d %H:%M:%S"` while real IG ships `"%Y/%m/%d %H:%M:%S"`

**File:** `tests/unit/test_feed_hydration.py:24-35`

```python
"snapshotTime": open_t.strftime("%Y-%m-%d %H:%M:%S"),
```

Real IG `/prices` v1 returns slashes (`"2026/05/15 13:00:00"`), per
the hydration docstring. The parser accepts both, so the tests pass —
but if someone deletes the slash format from `_parse_ig_timestamp`
(thinking it's redundant), the tests stay green while production
breaks. The test should use the real wire shape.

**Suggested fix:** swap the test fixture to slashes. Or add a small
parameterised test that exercises every format `_parse_ig_timestamp`
claims to handle.

---

### M6 (MEDIUM — orphaned `archive_dir_for_tests` helper) — defined but not referenced

**File:** `src/feed/archive.py:284-292` — `archive_dir_for_tests`
appears unused. Search across `tests/` confirms no callers.

This is a harmless code smell but signals an unfinished test pattern.
Either wire it into the test fixtures or delete it.

---

### M7 (MEDIUM — incorrect test comment) — UTM-to-UTC comment is wrong

**File:** `tests/unit/test_feed_lightstreamer_parsers.py:15`

```python
# Probe payload (2026-05-15) — UTM 1778837400000 = 2026-05-15 04:50:00 UTC.
```

`1778837400000 ms` is **2026-05-15 09:30:00 UTC**, not 04:50:00. The
test assertion below uses `09:35` correctly, so the bug is in the
comment only. Trivial to fix, but a sign-of-hurry artefact that
deserves a separate flag because it sits next to the C2 fixture-vs-
probe mismatch and could be the same kind of "transcribed from
memory, not from the JSON" error.

---

### M8 (MEDIUM — watchdog can fire during market-closed hours) — `MARKET_HOURS_GUARDS` is declared but `watchdog_stale_pairs` ignores it

**Files:** `src/feed/constants.py:130-144`, `src/feed/feed_manager.py:303-320`

`MARKET_HOURS_GUARDS` is a top-level constant. The constants
docstring says "the watchdog gating: do not emit FEED_STALE during
weekend / FX-close hours." But `watchdog_stale_pairs` returns stale
pairs without consulting `MARKET_HOURS_GUARDS`, and `_gap_fill_on_resume`
doesn't either. The guard lives only in the docstring.

If Phase 8 calls `watchdog_stale_pairs()` from a background loop over
the weekend, it will return every pair as stale, and any operator
alerting pinned to that output spams.

**Suggested fix:** import `MARKET_HOURS_GUARDS` into `feed_manager`
and add a market-open check before reporting stale pairs (and before
emitting `FEED_STALE`). Or delete the constant and document the
responsibility as Phase 8's.

---

### L1 (LOW — naming) — `LIGHTSTREAMER_CANDLE_ADAPTER` is the item-pattern, not an "adapter"

**File:** `src/feed/constants.py:85`

`"CHART:{epic}:5MINUTE"` is a Lightstreamer *item name*, not an
adapter set (the adapter set is `"DEFAULT"`, passed to
`LightstreamerClient(endpoint, "DEFAULT")`). Calling it
`LIGHTSTREAMER_CANDLE_ITEM_TEMPLATE` would match LS terminology and
reduce the chance of someone later confusing it with the adapter set.

---

### L2 (LOW — test brittleness) — `test_subscribe_pair_uses_locked_adapter_and_fields` depends on real `lightstreamer.client.Subscription` having `getMode()/getItems()/getFields()/getListeners()` accessors

**File:** `tests/unit/test_feed_lightstreamer_client.py:181-189`

The test mocks the LS *client* but uses the real LS *Subscription*
(`_build_subscription` always imports the real class when the SDK is
installed). If the LS SDK ever changes its public accessor API across
versions, this test breaks. Currently safe (we pin to 1.0.3 in
`pyproject.toml`), but worth a comment in the test.

---

### L3 (LOW — minor inefficiency) — `_refresh_last_ts_from_disk` reads the *whole* archive on construction

**File:** `src/feed/archive.py:259-281`

The docstring acknowledges this and points out that "a year of M5 ≈
75k rows" is fine. Fair, but a tail-only read (open `"rb"`, seek
backwards line-by-line, parse the last valid line) is a one-page
disk read versus a multi-MB read on every startup. Low priority — a
micro-optimisation. Mentioned for the record.

---

### L4 (LOW — dead branch in disconnect) — `disconnect` swallows everything

**File:** `src/feed/lightstreamer/client.py:196-212`

`disconnect()` ignores `unsubscribe` and `disconnect` failures with a
warning log. Reasonable for shutdown idempotency. The only worry: if
the SDK's `disconnect` doesn't return cleanly, we set `self._client =
None` but the LS reader thread may still be alive. Practically this
means a callback could fire on a torn-down manager. Already mitigated
by `_dispatch`'s try/except, and `FeedManager.stop` is the only path
that nullifies `self._subscriber`. Worth a tracking comment but not
worth changing.

---

### L5 (LOW — substring trap reappears in probe script) — `'CONNECTED' in last_status.upper()` in `scripts/probe_lightstreamer.py:255`

**File:** `scripts/probe_lightstreamer.py:255`

```python
if "CONNECTED" in last_status.upper():
```

The exact substring trap the Phase 7 production code carefully
avoids. This is a probe script, not production, and it's already
served its purpose, so the bug never materialised. But anyone reading
the probe and copy-pasting "this is how IG LS connects" gets a footgun.
Worth a one-line `# matches DISCONNECTED too — fixed in feed_manager.py`
comment.

---

## Test suite observations

- **72 tests, 1.29s.** No slow tests; no networked tests; suite is
  hermetic.
- **Threading test (`test_threading_concurrent_pushes_are_consistent`)
  is partial.** Four workers in disjoint timestamp ranges exercise the
  lock under append-only writes, but there is no concurrent
  *reader* (e.g., a thread calling `snapshot()` while writers push).
  The lock would catch a reader/writer race too, but the test doesn't
  prove it.
- **No test loads `scripts/probe_lightstreamer_output_2026-05-15.json`.**
  As called out in C2, a single verbatim-probe ingestion test would
  have caught the price-scale issue and the UTM convention issue both.
- **No multi-pair Lightstreamer test.** All `test_feed_manager`
  scenarios use a single pair; the multi-pair path
  (`start_live → subscribe_pair × N → fan-out on a single LS thread`)
  is exercised only via `test_start_live_connects_and_subscribes_each_pair`,
  which checks the subscribe count but not the per-pair routing.
- **`test_archive_dedups_duplicate_close`** asserts only that the
  archive has 1 row after a single CONS_END=1 update; doesn't test
  that a follow-up CONS_END=1 for the same UTM is also dropped.
- **`test_long_gap_skips_rest_backfill`** correctly verifies that a
  90-min gap doesn't trigger REST, but doesn't verify that the
  buffer + archive remain consistent across the skipped gap, nor that
  a `FEED_RESUMED` event still fires (it does, per the code, but it's
  unasserted).

---

## Decision-by-decision pass against the locked design

| Decision                          | State                              |
|-----------------------------------|------------------------------------|
| BACKFILL_BARS=100                 | ✓ in `constants.py`                |
| BUFFER_CAPACITY=600               | ✓                                  |
| FRESHNESS_THRESHOLD_MIN=60        | ✓                                  |
| Cache-first hydration             | ✓ (but see H1)                     |
| REST budget bounded post-cold-start | ✓ (single caller per gap)        |
| LS adapter `CHART:{epic}:5MINUTE` | ✓                                  |
| LS mode MERGE                     | ✓                                  |
| LS fields (12 from probe)         | ✓ (locked tuple matches probe)     |
| Close signal `CONS_END=1`         | ✓                                  |
| Two-layer dedup                   | ✓ (manager + archive `_last_ts`)   |
| Reconnect via LS library          | ✓                                  |
| FEED_STALE / FEED_RESUMED / GAP_FILLED events | ✓ (but see H2)           |
| Callback registration             | ✓                                  |
| Callback runs on LS thread        | ✓ documented in `on_event` + `client.py` header |
| Lazy LS SDK import                | ✓                                  |
| Buffer + archive lock-protected   | Buffer ✓, archive partial (M2)     |
| FeedManager `_state_lock`         | ✓                                  |
| Watchdog gated on market hours    | ✗ — constant declared, not enforced (M8) |
| Candle `source` field             | ✓ (`LS_NATIVE_5M` / `REST`)        |

---

## Final recommendation

**RE-WORK.**

C1 and C2 each can corrupt the entire candle stream. They need a
fresh multi-tick probe to disentangle (probe should capture the full
update sequence within a single 5-minute bar so the UTM semantics are
unambiguous, and should record the prices verbatim alongside the
expected decimal interpretation). H1–H4 should land in the same pass
since they all hit the same code paths the criticals do; the MEDIUMs
can follow up.

The architectural shape of the layer is sound: the cache-first
decision tree, the manager/parser split, the `FeedEvent` envelope,
the `RollingBuffer`/`CandleArchive` separation. Once the wire-format
and UTM questions are settled, this is a thin patch.

---

# Addendum — re-review pass on `feature/feed` @ `ca8ab21`

- **Branch reviewed:** `feature/feed` @ `ca8ab21`
- **Base:** `develop`
- **Date:** 2026-05-15
- **Reviewer:** AutoBot-OG (read-only verification pass)
- **Test status:** `654 passed in 3.19s` — full suite green, matches the
  committed claim. Feed-only subset is `81 passed in 1.31s` (was 72;
  9 new tests for C2/H1/H2/H3/H4).

## Disposition of original findings

| ID | Status | Note |
|----|--------|------|
| **C1** UTM ≠ open-time | **INVALID** | Disproved by `scripts/probe_lightstreamer_multitick_output_2026-05-15.json` — 92 updates over ~3 min with UTM = `1778847900000` constant (= 2026-05-15 12:25:00 UTC, the 12:25-12:30 bar's open). `unique_utm_values: 1, consecutive_utm_differs_count: 0`. The original concern was correctly raised given a single-sample probe, but multi-tick evidence resolves it. No code change required; comment in `parsers.py` could note "UTM is bar-anchored open time; verified by `probe_lightstreamer_multitick_output_*.json`" but that's a polish item, not a blocker. |
| **C2** Price scaling | **FIXED (TESTS + DOCSTRINGS)** | The parser was always correct; the bug was confined to test fixtures and the headline-misleading docstring. Verified below. |
| **H1** Hydration drops cache on REST failure | **FIXED** | Verified below. |
| **H2** Single GAP_FILLED hides intermediate BAR_CLOSEs | **FIXED** | Verified below. |
| **H3** `snapshotTime` timezone | **FIXED** | Verified below; minor DST-fold caveat documented. |
| **H4** Silent out-of-order drops | **FIXED** | Verified below. |
| M1–M8, L1–L5 | **Unchanged** | Out of scope for this pass, per task remit. |

## Verifications

### C2 — verbatim probe fixture, no scaling elsewhere

- `tests/unit/test_feed_lightstreamer_parsers.py:35-48` — `PROBE_PAYLOAD`
  now contains the raw probe values (`"BID_OPEN": "13339.2"`, …). The
  module docstring (lines 1-10) explicitly calls out the prior
  scaled-decimal version as the hidden bug.
- `tests/unit/test_feed_lightstreamer_parsers.py:72-95` — new
  `test_parse_verbatim_probe_json_from_disk` opens
  `scripts/probe_lightstreamer_output_2026-05-15.json`, pulls the
  `CHART:5MINUTE` `sample_payload`, parses it, and asserts the candle's
  open/close fall in `1000 < x < 100000` (the raw-points range). This is
  exactly the "single verbatim-probe ingestion test" called for in the
  original review, so a future fixture drift back to decimal scaling
  will fail loudly.
- `src/feed/lightstreamer/parsers.py:72-83` adds a multi-paragraph
  docstring block explaining the raw-points convention and pointing at
  the probe + the production AutoBotV1 rolling-CSV format. Inline
  comments at lines 120-124 reinforce "no decimal scaling" at the only
  place a reader might be tempted to add one.
- Cross-check: `grep -rn "1\\.33392\\|/ *10000\\|\\* *10000\\|scale.*[Pp]rice" src/feed/` —
  no results. No scaling sneaked in elsewhere.
- `test_parse_full_payload_yields_mid_ohlc` keeps a sanity guard
  (`assert 1000 < candle.open < 100000`) — a regression to decimal
  scaling would also break this.

C2 is properly fixed and double-protected by two independent tests.

### H1 — `cache_only_degraded` mode

- `src/feed/constants.py:83` declares `FEED_MIN_USABLE_BARS = _i("FEED_MIN_USABLE_BARS", 50)`.
- `src/feed/hydration.py:417-440` walks the three branches:
  - REST fails + `len(cached) >= 50` → `mode = "cache_only_degraded"`,
    `rest_bars = []`, `degraded_error = "REST top-up failed: …"`, then
    falls through to the merge block at line 461 which calls
    `buffer.bulk_append(combined)` (combined = cached only). The
    returned report has `cached_bars > 0`, `rest_bars = 0`,
    `final_buffer_size = len(buffer)`, `error = degraded_error`.
  - REST fails + `len(cached) < 50` → returns early with `mode = "failed"`,
    `final_buffer_size = 0`, `error = …`. Buffer is intentionally not
    populated; the strategy layer must refuse to trade.
- `HydrationReport.ok` (lines 136-138) is `all(p.mode != "failed")`. So
  `cache_only_degraded` counts as OK for trading purposes, matching the
  H1 spec. `degraded_pairs` property (lines 144-149) exposes the
  ops-visibility list.
- Tests:
  - `test_hydrate_rest_failure_with_short_cache_returns_failed` (30 bars,
    REST raises) → asserts `mode == "failed"`, `len(buf) == 0`.
  - `test_hydrate_rest_failure_with_usable_cache_degrades` (80 bars,
    REST raises) → asserts `mode == "cache_only_degraded"`,
    `cached_bars == 80`, `rest_bars == 0`, `len(buf) == 80`,
    `error` contains the REST exception.
- Walk-through of the threshold semantics:
  - 30 bars → fails (under threshold).
  - 50 bars → degrades (`>=` is inclusive).
  - 75 bars → degrades (above threshold).
  - 100 bars + REST fails → degrades (still tops out at cache because we
    only consider top-up after the cache passed the freshness check,
    which it didn't here).

Subtle gap: the **boundary** at exactly 50 bars is not tested
explicitly. The code uses `>=`, so 50 should degrade and 49 should
fail; the test pair (30, 80) does not pin the boundary. This is a LOW
test-quality gap, not a correctness gap.

### H2 — per-bar BAR_CLOSE on gap-fill, plus summary

- `src/feed/feed_manager.py:497-526` after `bulk_append` and
  `archive.append_many`, the loop dispatches one `BAR_CLOSE` per bar in
  `missing` (debug `reason="gap_fill_backfill"`), then a single
  `GAP_FILLED` summary (debug carries `bars_filled`, `first_close`,
  `last_close`).
- Test `test_gap_fill_emits_bar_close_per_backfilled_bar` seeds a
  20-min-old bar, drops/reconnects, returns 3 REST bars, and asserts
  exactly 3 `BAR_CLOSE` events with `reason == "gap_fill_backfill"`
  plus 1 `GAP_FILLED`. Asserts time order on the per-bar closes.

Walk-through against the question "could strategies double-process":

1. `_gap_fill_on_resume` sets `state.last_emitted_close = missing[-1].close_time`
   and `state.last_emitted_was_closed = True` **before** dispatching the
   per-bar events.
2. The next live LS payload then enters `_on_ls_update`:
   - If its `close_time > missing[-1].close_time` → Path 1 (boundary
     crossing). `prev_was_closed` is `True`, so the inner "close the
     prior bar" branch is skipped — no extra `BAR_CLOSE` for
     `missing[-1]`. Push the new bar, emit a fresh `BAR_UPDATE`. ✓
   - If `close_time == missing[-1].close_time` → Path 2 (in-progress
     update for the same bar). Push replaces the REST-sourced
     `missing[-1]` with the LS-sourced candle, emit `BAR_UPDATE`. The
     `cons_end and not prev_was_closed` guard prevents a redundant
     `BAR_CLOSE` (we already set `was_closed=True`). ✓
   - If `close_time < missing[-1].close_time` → Path 3 (out-of-order),
     dropped per H4. ✓

So no double-process. The trade-off the design has accepted is that a
strategy listening on `BAR_CLOSE` will receive REST-sourced bars
during gap-fill — their `source == "REST"` and the event `debug` has
`reason == "gap_fill_backfill"`, so strategies can filter if they want
LS-only logic.

One latent issue worth a comment (not flagging as a new finding):
during the per-bar `BAR_CLOSE` dispatch loop, `latest_candle(pair)`
returns `missing[-1]` for every iteration — not the bar being
dispatched. If a strategy callback calls back into `latest_candle`
during a per-bar BAR_CLOSE handler, it will see the wrong bar. The
documented contract is "consume `event.candle`, not `latest_candle()`
inside the handler", but it's worth a docstring note in `feed_manager.py`.

### H3 — Europe/London tz handling

- `src/feed/hydration.py:85` `_LONDON = ZoneInfo("Europe/London")`.
- `_parse_history_entry` (lines 219-237) prefers `snapshotTimeUTC` with
  `is_utc=True`; falls back to `snapshotTime` with `is_utc=False`.
- `_coerce_to_utc` (lines 310-320) honours explicit tzinfo first, then
  branches on `is_utc`.
- Tests:
  - `test_parse_history_v2_utc_passthrough` — `snapshotTimeUTC =
    "2026-05-15T13:00:00"` → close 13:05 UTC.
  - `test_parse_history_v1_bst_shifts_minus_one_hour` — `snapshotTime =
    "2026/06/15 13:00:00"` (BST) → close 12:05 UTC (13:00 London − 1h).
  - `test_parse_history_v1_gmt_no_shift` — `snapshotTime =
    "2026/01/15 13:00:00"` (GMT) → close 13:05 UTC.
  - `test_parse_history_explicit_offset_overrides_flag` —
    `snapshotTimeUTC = "2026-05-15T08:00:00-05:00"` → close 13:05 UTC.
    This catches the "what if IG sends an explicit offset even on a
    UTC-flagged field" case; the `if dt.tzinfo is not None: return
    dt.astimezone(timezone.utc)` branch in `_coerce_to_utc` is what's
    being exercised.

**DST edge cases — not tested, behaviour verified empirically:**

```text
spring-forward gap 01:30 BST (2026-03-29)  → 01:30 UTC  (treats as GMT, pre-shift)
fall-back ambiguous 01:30 BST/GMT (2026-10-25, fold=0 default)
                                            → 00:30 UTC  (first occurrence = BST)
fall-back ambiguous 01:30 fold=1            → 01:30 UTC  (second occurrence = GMT)
summer 13:00 BST (2026-06-15)               → 12:00 UTC  ✓
winter 13:00 GMT (2026-01-15)               → 13:00 UTC  ✓
```

The spring-forward gap can only matter if IG were to publish a bar
whose `snapshotTime` falls in the (non-existent) 01:00-02:00 BST gap —
which it can't because clocks jump. The fall-back ambiguity could in
principle mis-attribute one hour of bars per year to BST instead of
GMT (1h drift) if IG returns naive timestamps in that hour. In
practice, FX markets are closed during the weekend DST transitions
in the UK (transitions happen on a Sunday morning), so the affected
window is empty. **Recommend adding a `# DST caveat:` docstring note
to `_coerce_to_utc`** so a future maintainer doesn't get surprised.
Not severe enough to block merge.

### H4 — out-of-order observability

- `_PairState.out_of_order_count: int = 0` (line 132). Docstring
  explicitly cites H4.
- `FeedManager._on_ls_update` Path 3 (lines 423-437):
  `state.out_of_order_count += 1`, then `logger.warning(...)` with the
  count in the message.
- `FeedManager.out_of_order_counts()` (lines 310-321) returns
  `{pair: state.out_of_order_count}` for every configured pair.
- Tests:
  - `test_out_of_order_payload_logs_warning_and_bumps_counter` — pushes
    a baseline bar at offset=+5, then a stale bar at offset=0, asserts
    `out_of_order_counts() == {"GBPUSD": 1}`, asserts the WARNING log
    fired, and confirms no BAR_UPDATE/BAR_CLOSE for the stale candle.
  - `test_out_of_order_counts_starts_zero_for_each_pair` — multi-pair
    init shows `{"GBPUSD": 0, "EURUSD": 0}`.

Walk-through against the question "could a same-timestamp update get
counted":

- `_on_ls_update`:
  - Path 1: `close_time > prev_close` → BAR_UPDATE (+ possible CLOSE).
  - Path 2: `prev_close is None or close_time == prev_close` → BAR_UPDATE
    (+ possible CLOSE on cons_end flip).
  - Path 3: everything else, i.e. `close_time < prev_close` → counted.
- Same-timestamp updates (the realistic LS in-progress case) take
  Path 2, **not** Path 3. The counter is incremented only for true
  out-of-order. ✓

Thread-safety note: `out_of_order_count` is read outside `_state_lock`
in `out_of_order_counts()`. Reading an `int` attribute is GIL-atomic
in CPython, so reads can be off-by-one relative to a concurrent
increment but never corrupt or torn. Acceptable for an ops-metrics
read.

## New issues introduced by the fixes

None of the fixes introduce a CRITICAL or HIGH regression. A small
number of LOW-severity nits surfaced:

### A1 (LOW — docstring drift) — module docstring in `hydration.py` shows the old decision tree

**File:** `src/feed/hydration.py:10-23`

The module's top-level decision tree:

```text
if len(cached) >= BACKFILL_BARS and is_fresh(newest):
    # Cache-only: no REST call.
elif cached:
    # Cache-stale or short: REST top-up only the missing tail.
else:
    # No cache: REST fetch BACKFILL_BARS.
```

does not mention the H1 fallback (`cache_only_degraded`). A reader who
trusts the module docstring would miss the new mode. The
`PairHydrationReport` docstring (lines 95-110) covers all five modes
correctly. Recommend a one-line addition to the module-level tree:
"REST fails + cache >= MIN_USABLE_BARS: degrade to cache only".

### A2 (LOW — undocumented DST fold caveat) — `_coerce_to_utc` should note the fall-back ambiguity behaviour

**File:** `src/feed/hydration.py:310-320`

The function correctly handles 99.99% of timestamps but silently
defaults to fold=0 (BST) for the ambiguous fall-back hour. A one-line
docstring note ("naive timestamps in the fall-back ambiguous hour
default to BST/first-occurrence; explicit offset preferred") would
inoculate future readers against assuming GMT-by-default.

### A3 (LOW — gap-fill latest_candle nuance) — strategies must use `event.candle`, not `FeedManager.latest_candle()`, inside a gap-fill `BAR_CLOSE` handler

**File:** `src/feed/feed_manager.py:497-514`

During the per-bar BAR_CLOSE loop in `_gap_fill_on_resume`,
`state.buffer.bulk_append(missing)` has already placed *every*
backfilled bar in the buffer. So `latest_candle()` returns
`missing[-1]` for every dispatched event in the loop, not the
currently-dispatched bar. The contract documented in `on_event` and
in `types.FeedEventKind.GAP_FILLED` does imply "use `event.candle`",
but a Phase 8 author who reaches for `latest_candle()` inside a
`BAR_CLOSE` handler would see surprising results during reconnects.
One-line docstring addition to `_gap_fill_on_resume` (or to the
`on_event` contract) is the minimal fix.

### A4 (LOW — gap-fill blocks the LS thread on REST) — `_gap_fill_on_resume` is serial across pairs

**File:** `src/feed/feed_manager.py:471-530`

The loop is sequential: pair-1 REST → pair-2 REST → … on the LS
reader thread, all inside `_on_ls_status`. For 4 pairs × ~1-2s per
REST call, that's a 4-8 second window where the LS thread isn't
servicing live ticks. Practical impact is small (gap-fills are rare
and on already-resuming sessions, the LS SDK queues), but the parallel
pattern used by `hydrate_pairs` could be reused here. Not blocking.

## Test-quality observations on the new tests

- **Probe round-trip test** is the kind of fixture-anchored regression
  guard the original review explicitly asked for. Good.
- **H1 boundary** at exactly `FEED_MIN_USABLE_BARS=50` is not tested
  (30 fails, 80 degrades — gap of 20 either side). Not blocking, but a
  one-line parameterised test would seal it.
- **H2 test** explicitly asserts both the count (3) and the temporal
  ordering of the BAR_CLOSEs. Catches the bug it set out to.
- **H3 DST** transitions (spring-forward gap, fall-back ambiguity) are
  not exercised. The summer/winter tests prove the offset is *applied*
  but not the *transition* handling. Low priority — covered above
  empirically.
- **H4** test correctly distinguishes Path 3 from Path 2 (same-ts
  update goes to BAR_UPDATE, not the counter), and uses `caplog` to
  prove the WARNING fires. Solid.

## Final recommendation

**APPROVE FOR MERGE.**

All four HIGH items and the C2 issue are properly fixed with code
changes plus targeted tests that exercise the bug paths (not just
smoke tests). C1 was disproved by the multi-tick probe; the parser is
correct as written. No new CRITICAL or HIGH defects were introduced.
The four LOW-severity nits (A1–A4) are all docstring-or-polish items
that can land in a follow-up commit without blocking merge.

Full suite is green: **`654 passed`** as claimed in the commit
message. The unaddressed MEDIUM/LOW items from the original review
remain valid for a follow-up pass but are explicitly out of scope per
the re-review remit.
