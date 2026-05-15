# Adversarial review — `feature/alerts-integration` commit 2b (alerts wired into Phase 6/7/8)

**Branch:** `feature/alerts-integration`
**Base:** `develop` after commit 2a merge (i.e. with `9f68428` already in)
**Working-tree diff stat vs. `develop`:** 6 files, +1168 / -16
**Review scope:** alerts wired into `bot/loop.py`, `bot/main.py`, `execution/executor.py`, plus 3 updated test files.
**Test suite:** **840 passed, 0 warnings** (matches plan target).

---

## Headline

Integration is in good shape. The plan's locked design decisions are
faithfully implemented: BotLoop owns the single alerter dependency,
`bot.main` constructs once via env, the FEED_STALE/RESUMED transition
is state-first-then-alert, TRADE_OPENED fires only after
`position_manager.upsert` succeeds, AMEND_FAILED fires only after
retry exhaustion, force-close emits TRADE_CLOSED + populates the
deal log + clears it on the next reconciliation, the four
operator-actionable reconciliation kinds translate to alerts and the
INFO-level kinds are correctly suppressed. CRITICAL bypass for
FAILURE_THRESHOLD_TRIPPED and crashed-SHUTDOWN is wired correctly.

Findings are mostly MEDIUM (observability gaps, an unbounded-growth
defect in `_recent_closes`, and a test that asserts the wrong
ordering invariant). **No CRITICAL.** One HIGH-shaped issue around
silent persist-after-broker-accept on the amend path. A few LOW
polish items.

P1 (process): the diff is in the working tree, not yet committed.
Same shape as the Phase 8 and commit-2a P1s — the commit boundary
matters for ops rollback and bisect.

---

## Findings

### H1 (HIGH — silent state divergence on amend persist failure) — `apply_amend` upsert has no emergency-rollback or alert path

**File:** `src/execution/executor.py:333-342`

```python
is_be_move = amend.reason == "be_move_at_1r"
updated = position.with_sl_amend(
    new_sl_price=amend.new_sl_price,
    at_utc=self._clock(),
    reason=amend.reason,
    deal_id_or_reference=confirmation.deal_id or confirmation.deal_reference,
    be_moved=True if is_be_move else None,
    trail_active=True if is_be_move else None,
)
self._positions.upsert(updated)            # <-- can raise; not isolated
return AmendResult(success=True, ...)
```

`open_from_signal` has the M2-fix wrap (`try/except` on
`self._positions.upsert(position)` with an emergency-close call to
the broker plus re-raise so the caller sees the root cause); the
parallel amend path has no equivalent. If the broker has just
accepted the amend and `upsert` then raises (disk full, JSON
encode failure on a transient float NaN, permission flip mid-fsync),
the outcome is:

- Broker SL: **new value (correct)**.
- Local SL: **old value (stale)**.
- Telegram: **silent** — the executor's `_emit_amend_failed` only
  fires inside the `else` branch of the retry `for/else` (i.e. only
  on broker rejection), not on a post-success persist failure.
- Next reconciliation pass (up to 10 minutes later) detects the
  drift as `SL_UPDATED_FROM_BROKER` (INFO severity) and adopts the
  broker value, which `_dispatch_reconciliation_alerts` **suppresses**
  from Telegram. So the operator never sees a Telegram alert for the
  persist failure — only a stack trace in the local log.

Severity is HIGH because:

1. It's a **silent state divergence** between broker and local state
   that survives until the next reconciliation cadence.
2. During that window, the BAR_CLOSE pipeline's SL evaluator
   (`_run_sl_evaluation`, loop.py:993-1019) sees a stale local SL
   and may compute an unnecessary further amend, triggering a second
   broker call. If that one also persist-fails, the divergence
   compounds.
3. The fix that already exists for `open_from_signal` (M2 from the
   2026-05-14 review) was explicitly designed to "fail loudly" on
   the broker-accepted-but-local-failed pattern. The same reasoning
   applies here.

**Suggested fix:**

```python
try:
    self._positions.upsert(updated)
except Exception as upsert_exc:
    logger.critical(
        "Persist failed after broker accepted amend: "
        "deal_id=%s, new_sl=%.5f, exception=%r. Broker has the "
        "new SL; local state has the old. Next reconciliation "
        "will adopt broker value.",
        amend.deal_id, amend.new_sl_price, upsert_exc,
        exc_info=True,
    )
    self._emit_amend_failed(
        position=position, amend=amend,
        reason=f"persist_failed_after_broker_accept:{upsert_exc!r}",
    )
    raise
```

The choice not to attempt an emergency *unwind* (re-amending the
broker back to the old SL) matches the existing open-path
"emergency close" pattern's bias: when local and broker diverge,
prefer alerting + raising over a second broker call that may also
fail. The reconciliation pass will adopt the broker value cleanly.

The AMEND_FAILED alert path under this branch uses a distinct
`reason` string so the operator can distinguish "broker rejected"
(action: investigate market state) from "persist failed after broker
accepted" (action: check disk / file permissions).

---

### M1 (MEDIUM — `_recent_closes` grows unbounded for force-closed deal_ids)

**Files:** `src/bot/loop.py:878-883` (write), `:773-780` (the only pop).

```python
# _execute_force_close (write)
self._positions.remove(position.deal_id)
self._recent_closes[position.deal_id] = {
    "pair": position.pair,
    "reason": order.reason,
    "closed_at_utc": self._clock().isoformat(),
    "source": "force_close",
}
self._send_alert(...)
```

```python
# _reconcile_once (the only pop)
for deal_id in outcome.actions.remove_deal_ids:
    self._positions.remove(deal_id)
    self._recent_closes.pop(deal_id, None)
```

`outcome.actions.remove_deal_ids` is populated only from
`POSITION_CLOSED` events
(`src/execution/reconciliation.py:136-137`). `POSITION_CLOSED` only
fires when:

1. `_handle_missing_broker` sees `local_by_id` still contains the
   deal_id (broker payload missing it, local has it), AND
2. The deal log lookup returns a non-empty entry.

But `_execute_force_close` removes the position locally **before**
the next reconciliation runs. So `local_by_id` no longer contains
the deal_id, the `for deal_id, local in local_by_id.items()` loop
skips it, `POSITION_CLOSED` never fires for it, and the
`_recent_closes` entry is never popped.

**Empirical impact:** at, say, 10 force-closes per UTC day (EOD
flatten of 2 pairs + a handful of regime-transition closes), this
adds ~10 dict entries per day, each carrying a small payload
dict (~250 bytes). 30-day uptime → ~75 KB. 1-year uptime →
~900 KB. Not a memory exhaustion risk, but unbounded growth in a
critical-path dict that's iterated/queried on every reconciliation
pass (every BAR_CLOSE after the 10-minute window). At
~280 dict entries the iteration is still negligible, but the
"unbounded growth" signal in a long-running observability dict
is the kind of latent bug that bites a 6-month-uptime deployment.

The docstring at loop.py:867-883 acknowledges the entry is "not
strictly needed for alerting" — it's a race-defence measure for the
case where `self._positions.remove(...)` raises. If that race
doesn't fire (the common case), the entry is dead and never cleaned
up.

**Suggested fix (any of three):**

1. Pop immediately on the success path:
   ```python
   self._send_alert(...)
   # The entry was a race-defence measure; if we reached the alert
   # path the race didn't happen — drop the entry so it doesn't
   # accumulate.
   self._recent_closes.pop(position.deal_id, None)
   ```
   (Loses the race protection — see option 3.)

2. TTL prune in `_maybe_reconcile`:
   ```python
   cutoff = now - timedelta(hours=24)
   stale = [
       did for did, entry in self._recent_closes.items()
       if datetime.fromisoformat(entry["closed_at_utc"]) < cutoff
   ]
   for did in stale:
       self._recent_closes.pop(did)
   ```

3. **Recommended:** keep the race defence but bound the window
   to the next reconciliation cycle. After one reconciliation pass
   has run since the close, the race-defence purpose is satisfied;
   drop any entry that's >2× the reconciliation interval old. This
   preserves the defence and prunes the residue.

Severity is MEDIUM not LOW because the "unbounded" property is the
defect, not the absolute bytes-per-day rate.

---

### M2 (MEDIUM — STALE_POSITION and SL_DRIFT_LARGE misclassified as INFO in dispatch comment)

**Files:** `src/bot/loop.py:738-743` (the docstring/comment),
`src/execution/reconciliation.py:144-146, 295-297`
(actual severities).

```python
# Dispatch alerts only after applying actions so the
# operator sees the same view the bot acted on. INFO-level
# reconciliation events (OK_NO_OP, SL_UPDATED_FROM_BROKER,
# STALE_POSITION, SL_DRIFT_LARGE) are suppressed; only the
# operator-actionable kinds translate to alerts.
```

But `STALE_POSITION` and `SL_DRIFT_LARGE` are emitted at
**WARNING** severity, not INFO. Verified at
`reconciliation.py:146`:

```python
severity=ReconciliationSeverity.WARNING,
kind=ReconciliationKind.STALE_POSITION,
```

and `reconciliation.py:295-297`:

```python
if drift_pips > EXECUTION_SL_DRIFT_WARN_PIPS:
    severity = ReconciliationSeverity.WARNING
    kind = ReconciliationKind.SL_DRIFT_LARGE
```

The code behaviour matches what the locked plan says
("BROKER_ORPHAN / MISSING_LOCAL_KEPT / MANUAL_SL_MOVE only for
WARNING/ALERT severity reconciliation events; INFO suppressed"
— the suppression is by hand-picked kind, not by severity-class).
But the comment misclassifies STALE_POSITION and SL_DRIFT_LARGE as
"INFO-level", which is wrong and will mislead the next person
maintaining this dispatch.

**Suggested fix:** rewrite the comment to describe the actual
policy:

```python
# Dispatch alerts for the four operator-actionable kinds only:
# POSITION_CLOSED, BROKER_ORPHAN, MISSING_LOCAL_KEPT, MANUAL_SL_MOVE.
# Suppressed: OK_NO_OP and SL_UPDATED_FROM_BROKER (INFO — local-only
# bookkeeping), STALE_POSITION and SL_DRIFT_LARGE (WARNING but
# re-fires every BAR_CLOSE for the same position; would generate
# noise even with coalescing). AMEND_FAILED is emitted by the
# Executor at the call site (with broker context); the reconciler
# does not currently set this kind for v1.
```

Severity: MEDIUM because the inline `_reconciliation_event_to_alert`
docstring (loop.py:1252-1256) carries the same misclassification in
a slightly different form. Two comment locations to fix; behaviour is
correct.

---

### M3 (MEDIUM — STARTUP alert does not surface degraded-mode pairs)

**File:** `src/bot/loop.py:258-279` (`hydrate()`) and
`src/bot/main.py:362-388` (`_emit_startup_alert`).

```python
def hydrate(self) -> dict:
    report = self._feed.hydrate()
    ...
    if not report.ok:
        raise RuntimeError(...)
    return {
        "cached_bars": sum(p.cached_bars for p in report.per_pair),
        "rest_bars": sum(p.rest_bars for p in report.per_pair),
    }
```

If `report.ok` is true but one or more pairs hydrated in degraded
mode (cache-only, no REST top-up because the REST call failed but
the cache was within the freshness window), the STARTUP alert reads
"Hydration: X cached, Y REST" with no signal that some pairs are
running on stale-ish cache. The operator can't tell from the
Telegram message that they should investigate.

The hydration report carries `degraded_pairs` (loop.py:270) and the
per-pair report likely carries a `degraded: bool` (verified by the
test stub at `tests/unit/test_bot_loop.py:1269-1277` which includes
the `degraded_pairs` field). Surfacing this in the STARTUP alert
gives operators the heads-up they need.

**Suggested fix:** thread the degraded-pair count into the summary:

```python
return {
    "cached_bars": sum(p.cached_bars for p in report.per_pair),
    "rest_bars": sum(p.rest_bars for p in report.per_pair),
    "degraded_pairs": list(report.degraded_pairs),
}
```

And in `_emit_startup_alert`:

```python
hydr_line = f"Hydration: {cached} cached, {rest} REST"
if hydration_summary.get("degraded_pairs"):
    pairs_csv = ", ".join(hydration_summary["degraded_pairs"])
    hydr_line += f" (degraded: {pairs_csv})"
```

Severity: MEDIUM because operators rely on STARTUP for the first
"is this bot healthy?" signal; hiding degraded-mode there breaks
that contract.

---

### M4 (MEDIUM — FEED_STALE ordering test does not actually verify state-before-alert)

**File:** `tests/unit/test_bot_loop.py:1043-1062`

```python
def test_feed_stale_transitions_first_then_emits_alert_then_ticks(monkeypatch) -> None:
    bot, pieces = _build_with_alerter(monkeypatch)
    bot.start()
    bot.mark_ready()
    initial_state = bot.state
    pieces["feed"].fire(FeedEvent(
        kind=FeedEventKind.FEED_STALE, pair="*",
        candle=None, timestamp=_NOW,
    ))
    assert initial_state == BotState.NORMAL
    assert bot.state == BotState.STALE  # transition happened
    assert pieces["alerter"].events == ["send:FEED_STALE", "tick"]
```

The test verifies:
- Initial state was NORMAL ✓
- Final state is STALE ✓
- Alerter saw `send` then `tick` ✓

What it does NOT verify is the docstring's actual claim: **"state
transition BEFORE the alert"**. The transition could happen before,
during, or after the `_alerter.send` call — the test asserts only
on the alerter-internal call ordering (which is `send` before
`tick`, never about state-vs-alert).

If a future refactor reorders the `_dispatch_event` block to
`send → transition → tick`, the docstring promise is violated but
this test still passes.

**Suggested fix:** make `_RecordingAlerter.send` capture `bot.state`
at send-time:

```python
class _RecordingAlerter:
    def __init__(self, bot_state_getter=None) -> None:
        ...
        self._state_getter = bot_state_getter
        self.state_at_send: list = []

    def send(self, alert) -> None:
        self.sent.append(alert)
        self.events.append(f"send:{alert.event_subtype}")
        if self._state_getter is not None:
            self.state_at_send.append(self._state_getter())
```

then:

```python
assert pieces["alerter"].state_at_send == [BotState.STALE]
```

That actually pins the ordering invariant. Same fix applies to
the FEED_RESUMED ordering test (test_bot_loop.py:1064-1079).

Severity: MEDIUM because the *intent* documented in the production
code is genuinely important (the alert text describes current state)
and the test claims to verify it. A test that lies about what it
checks is worse than no test.

---

### M5 (MEDIUM — TelegramAlerter is not thread-safe; integration 2b introduces multi-threaded callers)

**Files:** `src/alerts/coalescer.py:97` (no lock on `_pending`),
`src/bot/main.py:137-142` (main-thread STARTUP alert), `:155`
(main-thread SHUTDOWN alert).

`AlertCoalescer._pending` is a plain `dict` with no surrounding lock.
The coalescer's `add()` method does a multi-step
read-then-mutate (`existing = self._pending.get(key); if existing
is None: self._pending[key] = ...`) that is **not atomic** across
multiple Python-bytecode instructions even under the GIL.

Pre-2b, all alerts originated on the LS reader thread (one writer).
2b introduces two main-thread callers:

- `_emit_startup_alert` — fires after `mark_ready()` (state is
  NORMAL, LS events are live) and before
  `bot.shutdown_event().wait()`. LS reader thread may concurrently
  call `_send_alert(...)` for FEED_STALE / FEED_RESUMED / TRADE_*.
- `_emit_shutdown_alert` — fires after `shutdown_event` is set but
  before `bot.stop()` drains. The LS reader thread can still be
  mid-`_dispatch_event` when state flips to SHUTTING_DOWN if it
  passed the entry-gate check before the transition.

In practice the realistic concurrent call sites all carry
**distinct event_subtypes** (`STARTUP` / `SHUTDOWN` vs `FEED_STALE`
/ `TRADE_OPENED` / …), so the `(category, event_subtype, pair,
severity)` coalesce keys do not collide. Distinct-key dict mutations
under the GIL are safe at the individual-bytecode level.

But the design contract ("single-thread alerter") is no longer
maintained and the safety relies on the call-site set never growing.
A future Phase 10 addition (e.g. a healthcheck alert on a separate
heartbeat thread that emits `STARTUP` or `SHUTDOWN` retries) could
silently introduce a same-key race.

**Suggested fix:** add a `threading.Lock` to the coalescer
(`self._lock = threading.Lock()`) and wrap `add()` / `tick()` /
`drain_all()` / `close()` with it. The lock is uncontended in the
single-threaded steady state, so cost is negligible.

Severity: MEDIUM because the current call set doesn't hit the race,
but the integration silently broke the single-thread contract that
the alerts module was documented as relying on.

---

### M6 (MEDIUM — `tick()` after `close()` silently no-ops; commit 2a's M2 finding now affects a real path)

**Files:** `src/bot/loop.py:1174-1181` (`_tick_alerter`), `:389-407`
(`stop()`).

`bot.stop()` sequence:

1. `self._drain_inflight(...)` — waits for in-flight broker calls.
2. `self._positions.save_if_dirty()`.
3. `self._alerter.close()` — coalescer marked closed.
4. `self._feed.stop()` — LS disconnects.

Between steps 3 and 4 the LS reader thread can still fire BAR_CLOSE
events. The handler runs `_handle_bar_close` → `_tick_alerter` →
`self._alerter.tick()`. After `close()`, the coalescer's `tick()`
silently no-ops (per the M2 finding deferred from the commit 2a
review).

`_send_alert` calls during this window are rejected with a WARNING
log line (the L5 fix in commit 2a). `_tick_alerter` calls produce
no log line at all — the operator has no breadcrumb that a tick
fired post-close.

**Suggested fix:** match the send-after-close asymmetry. Either:

- Make `coalescer.tick` emit a DEBUG (not WARNING — it's benign)
  when called after close.
- Or have `BotLoop._tick_alerter` check `self._alerter.closed`
  (would need a public property) and skip with a DEBUG.

Severity: MEDIUM because commit 2a's deferred M2 was filed under
"address in commit 2b's integration pass"; this is the integration
pass.

---

### M7 (MEDIUM — duplicate `_git_short_hash` invocation in STARTUP alert)

**File:** `src/bot/main.py:386-389`

```python
f"Build: {_git_short_hash()} ({_git_branch_name()})"
)
short = f"started ({_git_short_hash()}, {ig_env})"
```

`_git_short_hash()` is called twice. Each call is a fresh
`subprocess.run(["git", "rev-parse", "--short", "HEAD"], timeout=2.0)`.
Two subprocess invocations for one alert. Worst case (git
unresponsive): 4 seconds. Plus `_git_branch_name` adds another
2 seconds. STARTUP alert can stall up to 6 seconds.

**Suggested fix:** cache:

```python
git_hash = _git_short_hash()
git_branch = _git_branch_name()
full = (
    f"\U0001f916 BOT STARTUP\n"
    f"Account: {ig_env}\n"
    f"Pairs: {len(pairs)} ({pair_list})\n"
    f"Hydration: {cached} cached, {rest} REST\n"
    f"Build: {git_hash} ({git_branch})"
)
short = f"started ({git_hash}, {ig_env})"
```

Severity: MEDIUM rather than LOW because STARTUP is on the critical
boot path and three subprocess calls per startup is ugly enough to
catch in a review — not because the runtime cost matters.

---

### L1 (LOW — STARTUP/SHUTDOWN tests use substring assertions only)

**Files:** `tests/unit/test_bot_main.py:123-198`

The tests check substrings (`"BOT STARTUP"`, `"Account: DEMO"`,
`"Pairs: 2 (GBPUSD, EURUSD)"`) but don't pin the **full structure**:

- The bot-emoji (`\U0001f916`, "🤖") at the start.
- The exact `\n` line-break layout (the plan locked the format).
- The trailing `"Build: …"` line ordering.

A refactor that drops the emoji, joins lines with `, ` instead of
`\n`, or reorders header → body → footer would silently pass the
current tests.

**Suggested fix:** add one structural assertion per test:

```python
expected_lines = [
    "\U0001f916 BOT STARTUP",
    "Account: DEMO",
    "Pairs: 2 (GBPUSD, EURUSD)",
    "Hydration: 180 cached, 70 REST",
    "Build: abc1234 (feature/alerts-integration)",
]
assert a.full_text == "\n".join(expected_lines)
```

The locked plan format is the contract; the test should pin it.

---

### L2 (LOW — `_emit_startup_alert` uses `datetime.now` directly, not the bot's clock)

**File:** `src/bot/main.py:398`, `:435`

```python
timestamp=datetime.now(timezone.utc),
```

Every other Alert in the codebase uses an injected clock
(`self._clock()` in BotLoop and Executor). `bot.main` doesn't have
a clock to inject, so this is consistent with where it's called,
but it does mean STARTUP/SHUTDOWN tests can't pin the timestamp
without monkeypatching `datetime.now`.

Not a real bug; flagged in case tests want timestamp-stability.

---

### L3 (LOW — no git tag detection)

**File:** `src/bot/main.py:330-359`

`_git_short_hash` and `_git_branch_name` give the operator a short
hash and a branch name. If the bot is deployed from a tagged
release (`v1.0.3`), the operator sees the hash but not the tag.

**Suggested fix (only if useful):** add `_git_tag()` that runs
`git describe --tags --exact-match HEAD` and falls back to
`"unknown"`. Surface in the STARTUP alert as
`Build: v1.0.3 (abc1234)` when tagged, else fall back to current
format.

The plan didn't specify tag handling; flag this as a future
enhancement.

---

### L4 (LOW — `_RecordingAlerter` in test_bot_loop.py doesn't enforce the alerter contract)

**File:** `tests/unit/test_bot_loop.py:932-955`

`_RecordingAlerter.send` accepts any object and just appends it.
It doesn't validate that the object is actually an `Alert`. Tests
that pass through it could miss a regression where BotLoop passes
a malformed payload (e.g. a dict instead of an `Alert`).

The real `TelegramAlerter.send` accepts `Alert` (frozen dataclass),
and Python's lack of runtime type-checking means the recording
alerter never trips on a regression like:

```python
self._alerter.send({"category": ..., "event_subtype": ..., ...})
```

**Suggested fix:** assert the type in the fake:

```python
def send(self, alert) -> None:
    from alerts import Alert
    assert isinstance(alert, Alert), f"expected Alert, got {type(alert).__name__}"
    self.sent.append(alert)
    ...
```

Same applies to `test_execution_executor.py::_RecordingAlerter` and
`test_bot_main.py::_RecordingAlerter`.

---

### L5 (LOW — no-alerter mode test doesn't cover the force-close or reconciliation alert paths)

**File:** `tests/unit/test_bot_loop.py:1114-1124`

`test_no_alerter_wired_does_not_raise_on_any_path` exercises:

- `bot.start()`
- `bot.mark_ready()`
- `bot.request_shutdown(reason=...)`
- `FEED_STALE`
- `bot.stop()`

It does NOT exercise:

- A `_execute_force_close` round (which goes through
  `_send_alert(TRADE_CLOSED)` and `_recent_closes` mutation).
- A reconciliation outcome with actionable kinds (which calls
  `_dispatch_reconciliation_alerts`).
- A successful `Executor.open_from_signal` (which calls
  `_emit_trade_opened`).

All of these have the `if self._alerter is None: return` guard so
they're internally safe, but the test misses coverage of those
guards. A future refactor that, say, restructures `_send_alert` and
removes the early-out before the `try:` block would silently break
no-alerter mode.

**Suggested fix:** extend the test with at least one force-close
call and one `_dispatch_reconciliation_alerts(outcome)` call against
the no-alerter bot.

---

### P1 (PROCESS — branch state) — commit 2b is in the working tree, not committed

**File:** the entire diff lives in the working tree.

```
On branch feature/alerts-integration
Changes not staged for commit:
  modified:   src/bot/loop.py
  modified:   src/bot/main.py
  modified:   src/execution/executor.py
  modified:   tests/unit/test_bot_loop.py
  modified:   tests/unit/test_bot_main.py
  modified:   tests/unit/test_execution_executor.py
```

Same pattern as the Phase 8 H2 finding and the commit 2a P1.
Commit the diff before merging so bisect / rollback has a
boundary. Same fix recipe as commit 2a: `feat(bot+execution):
Phase 9 commit 2b — wire alerts into BotLoop, Executor,
bot.main`.

---

## Spec walkthrough

Locked design decisions vs. implementation.

| Decision | Status | Verified by |
|----------|--------|-------------|
| BotLoop owns single alerter, threads through Executor | ✓ | `_build_runtime` (main.py:234, 288) passes the same `alerter` instance to both |
| `bot.main` constructs `TelegramAlerter()` from env, no-op mode if missing | ✓ | main.py:86 — no-op mode handled inside `TelegramAlerter` itself |
| FEED_STALE/FEED_RESUMED: state transition FIRST, then alert | ✓ (code) / ⚠️ (test — see M4) | loop.py:508-516, 520-528 |
| TRADE_OPENED after `position_manager.upsert` succeeds | ✓ | executor.py:213 (`upsert`), :257 (`_emit_trade_opened`) — emit follows upsert; emergency-close path doesn't emit |
| AMEND_FAILED after retry exhaustion only | ✓ | executor.py:322-325 — emit inside `for/else` |
| AMEND_FAILED on broker-accepted-but-persist-failed | **✗** | See H1 — gap on the upsert at executor.py:342 |
| TRADE_CLOSED from `_execute_force_close` (EOD) | ✓ | loop.py:884-899 |
| TRADE_CLOSED from reconciliation broker-close detection | ✓ | loop.py:1208-1218 (POSITION_CLOSED branch) |
| BROKER_ORPHAN / MISSING_LOCAL_KEPT / MANUAL_SL_MOVE only as WARNING/ALERT | ✓ (code) / ✗ (comment — see M2) | loop.py:1219-1251 |
| INFO reconciliation events suppressed | ✓ | loop.py:1252-1257 |
| STARTUP alert: bot-emoji + Account + Pairs + Hydration + Build | ✓ (format) / partial (test — L1, M3 degraded mode) | main.py:382-388 |
| SHUTDOWN: INFO clean / CRITICAL crashed | ✓ | main.py:419-426 |
| FAILURE_THRESHOLD_TRIPPED: CRITICAL via `request_shutdown(reason=...)` | ✓ | loop.py:341-350 |
| `_recent_closes` deal log prevents re-alert of cleanly-closed positions | ✓ (function) / ✗ (cleanup — see M1) | loop.py:878-883, reconciliation.py:245-258 |
| Coalesced summary tick on every BAR_CLOSE + feed transition | ✓ | loop.py:517, 529, 611 |

---

## Test-quality observations

- **`_RecordingAlerter` is genuinely useful** — recording the
  `events` list captures both `send` ordering and tick/close
  ordering. It does what Phase 8's `_RecordingExecutor` did right.
- **`test_request_shutdown_with_reason_emits_critical_failure_alert`**
  is solid: pins `event_subtype`, `severity is CRITICAL`,
  `category is SYSTEM`, and substring of the reason in `full_text`.
- **`test_force_close_emits_trade_closed_and_records_in_deal_log`**
  asserts both the alert AND the side-effect on `_recent_closes`.
  Good — but doesn't assert the **timing** of the side-effect vs
  the alert (the entry could be added after the alert and the test
  would pass).
- **`test_reconciliation_dispatches_alerts_for_actionable_kinds`**
  is the best test in this commit. Constructs an outcome with all 8
  ReconciliationKind values, asserts the alert subtypes set, pins
  the suppression boundary cleanly.
- **`_FakeIGClient.close_position` returns synthetic ACCEPTED by
  default** — this is the right shape for the happy-path tests but
  means rejection-path tests must explicitly set `close_should_fail`.
  Convention is fine and used correctly.
- **No test for amend-success + persist-failure** — directly maps to
  H1. Adding one would also force the fix.
- **No test asserting `_recent_closes` is bounded over many
  force-closes** — maps to M1. A test that runs 100 force-closes and
  asserts `len(bot._recent_closes) < N_close_history_bound` would
  catch the leak.
- **No test for concurrent send() from multiple threads** — maps to
  M5. A test that launches two threads each calling
  `bot._send_alert(...)` and asserting the coalescer's `pending_count`
  matches `2 - n_coalesced` would catch the race. Threading tests
  are notoriously flaky; documenting the limitation may be more
  pragmatic than adding the test.

---

## Deferred-item check from commit 2a review (M2, M3, M4, M5-DEL, L1-L5, P1)

| Item | Status now | Should have been addressed in 2b? |
|------|-----------|------------------------------------|
| M2 (tick-after-close observability) | **Now affects a real path** — see M6 above. | Yes — would have been a 3-line fix. |
| M3 (em-dash docstring sweep in coalescer.py) | Unchanged. Docstring-only. | No — cosmetic. |
| M4 (filter doesn't auto-install for future submodules) | Unchanged. | No — no new alerts.* submodules in 2b. |
| M5-DEL (`\x7f` survives sanitiser) | Unchanged. | No — no new control-char vectors in 2b. |
| L1 (filter doesn't scrub args for non-string elements) | Unchanged. | No. |
| L2 (`_truncate_for_log` off-by-three) | Unchanged. | No. |
| L3 (DEBUG log fires during close-drain) | Unchanged. | No. |
| L4 (coalescer `close()` semantics for tick/drain_all) | Unchanged. | No. |
| L5 (`_DELIVERY_LOG_MAX_LEN` vs `_MAX_TEXT_LOG_LEN` magic numbers) | Unchanged. | No. |
| P1 (commit hygiene) | Re-surfaced for commit 2b — see P1 above. | n/a — process discipline. |

Only M2 from commit 2a meaningfully escalated with integration;
it now appears as M6 in this review.

---

## Final recommendation

**APPROVE WITH CONDITIONS.**

Fix the following before merging to `develop`:

1. **H1** — wrap `apply_amend`'s post-success upsert in
   try/except, emit `AMEND_FAILED` with a `persist_failed_after_broker_accept`
   reason, and re-raise so the call site sees the failure.
2. **M1** — bound `_recent_closes` growth. Either pop on the
   force-close success path (simplest) or add the TTL prune in
   `_maybe_reconcile`.
3. **M2** — fix the comment misclassification of STALE_POSITION /
   SL_DRIFT_LARGE as INFO-level. Two locations
   (loop.py:738-743, 1252-1256).

The following can land in 2b OR as a single follow-up cleanup
commit on `develop` (matching the Phase 7 / Phase 8 pattern):

- **M3** (degraded-pairs in STARTUP alert) — 3-line plumbing.
- **M4** (FEED_STALE/RESUMED ordering test actually pins the
  invariant) — 5-line test change.
- **M6** (tick-after-close observability) — finally clears commit
  2a's deferred M2.
- **M7** (cache `_git_short_hash` calls) — 3-line refactor.
- **L1**–**L5** — polish, all in this review's "follow-up bundle".

**M5** (thread-safety of the coalescer) is worth a docstring
acknowledgement minimum. The lock fix is small but should be
deliberate (5 lines + a test); deferring to commit 2c / Phase 10
is acceptable if the immediate call sites don't collide.

The work delivered is otherwise sound: the design decisions hold,
the integration paths are clean, the 840-test suite is green, and
the no-alerter mode is consistently respected via the `if
self._alerter is None: return` early-outs. Once H1, M1, and M2 land,
this is a clean APPROVE FOR MERGE.

**P1 (commit hygiene)**: please commit the working tree as
`feat(bot+execution): Phase 9 commit 2b — wire alerts into BotLoop,
Executor, bot.main` before merging. Same recipe as commit 2a.
