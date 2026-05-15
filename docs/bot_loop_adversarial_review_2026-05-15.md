# Phase 8 (`src/bot/`) — adversarial review

- **Branch reviewed:** none — the work is **uncommitted on `develop`**
  (no `feature/bot` or `feature/bot-loop` branch was created). Local
  tip is `e977521` (Phase 7 cleanup).
- **Files reviewed:** `src/bot/{types,constants,logging_setup,
  preflight,loop,main}.py` and `tests/unit/test_bot_*.py`.
- **Date:** 2026-05-15
- **Reviewer:** AutoBot-OG (read-only audit pass)
- **Test status:** `704 passed in 3.74s`.

---

## Headline

**Two CRITICAL bugs, both of which directly invalidate the v1 risk
posture and would only surface in live trading. They are independent
and both must be fixed before this lands.**

1. **Force-close opens new positions instead of closing them.**
   `BotLoop._execute_force_close` pre-inverts the position direction
   and passes the *opposite* as `CloseRequest.position_direction`. The
   `feed.ig_rest.positions.close_position` wrapper *also* inverts —
   the two inversions cancel, so IG receives the position's *original*
   direction, which opens a new position rather than closing the
   existing one. The bot's daily EOD flatten and regime-transition
   close would therefore **double exposure** on every fire instead of
   flattening. None of the tests catch this because the fake
   `_FakeIGClient.close_position` doesn't assert on `position_direction`.

2. **The risk layer reads a regime engine that the bot never updates.**
   `bot.main._build_runtime` instantiates a fresh `RegimeEngine` and
   hands it to `RiskGuard`; the `BotLoop` separately instantiates one
   `RegimeEngine` *per pair* in `_pair_state[pair].regime_engine` and
   feeds it M5/H1 closes. The two trees never meet. So `RiskGuard`'s
   `get_recent_emissions` is permanently empty and
   `regime_live_at_last_h1_close()` is permanently `False`. The
   regime-instability circuit breaker is therefore **silently
   disabled** for v1 — the bot will not pause after volatility/regime
   churn even though the per-pair engine does detect it.

Beyond the criticals, there's one HIGH-severity correctness issue
(strategies consume a *partial* latest H1 bar between hour boundaries
because the M5→H1 resample includes the in-progress bin), a handful
of MEDIUM/LOW design and test-hygiene findings, and one repository
hygiene complaint (the entire Phase 8 implementation is uncommitted
on develop, with no feature branch — there is no PR to review against
and no revert path if a regression lands).

**Recommendation: RE-WORK.** C1 and C2 are direct money-loss /
risk-rule-disablement paths; H1 affects strategy correctness; and the
branch model violates the repo's "feature/* + adversarial review +
merge" pattern that every prior phase followed.

---

## Findings

### C1 (CRITICAL — money loss / wrong direction) — force-close fires the WRONG side and opens new positions

**File:** `src/bot/loop.py:584-615` (`_execute_force_close`)

```python
opposite = "SELL" if position.direction == Direction.BULLISH else "BUY"
request = CloseRequest(
    deal_id=position.deal_id,
    epic=self._pair_to_epic[position.pair],
    position_direction=opposite,                # ← pre-inverted
    size=position.size_units,
)
confirmation = self._client.close_position(request)
```

The contract on `CloseRequest` (`src/feed/ig_rest/types.py:68-86`) is
that `position_direction` holds the **position's own direction** and
the wrapper internally inverts it:

```python
# src/feed/ig_rest/positions.py:89
opposite = "SELL" if close.position_direction == "BUY" else "BUY"
raw = session.service.close_open_position(direction=opposite, …)
```

The Executor's emergency-close path uses this contract correctly
(`src/execution/executor.py:216-227` — passes the position's own
direction). The BotLoop pre-inverts, so the wrapper inverts again,
and IG receives the position's *original* direction.

Reproduced empirically:

```
BotLoop sets request.position_direction = SELL    (for a BULLISH position)
Wrapper sends to IG: direction = BUY
To close a BULLISH position, IG needs direction=SELL.
Wrapper sent BUY — IG would OPEN a new BUY position rather than close.
```

**Impact:** every force-close (Phase 4 EOD NY-close flatten, regime
transition exits) opens an equal-and-same-direction position instead
of closing the existing one. The local PositionManager state then
`remove`s the deal_id (the bot believes it closed) while the broker
holds *two* positions in the same direction. Reconciliation 10
minutes later sees the orphan as MISSING_LOCAL_KEPT and surfaces
operator alerts — and the orphan keeps running with the bot's
SL/TP not tracking it. **This is a direct money-loss path on every
EOD.**

**Why no test caught it:** `tests/unit/test_bot_loop.py:103-122`'s
`_FakeIGClient.close_position` ignores the request's
`position_direction` field and just returns `"ACCEPTED"`. The
existing `test_force_close_order_executes_close` asserts `deal_id`
and the post-close removal but not the side. Adding one assertion
would have caught this:

```python
assert pieces["ig"].close_calls[0].position_direction == "BUY"  # position's own side
```

**Suggested fix:** drop the local inversion — pass the position's
direction directly:

```python
position_direction = "BUY" if position.direction == Direction.BULLISH else "SELL"
request = CloseRequest(
    deal_id=position.deal_id,
    epic=self._pair_to_epic[position.pair],
    position_direction=position_direction,  # the position's OWN direction
    size=position.size_units,
)
```

Also: update the fake client in the test to assert direction inversion
end-to-end, and add a regression test that pins the exact
`position_direction` value sent.

---

### C2 (CRITICAL — risk-rule disabled) — RiskGuard reads a regime engine that never receives updates

**Files:**
- `src/bot/main.py:172-173` — constructs a fresh `RegimeEngine()` for `RiskGuard`
- `src/bot/loop.py:182-185` — `BotLoop` creates a *separate* per-pair engine map
- `src/bot/loop.py:478-500` — `_update_regime` calls `process_m5_close` / `process_h1_close` on `self._pair_state[pair].regime_engine` only
- `src/risk/guard.py:122-126` — `RiskGuard.allow_entry` reads `self.engine.get_recent_emissions` and `self.engine.regime_live_at_last_h1_close`

The `primary_regime` engine handed to `RiskGuard` is never the same
object as the engine the `BotLoop` maintains. So at every entry
decision:

```
Primary engine current_regime: TRANSITION
Primary engine is_live(): False
Primary engine regime_live_at_last_h1_close(): False
Primary engine recent_emissions in last hour: 0
```

**Impact on the risk pipeline** (`src/risk/rules/circuit_breakers.py`):
- The regime-instability rule counts emissions in `recent_emissions`.
  Since the list is always empty, the rule never trips — instability
  cooldowns never start. **The bot will keep trading through a
  volatile regime churn the per-pair engine has classified
  correctly.**
- The cooldown extension after primary cooldown expires waits for
  `live_at_last_h1_close = True`. With the stale engine it's always
  `False` — but since no cooldown ever starts in the first place,
  this is dormant.

Net effect: the regime-instability circuit breaker — the v1 risk
layer's primary brake against trading into a regime breakdown — is
**silently disabled**.

Note the contradicting code paths in the bot itself:
- `BotLoop._run_signal_pipeline` (line 633-634) correctly checks
  `self._pair_state[pair].regime_engine.is_live()` — strategies are
  gated on the *live* per-pair regime.
- Signals that pass through then hit `RiskGuard.allow_entry` which
  consults the *stale* primary engine.

So strategy generation is regime-aware; risk-layer veto is not.

The author's intent is documented in the `main.py:166-173` comment:

> Risk guard — needs at least one regime engine. v1 design is
> per-pair regime; the BotLoop instantiates per-pair engines
> internally, so we hand RiskGuard the "first pair's" engine for
> the rule that consults regime state (TREND-overnight-hold). v1
> trades GBPUSD only, so this collapses cleanly.

— but the bot does **not** hand `RiskGuard` the same object as
`BotLoop._pair_state["GBPUSD"].regime_engine`. It hands it a brand
new engine that nothing updates. The "collapses cleanly" assertion
is wrong.

**Suggested fix:** thread the regime engine through one of:

1. **Constructor sharing.** Construct the engine map *before*
   `BotLoop` and pass into both:

   ```python
   per_pair_engines = {p: RegimeEngine() for p in config.pairs}
   risk_guard = RiskGuard(engine=per_pair_engines[config.pairs[0]])
   bot = BotLoop(..., pair_state_engines=per_pair_engines, ...)
   ```

2. **Accessor on BotLoop.** After construction, fetch the engine and
   hand to RiskGuard:

   ```python
   bot = BotLoop(...)
   risk_guard.engine = bot.regime_engine_for("GBPUSD")
   ```

3. **Per-pair RiskGuard.** A `dict[pair, RiskGuard]` and route
   entries through the right one — cleanest for multi-pair but heaviest.

Whichever option lands, also add a test that asserts the
`RiskGuard.engine` instance is the same object as the BotLoop's
per-pair engine after construction.

**No test caught this either** because `_FakeRiskGuard` doesn't
consult any engine — it just records `allow_entry` calls. A test
that builds a real `RiskGuard` with the primary engine and asserts
`risk_guard.engine.is_live()` is `True` after seeding the per-pair
engine with H1 closes would have caught the divergence.

---

### H1 (HIGH — strategy correctness) — strategies read a *partial* latest H1 bar between hour boundaries

**File:** `src/bot/loop.py:450-472` (`_derive_and_enrich_h1`)

```python
agg = df_m5.resample("1h", label="right", closed="right").agg(...).dropna()
```

`label="right"` + `closed="right"` mean an M5 bar with `close_time =
13:35` is placed in the H1 bin labeled `14:00` (the right boundary of
the interval `(13:00, 14:00]`). At M5 close 13:35 the 14:00 bin
contains 7 of the expected 12 M5 bars, but every OHLC slot is
populated so `dropna()` does NOT drop it. The strategy dispatcher
reads `df_h1.iloc[-1]` (`src/strategies/bb_reclaim.py:86`,
`src/strategies/ema_continuation.py:88`) — which is the **forming
14:00 H1 with 7 M5s of data**.

Reproduced empirically:

```
H1 bars (after M5 close at 13:35):
                           open  high  low  close  volume
close_time
2026-05-15 13:00:00+00:00   1.0   1.0  1.0    1.0      12
2026-05-15 14:00:00+00:00   1.0   1.0  1.0    1.0       7   ← partial
```

**Impact:** `_apply_indicators(df_h1)` then computes
`bb_width_norm_20_2`, `ema_slope_norm_50_10`, MACD etc. on a series
whose latest row is incomplete. Strategies that gate on H1
indicators (`bb_reclaim` checks `_safe(h1, "bb_width_norm_20_2") <
_BB_WIDTH_MAX`, `ema_continuation` similarly) see a value that
re-computes on every M5 close within the hour rather than being
stable per-H1. The result is non-deterministic firing: a setup might
clear the H1 gate at M5 close 13:35 and reject at 13:40 even though
nothing about the *last completed* H1 has changed.

The legacy autobot (`autobot-og-port-staging/autobot.py`) only
publishes H1 events on actual H1 closes — exactly because of this
issue.

`_update_regime` *does* gate H1 commits on
`m5_candle.close_time.minute == 0` (line 491) — so the regime engine
itself is correctly H1-aligned. The bug is that the dataframe
*handed to strategies* isn't.

**Suggested fix:** trim the forming H1 from `df_h1` before returning.
Either:

```python
# Drop the partial latest H1 unless the M5 close that triggered this
# was on an hour boundary.
if not agg.empty and m5_close_time.minute != 0:
    agg = agg.iloc[:-1]
```

…or pass `m5_candle` into `_derive_and_enrich_h1` and compute the
expected boundary explicitly.

Add a test that fires `_bar_close("GBPUSD", offset_min=mid_hour)` and
inspects `df_h1.iloc[-1]` — it should equal the last *full* H1, not
the in-progress one.

---

### H2 (HIGH — repo hygiene / process) — the entire Phase 8 implementation is uncommitted on `develop`, with no feature branch

**Files:** all of `src/bot/*.py` and `tests/unit/test_bot_*.py` show
as `??` or modified in `git status` on the local `develop`. Local
HEAD (`e977521`) is the Phase 7 cleanup. No `feature/bot` or
`feature/bot-loop` branch exists locally or on `origin`.

Every prior phase followed the pattern: `feature/<scope>` branch,
adversarial review, fix pass, merge with `--no-ff`. Phase 8 broke
that — there is no PR-like artefact to review against, no rollback
target if a regression lands, and `develop`'s working tree is in a
state where a `git pull --ff-only` from another machine would
conflict.

**Suggested fix:** before re-running this review, create
`feature/bot-loop` from `e977521`, commit the Phase 8 work there,
push, then run the review against the branch. Once the criticals
fix, merge with `--no-ff` per the established pattern.

**Why this is HIGH and not just LOW:** the divergence makes
collaborative review (and `git bisect`) impossible. A future
regression can't be isolated to "the Phase 8 commit" because there
is no Phase 8 commit — it's intermixed with whatever state
`develop` happens to be in.

---

### H3 (HIGH — counter asymmetry) — `_maybe_force_close_orders` never records a success, only failures

**File:** `src/bot/loop.py:546-583`

```python
def _maybe_force_close_orders(self) -> None:
    positions = self._collect_open_positions(...)
    try:
        orders = self._risk.positions_to_force_close(...)
    except Exception as exc:
        self._periodic_failures.record_failure(...)
        ...
        self._maybe_periodic_shutdown()
        return
    if not orders:
        return
    # iterate orders ...
```

Compare to `_maybe_reconcile` (line 511-513):

```python
try:
    self._with_inflight_tracked(self._reconcile_once)
    self._periodic_failures.record_success()    # ← success path resets the counter
except Exception as exc:
    self._periodic_failures.record_failure(...)
```

`_maybe_force_close_orders` only records failures. A run that
returns cleanly does NOT reset `_periodic_failures.consecutive`.
Pathological pattern:

- t=0: reconcile fails → consecutive=1
- t=5min: force-close runs cleanly → counter unchanged (still 1)
- t=10min: reconcile fails → consecutive=2
- t=15min: force-close runs cleanly → counter unchanged
- … after 5 reconcile failures across 50 minutes the threshold trips
  even though force-close worked fine on every interleaved bar.

The intent in the design comment (`constants.py:48-57`) is "Two
independent 5-strike counters". They're not independent — they
share `_periodic_failures` and only one of them resets it.

**Suggested fix:** either add `self._periodic_failures.record_success()`
after the iteration completes without raising, or (cleaner) split
into two separate FailureCounters (`_reconcile_failures` and
`_force_close_failures`) since they fail in disjoint code paths and
have distinct operational signatures.

---

### H4 (HIGH — STARTING never re-checks) — bot stuck in STARTING if `start_live()` fails to settle and `verify_subscriptions` is bypassed

**Files:** `src/bot/main.py:99-117`, `src/bot/loop.py:227-232`

`BotLoop.start()` unconditionally sets `self._state = BotState.NORMAL`
right after `self._feed.start_live()` returns — there's no check
that any subscription actually settled, that the LS subscriber
report `CONNECTED:*`, or that the first event ever fires. The
state machine moves to NORMAL based on a return-from-call, not on
any reachability signal.

`main.py` then runs `verify_subscriptions` *after* `start()` already
flipped the state. If `verify_subscriptions` fails, main calls
`bot.stop()` and exits 1 — but for a window of 10 seconds the bot's
state says NORMAL while the feed is partially or wholly broken.

The window is small and the main thread enforces the gate, so this
isn't a CRITICAL — but it conflicts with the documented contract
that NORMAL is reached "after :py:meth:`BotLoop.start` completes
(pre-flight ok, hydration done, ``start_live`` called)" (line 32-33
of `types.py`). Pre-flight subscription verification clearly isn't
included.

**Also:** the spec asks "what if preflight passes but first
BAR_CLOSE never arrives (e.g., market closed)? Bot stuck in STARTING
forever?" — the answer is no, because `start()` unconditionally
transitions. But that's actually a *different* failure mode: a bot
in NORMAL with no live events would happily sit there, the watchdog
in `FeedManager.watchdog_stale_pairs` will surface stale pairs
during market-open hours (Phase 7 follow-up M8), but BotLoop never
calls it. There's no production-side caller of
`watchdog_stale_pairs` and the BotLoop doesn't subscribe to the
watchdog.

**Suggested fix:** swap `start()` so the state transition happens
*after* `verify_subscriptions` succeeds:

```python
# main.py
bot.hydrate()
bot._feed.on_event(bot._handle_feed_event)
bot._feed.start_live()
sub_check = preflight.verify_subscriptions(...)
if not sub_check.ok:
    bot.stop(...)
    return 1
bot._state = BotState.NORMAL  # only now
```

Plus: wire the watchdog. Either main.py spawns a background thread
that polls `bot._feed.watchdog_stale_pairs()` every minute, or the
BotLoop calls it inline on `_handle_bar_close` and emits a stronger
alert when no live updates arrive across 2× the threshold during
market hours.

---

### M1 (MEDIUM — realized PnL never updates) — `AccountState.realized_pnl_today_r` is hardcoded to 0 forever

**File:** `src/bot/loop.py:208`

```python
self._realized_pnl_today_r: float = 0.0
```

Set once in `__init__`, never written to elsewhere. Every
`AccountState` constructed in `_evaluate_and_execute` (line 657-660)
passes `realized_pnl_today_r=0.0`.

**Impact:** the daily-DD circuit breaker
(`risk.rules.circuit_breakers.check_daily_dd`) reads
`account.realized_pnl_today_r`. With it permanently 0, the
daily-loss brake **never trips**. A bot that takes 5 losing trades
in a row would still be cleared by the risk layer to take a 6th.

The author flagged this in the source comment (line 206-208) as
"Phase 9+ will source from reconciliation events. Starts at 0." —
so it's a known v1 simplification. But:

1. The comment lives in `__init__`, not in `_evaluate_and_execute`
   or `AccountState` construction where a reader would notice.
2. The module's MODULE.md should call this out as a v1 limitation
   so an operator knows the daily-DD breaker is informational only.
3. `_reconcile_once` already has access to the reconciliation
   outcome which carries close events — a 10-line update inside
   the reconciliation handler would track this correctly.

**Suggested fix (minimum):** add a `# v1 limitation` block in
`MODULE.md` and a `logger.warning` on construction when
`_DEFAULT_BALANCE` and `realized_pnl_today_r=0.0` would silently
disable the breaker. **Suggested fix (better):** thread close-event
PnL through the reconcile path and into this counter.

---

### M2 (MEDIUM — counter reset on side-channel "success") — every successful event (incl. BAR_UPDATE no-ops) resets the event-failures counter

**File:** `src/bot/loop.py:336`

```python
def _handle_feed_event(self, event: FeedEvent) -> None:
    if self._state == BotState.SHUTTING_DOWN:
        return
    try:
        self._dispatch_event(event)
    except Exception as exc:
        ...
    self._event_failures.record_success()
```

`_dispatch_event` returns early for `BAR_UPDATE` (line 352-353) — no
work happens. But `_event_failures.record_success()` runs anyway,
zeroing the consecutive counter.

**Impact:** a bot taking 4 consecutive BAR_CLOSE failures could see
a BAR_UPDATE arrive in between and reset the counter, never tripping
the 5-strike threshold. BAR_UPDATEs fire frequently (every tick on a
live bar) so this is a realistic interleaving — in a busy bar the
counter would reset between every failed BAR_CLOSE.

**Suggested fix:** only call `record_success()` after a path that
*did* work, not after no-op event kinds. Either:

```python
def _handle_feed_event(self, event: FeedEvent) -> None:
    if self._state == BotState.SHUTTING_DOWN:
        return
    if event.kind in (FeedEventKind.BAR_UPDATE, FeedEventKind.FEED_STALE,
                      FeedEventKind.FEED_RESUMED, FeedEventKind.GAP_FILLED):
        # Status events / no-op kinds don't represent "successful work".
        try:
            self._dispatch_event(event)
            return
        except Exception as exc:
            self._event_failures.record_failure(...)
            ...
            return
    # BAR_CLOSE — the only kind that does real work.
    try:
        self._dispatch_event(event)
    except Exception as exc:
        self._event_failures.record_failure(...)
        ...
        return
    self._event_failures.record_success()
```

Or simpler: only reset on a `BAR_CLOSE` whose pipeline ran to
completion.

---

### M3 (MEDIUM — per-order force-close failures are invisible to the threshold) — broker-IO failures during force-close don't count

**File:** `src/bot/loop.py:570-582`

```python
for order in orders:
    try:
        self._with_inflight_tracked(
            lambda o=order: self._execute_force_close(o)
        )
    except Exception:
        logger.exception(...)
```

Per-order failures are isolated (one bad close shouldn't block the
others) but they're not counted anywhere — not `_periodic_failures`,
not `_event_failures`. So a broker outage that fails 100% of
force-close attempts would log noisily but never trip the bot.

The author's comment justifies this (line 576-579): "one failed
close should not block the others (and shouldn't count as a
*periodic* failure either, since the close is broker-IO)". Fine in
principle — but the bot then has *no* mechanism to surface a
sustained close-failure pattern. Operator would see ERROR-level
logs but no shutdown / state change / alert.

**Suggested fix:** maintain a separate `_force_close_failures` counter
with a generous threshold (e.g. 20) so a few transient failures don't
kill the bot but a sustained outage does. Or surface the count in a
new `bot.force_close_failure_count()` property that an external
ops watchdog can read.

---

### M4 (MEDIUM — open-from-signal failures are also silent) — `_evaluate_and_execute` swallows Executor exceptions

**File:** `src/bot/loop.py:683-691`

```python
try:
    self._with_inflight_tracked(
        lambda: self._executor.open_from_signal(signal)
    )
except Exception:
    logger.exception("Executor.open_from_signal raised for ...")
```

Same pattern as M3 — exception logged, not counted. A persistent
broker failure on opens (e.g., bad credentials, market closed,
margin exhausted) would just spam logs without ever triggering a
shutdown.

Phase 6 makes the Executor robust to many broker-side failures
(REJECTED status etc.) but a network-level RuntimeError lands here.

**Suggested fix:** same as M3 — either count toward an existing
counter or surface a dedicated metric.

---

### M5 (MEDIUM — test mislabel / counter narrative) — `test_event_failure_counter_increments_on_exception` actually verifies that the EVENT counter does NOT increment

**File:** `tests/unit/test_bot_loop.py:446-459`

The test name says "event failure counter increments". The body
sets up a failing `risk.positions_to_force_close` and asserts:

```python
assert bot.event_failures.consecutive == 0
assert bot.periodic_failures.consecutive == 1
```

…which is the opposite of what the name implies. The inline comment
acknowledges the confusion ("We want to count this against the EVENT
counter…") but doesn't rename the test. A future reader would
expect this test to lock in a specific counter behaviour and be
confused by the assertion.

**Suggested fix:** rename to `test_periodic_failure_path_does_not_bump_event_counter`
and clean up the comment.

---

### M6 (MEDIUM — STARTING-state events are processed) — events arriving in STARTING are not gated

**File:** `src/bot/loop.py:313-316`

```python
def _handle_feed_event(self, event: FeedEvent) -> None:
    if self._state == BotState.SHUTTING_DOWN:
        return
    ...
```

Only `SHUTTING_DOWN` short-circuits. In `STARTING` the handler
proceeds through the pipeline. Practically this is benign because
`start()` registers the callback *after* hydration (so no live
events arrive during hydration), and the first event arrives only
after `start_live()` succeeded. But there's no defensive guard if
the call order is ever rearranged (e.g., a test or a future
refactor inverts `on_event` registration vs. `start_live`).

**Suggested fix:** also short-circuit `STARTING`:

```python
if self._state in (BotState.STARTING, BotState.SHUTTING_DOWN):
    return
```

Or leave it but add a docstring assertion that the call order is
load-bearing.

---

### L1 (LOW — premature state set) — `_state = BotState.NORMAL` happens before subscription verification

(Covered by H4.) Cross-listed here so the LOW count is honest.

---

### L2 (LOW — start_live ordering) — `start()` registers the callback BEFORE opening the LS subscription, but doesn't check that registration matters

**File:** `src/bot/loop.py:227-232`

```python
def start(self) -> None:
    self._feed.on_event(self._handle_feed_event)
    self._feed.start_live()
    self._state = BotState.NORMAL
```

Order is correct (register first so the first LS payload after
`start_live` is captured). But it would be one line to make this
robust to a hypothetical refactor that splits `start_live` into
"connect" + "subscribe": e.g., guarantee callback registration is
idempotent via a flag.

Trivial — note for the maintainer.

---

### L3 (LOW — comment-vs-code drift) — `loop.py` docstring claims hydration runs gap-fill BAR_CLOSE *before* `GAP_FILLED`, but the comment in `_dispatch_event` says it explicitly

**File:** `src/bot/loop.py:346-350`

Just a reading-comprehension nit: the docstring at the top of the
class talks about the state machine; the `_dispatch_event` body has
an inline comment about per-bar BAR_CLOSE events arriving before
GAP_FILLED. Either centralise the ordering doc in `types.py` (next
to the FeedEventKind enum) or remove the inline comment as
redundant — duplicating in both places risks them drifting.

---

### L4 (LOW — _LateSubscriber is a class with one property) — replace with a `types.SimpleNamespace`-style closure

**File:** `src/bot/main.py:239-244`

```python
class _LateSubscriber:
    @property
    def subscribed_pairs(self) -> tuple[str, ...]:
        return captured[0].subscribed_pairs if captured else ()
```

A nested class to expose one property is heavier than necessary.
A function that returns a closure-bound object would do — but this
is genuine taste, not a correctness issue.

---

### L5 (LOW — signal handler enum lookup) — `signum in iter(signal.Signals)` is O(N)

**File:** `src/bot/main.py:91-93`

```python
signal.Signals(signum).name if signum in iter(signal.Signals) else signum
```

`iter(signal.Signals)` walks the enum once per signal. A
try/except KeyError is cheaper and clearer:

```python
try:
    name = signal.Signals(signum).name
except ValueError:
    name = str(signum)
```

Trivial.

---

## State-machine walkthrough (for the record)

| Scenario | Path | Verdict |
|---------|------|---------|
| `STARTING → NORMAL` after `start()` returns | always | ✓ (but see H4: bot moves to NORMAL even if `verify_subscriptions` will fail) |
| `NORMAL → STALE` on FEED_STALE | `_dispatch_event` line 340 | ✓ tested |
| `STALE → RESUMING` on FEED_RESUMED | line 343 | ✓ tested |
| `RESUMING → NORMAL` on GAP_FILLED | line 346 | ✓ tested |
| `RESUMING → NORMAL` on first live BAR_CLOSE (no gap-fill case) | line 406-407 (`is_gap_fill = False`) | ✓ tested |
| Gap-fill BAR_CLOSE during RESUMING — stays RESUMING | line 406 check inverts | ✓ tested |
| FEED_STALE arriving during RESUMING | `_transition` would set STALE; this is the documented "stale during recovery" path | not tested — would benefit from a test |
| `* → SHUTTING_DOWN` on SIGTERM | `request_shutdown` checks idempotent on re-entry | ✓ |
| Race: SIGTERM during BAR_CLOSE processing | signal handler runs on main thread; LS callback continues on its own thread to completion (no preemption) | ✓ |
| Test coverage `STALE` while already STALE | tested indirectly | ✓ |

## Test-quality summary

- **`_FakeIGClient.close_position` doesn't inspect direction** — masks C1.
- **`_FakeRiskGuard` doesn't touch a regime engine** — masks C2.
- **No mid-hour test for `_derive_and_enrich_h1`** — masks H1.
- **`test_event_failure_counter_increments_on_exception` name lies** — see M5.
- **Drain test (`test_stop_drains_inflight_before_disconnect`) is solid** — exercises real `Condition.wait`/`notify` semantics with two threads.
- **Pre-flight tests are well-isolated** with injected `sleep`/`clock`.
- **Signal-handler behaviour not tested** — acceptable for v1 since the signal module isn't easily unit-testable.

---

## Final recommendation

**RE-WORK.**

C1 and C2 are direct money-loss / risk-rule-disablement paths that
manifest only in production. H1 affects strategy correctness for
every signal that gates on H1 indicators (`bb_reclaim`,
`ema_continuation`). H2 is a process hygiene issue — there's no
feature branch / PR for this work, so a regression has no rollback
target.

The good news: every finding has a small, well-bounded fix. The
state machine itself is well-designed; the failure-counter
architecture is sound; the shutdown drain is correctly implemented
with `time.monotonic` and `Condition.wait`. The C-class issues are
both *wiring* bugs (passing the wrong object / pre-inverted enum
value), not architectural mistakes.

Suggested sequence:

1. Create `feature/bot-loop` from `e977521`, commit the current work
   there.
2. Fix C1 (force-close direction). Add the regression test that
   pins `close_calls[0].position_direction`.
3. Fix C2 (regime engine wiring). Add a test asserting
   `risk_guard.engine is bot._pair_state["GBPUSD"].regime_engine`.
4. Fix H1 (drop partial H1 from resample output).
5. Tackle H3, H4, M2 in the same pass — small.
6. M1, M3, M4 can land as follow-ups; M5/M6/L* can batch.

Once C1/C2/H1 are fixed and pinned by tests, this is a quick path to
APPROVE FOR MERGE.

---

# Addendum — re-review pass on `feature/bot-loop` @ `a8766d6`

- **Branch reviewed:** `feature/bot-loop`
  - `63045b8` — WIP snapshot of the original (buggy) state
  - `a8766d6` — fix pass under review
- **Base:** `develop` (`e977521` after Phase 7 cleanup)
- **Date:** 2026-05-15
- **Reviewer:** AutoBot-OG (read-only verification pass)
- **Test status:** `717 passed in 3.96s` — full suite green and
  matches the commit claim. `bot.loop` subset is 33 tests (was 20;
  13 new tests cover C1×2, C2×4, H1×2, H3, M2, M6, plus mark_ready
  lifecycle).

## Disposition of original findings

| ID | Status | Note |
|----|--------|------|
| **C1** Force-close wrong direction | **FIXED** | `_execute_force_close` now passes `position.direction` as-is. Two regression tests pin both BULLISH→"BUY" and BEARISH→"SELL". `_FakeIGClient.close_position` now asserts on `position_direction`. |
| **C2** RiskGuard reads stale engine | **FIXED** | `RiskGuard` accepts `engine_for_pair` callable; `BotLoop` accepts `regime_engines` dict; `bot.main._build_runtime` constructs one map and hands the SAME instances to both. Identity test pins the wiring. Phase 4 single-engine callers unchanged. |
| **H1** Partial trailing H1 bar | **FIXED** | `_derive_and_enrich_h1` accepts `m5_close_time` and trims the last bin when `minute != 0`. Two tests pin mid-hour trim and on-boundary keep. |
| **H2** No feature branch | **RESOLVED** | `feature/bot-loop` exists at `a8766d6` with proper commits. |
| **H3** Force-close never records success | **FIXED** | `_maybe_force_close_orders` now calls `record_success()` on both the no-op and the post-iteration paths. Regression test pins it. |
| **H4** Premature NORMAL on `start()` | **FIXED** | `start()` keeps state at `STARTING`; new `mark_ready()` flips to `NORMAL`; `bot.main.main` calls it after `verify_subscriptions`. Three new tests cover the lifecycle. |
| **M1** Daily-DD permanently disabled | **DOCUMENTED** | `MODULE.md` v1-simplifications section now spells out the limitation, and `BotLoop.__init__` logs a WARNING at construction. Behaviour unchanged — deferred to Phase 9+ per design. |
| **M2** Event counter resets on no-op kinds | **FIXED** | Only `BAR_CLOSE` resets the counter now. Regression test pins both `BAR_UPDATE` and `FEED_STALE` as non-resetters. |
| **M3** Per-order force-close failures invisible | **DEFERRED** | Author chose not to add a counter — comment in source justifies as "broker-IO". See §"Deferred items" below. |
| **M4** Open-from-signal failures silent | **DEFERRED** | Same reasoning as M3. |
| **M5** Test name mismatch | **FIXED** | Renamed to `test_periodic_failure_path_does_not_bump_event_counter` with corrected comment. |
| **M6** STARTING events not gated | **FIXED** | `_handle_feed_event` now short-circuits both `STARTING` and `SHUTTING_DOWN`. Regression test pins it. |
| **L1** Premature state set | **FIXED** | Folded into H4. |
| **L2** start() ordering robustness | **DEFERRED** | Trivial; not blocking. |
| **L3** Comment-vs-code drift | **DEFERRED** | Trivial. |
| **L4** `_LateSubscriber` style | **FIXED** | Renamed `_LateSubscriberView`, added `__slots__`. |
| **L5** Signal handler enum lookup | **DEFERRED** | Trivial. |

## Verifications

### C1 — force-close direction (both polarities through both layers)

```
BULLISH (BUY position):
  BotLoop sets request.position_direction = BUY
  Wrapper sends to IG: direction = SELL    ← correct to close a BUY
BEARISH (SELL position):
  BotLoop sets request.position_direction = SELL
  Wrapper sends to IG: direction = BUY     ← correct to close a SELL
```

Walk-through:

- `src/bot/loop.py:736-744` — `own_direction = "BUY" if BULLISH else "SELL"`.
- `src/feed/ig_rest/positions.py:89` — wrapper computes
  `opposite = "SELL" if close.position_direction == "BUY" else "BUY"`.
- Combined: wire-level direction is always the opposite of the
  position's, which is the IG semantic to close.

The mask in the original test (`_FakeIGClient.close_position`
accepting any direction silently) is gone — the fake now asserts
the value is `"BUY"`/`"SELL"` and records it for the regression
tests:

```python
# tests/unit/test_bot_loop.py:128-132
assert request.position_direction in ("BUY", "SELL"), (
    f"CloseRequest.position_direction must be BUY/SELL; "
    f"got {request.position_direction!r}"
)
```

Two regression tests pin the polarity:
`test_force_close_passes_position_own_direction_bullish` and
`test_force_close_passes_position_own_direction_bearish`.

### C2 — engine sharing through `engine_for_pair`

The fix introduces a two-mode constructor:

- Legacy single-engine form: `RiskGuard(engine=eng)` — Phase 4 tests
  unchanged (verified: `test_risk_guard_falls_back_to_single_engine_when_callable_not_provided` and 90/90 risk tests pass).
- Multi-pair form: `RiskGuard(engine_for_pair=lambda p: engines[p])`
  — `BotLoop` constructor accepts `regime_engines` dict, `bot.main`
  builds it once and hands the dict to BOTH:

```python
# bot/main.py
regime_engines = {p: RegimeEngine() for p in config.pairs}
risk_guard = RiskGuard(engine_for_pair=lambda pair: regime_engines[pair])
bot = BotLoop(..., regime_engines=regime_engines, ...)
```

Identity check (regression test
`test_bot_loop_and_risk_guard_share_engine_instance_identity`):

```python
assert bot.regime_engine_for("GBPUSD") is engines["GBPUSD"]
assert pieces["risk"]._engine_for_pair("GBPUSD") is engines["GBPUSD"]
```

Routing check (`test_risk_guard_routes_to_correct_pair_engine`)
exercises `_evaluate_and_execute` with a `Signal(pair="GBPUSD")`
and asserts the fake recorded `("GBPUSD", …)` as the routed pair.

The defensive `_resolve_engine` (`src/risk/guard.py:111-126`) raises
on `None` from the callable; `engine` property raises a friendly
error when only the callable form was wired. Both rejection paths
have tests.

**`engine_for_pair("UNKNOWN_PAIR")` behaviour** — the lambda
constructed by `bot.main` does `regime_engines[pair]` which raises
`KeyError`. `_resolve_engine` doesn't catch it (it only checks for
`None`), so the KeyError propagates. Callers:

- `allow_entry`: candidate.pair is always one the BotLoop generated
  signals for, so the pair is guaranteed in the engine map. No risk.
- `positions_to_force_close`: iterates pairs from positions handed
  in by `_collect_open_positions`, which reads `PositionManager.all()`.
  If a stale position from a prior session has a pair removed from
  `config.pairs`, KeyError propagates. The BotLoop's outer try/except
  catches and bumps `_periodic_failures`. After 5 strikes the bot
  shuts down — same as any other persistent periodic failure.

I'd flag this as a LOW-severity edge case (see §"New findings" below).

### H1 — partial-bar trim

Verified empirically:

```
A: m5_close=13:35 (mid-hour). H1 bars after trim:
   [Timestamp('2026-05-15 13:00:00+0000', tz='UTC')]    ← partial 14:00 dropped
B: m5_close=13:00 (boundary). H1 bars after trim:
   [Timestamp('2026-05-15 13:00:00+0000', tz='UTC')]    ← just-closed 13:00 kept
```

Code (`src/bot/loop.py:570-580`):

```python
if m5_close_time.minute != 0:
    # The latest bin is the in-progress H1 — trim it.
    agg = agg.iloc[:-1]
```

**Edge: m5_close exactly on hour.** At `minute=0` the M5 bar that
just closed (e.g. 12:55→13:00) lands in the 13:00 H1 bin AS THE
12th contribution — that bin is now complete. The trim condition
correctly evaluates `False` and the bar is kept. ✓

The test seam `_h1_for_test(pair, m5_close_time=…)` exposes the
exact computation without firing a full event.

**Performance:** the trim is `agg.iloc[:-1]` on an already-computed
DataFrame. Cost is one slice (constant time, returns a view).
Negligible per BAR_CLOSE.

### H3 — both reconcile and force-close reset symmetrically

```python
# _maybe_reconcile (already correct pre-fix):
self._periodic_failures.record_success()  # on clean reconcile

# _maybe_force_close_orders (fixed):
if not orders:
    self._periodic_failures.record_success()  # no-op path
    return
...
for order in orders:
    try: ...
    except Exception: logger.exception(...)
self._periodic_failures.record_success()  # post-iteration path
```

Walk-through against the pathological pattern from the original
review:

| t | event | reconcile result | force-close result | periodic counter |
|---|-------|------------------|--------------------|-------------------|
| 0 | BAR_CLOSE | fail (10-min gate elapses) | clean | reconcile=1, force_close resets to 0 |
| 5 | BAR_CLOSE | not run (gate) | clean | 0 |
| 10 | BAR_CLOSE | fail | clean | 1 → 0 |

Before the fix the column would be `1, 1, 2` — would trip after 5
× 10-min cycles. After the fix it never accumulates if force-close
keeps working. Regression test
`test_periodic_failures_reset_on_clean_force_close` pins this.

### H4 — STARTING → mark_ready → NORMAL lifecycle

`main.py` flow:

```python
bot.hydrate()
bot.start()                        # state stays STARTING
sub_check = verify_subscriptions(...)
if not sub_check.ok:
    bot.stop(...)                  # request_shutdown → SHUTTING_DOWN
    return 1
bot.mark_ready()                   # state → NORMAL
bot.shutdown_event().wait()
```

**Failed-subscription path:** `verify_subscriptions` returns
`ok=False`. `bot.stop()` calls `request_shutdown()` which sets
state to `SHUTTING_DOWN` regardless of prior state. main returns 1.
No events were processed during STARTING (handler short-circuits).
Clean.

**Signal-during-STARTING path:** SIGTERM arrives between `start()`
and `mark_ready()`. The handler calls `request_shutdown()` →
state=`SHUTTING_DOWN`. Main proceeds to call `mark_ready()` which
no-ops because `state != STARTING`. Then `shutdown_event().wait()`
returns immediately (event was set). Drain runs. Returns 0 or 2.
Verified mark_ready is idempotent
(`test_mark_ready_is_idempotent_and_no_op_after_transition`).

### M2, M5, M6 — bundled MEDIUMs

- **M2** event counter — `is_real_work = event.kind is FeedEventKind.BAR_CLOSE`
  gates the success call. Regression test fires a BAR_UPDATE and a
  FEED_STALE after a prior failure-record and asserts the counter
  stays at 1.
- **M5** test rename — `test_event_failure_counter_increments_on_exception`
  → `test_periodic_failure_path_does_not_bump_event_counter`.
  Body unchanged but the name/comment now match the assertion.
- **M6** STARTING-state events — `_handle_feed_event` first line is
  now `if self._state in (BotState.STARTING, BotState.SHUTTING_DOWN): return`.
  Regression test pins it.

### Test-fake hardening

Both fakes the original review flagged as masks are now load-bearing:

- `_FakeIGClient.close_position` asserts direction (tests/unit/test_bot_loop.py:128-132).
- `_FakeRiskGuard` now mirrors the real surface — `allow_entry` and
  `positions_to_force_close` actually call `engine.is_live()` and
  `engine.get_recent_emissions(...)`, with a per-call `(pair,
  is_live, recent_count)` record so routing assertions are direct
  (`observed_engine_lookups`).

The test-design lesson is now codified in fake docstrings ("The
existence of this assertion (rather than blanket acceptance) is
the lesson from C1.").

## New issues introduced by the fixes

Two LOW-severity edges; neither blocks merge.

### N1 (LOW — error surface) — `engine_for_pair` raises KeyError on stale-pair positions; bot trips the periodic counter instead of recovering

**File:** `src/risk/guard.py:111-124`, `src/bot/main.py:181-185`

`bot.main` wires `engine_for_pair=lambda pair: regime_engines[pair]`.
If a `PositionManager`-loaded position carries a pair removed from
`config.pairs` (operator removed a pair from `BOT_PAIRS`), the lambda
raises `KeyError`. `_resolve_engine` doesn't catch it. The
`positions_to_force_close` call inside `_maybe_force_close_orders`
raises, the outer try/except converts to `_periodic_failures.record_failure`,
and after 5 consecutive BAR_CLOSEs the bot shuts down with exit 2.

This is *not* wrong behaviour — a stale position the bot can't act
on IS a real problem — but the user-facing log says "periodic seam
failed" rather than the cleaner "stale position for unconfigured
pair". A defensive `try/except KeyError` in the lambda (or in
`_resolve_engine`) that logs the pair name once would be a small
quality-of-life improvement.

**Suggested fix:** swallow KeyError in
`bot.main._build_runtime`'s lambda and log a single warning:

```python
def _engine_for_pair(pair: str) -> RegimeEngine:
    eng = regime_engines.get(pair)
    if eng is None:
        raise RuntimeError(
            f"No regime engine for pair {pair!r} — orphaned position "
            f"from prior session? Remove via PositionManager or add "
            f"the pair back to BOT_PAIRS."
        )
    return eng
```

Not blocking.

### N2 (LOW — log spam) — `BotLoop.__init__` always logs WARNING about M1, even in tests

**File:** `src/bot/loop.py:227-237`

The realized-PnL warning fires on every construction. In a busy test
suite (33 BotLoop builds) the test log captures 33 copies. Pytest
captures stderr by default so this doesn't visibly leak — but ops
dashboards that key on WARNING counts will see a 1-per-startup
heartbeat that's actually a documentation note, not an alert.

**Suggested fix:** demote to `logger.info` (it's a v1 limitation
note, not an actionable warning), or gate behind an env var so
tests can suppress.

Not blocking.

## Deferred items the fix pass did NOT include

The author deferred M3, M4, L2, L3, L5 to follow-up commits. My read:

- **M3 / M4** (silent broker-IO failures on force-close and open) —
  defensible deferral. The source comments document the design
  choice. v1 operates on a single-pair, modest-cadence bot; a
  systematic broker outage will surface via reconcile failures
  (which DO count) or the watchdog. Defer.
- **L2, L3, L5** — all trivial. Defer.
- **A4 from the Phase 7 review** (parallel gap-fill) was also
  deferred. Not in scope for Phase 8.

No deferred item rises to the level that blocks merge.

## Final recommendation

**APPROVE FOR MERGE.**

All five HIGH/CRITICAL items (C1, C2, H1, H3, H4) are fixed with
regression tests that exercise the bug paths directly. M1 is
documented as a known v1 limitation with a runtime WARNING. M2 is
fixed. M5 / M6 / L1 / L4 are cleaned up. Test fakes are now
contract-checking rather than smoke-testing — the lessons from C1
and C2 are codified in the fake assertions.

The two new LOW findings (N1: KeyError on stale-pair positions; N2:
WARNING spam) can land in a follow-up commit without blocking the
merge.

Suite stable at 717 (704 + 13 new). Phase 4 single-engine RiskGuard
callers unchanged (90/90 risk tests green). Ready to land on
`develop`.
