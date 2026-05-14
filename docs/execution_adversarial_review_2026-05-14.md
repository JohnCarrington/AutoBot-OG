# Phase 6 (feature/execution) — adversarial review

- **Branch reviewed:** `feature/execution` @ commit `ccb67f9`
- **Base:** `develop`
- **Date:** 2026-05-14
- **Reviewer:** AutoBot-OG (read-only audit pass)
- **Scope:** Phase 6 broker integration — `src/feed/ig_rest/*`, `src/execution/*`, and corresponding `tests/unit/test_ig_rest_*.py` / `tests/unit/test_execution_*.py`.
- **Test status:** `565 passed in 2.19s` — full suite green.

---

## Headline

**One CRITICAL bug.** The deal-confirmation parser reads the wrong IG field
to decide ACCEPTED vs REJECTED, and treats genuine IG rejections as
acceptances. The bot would record a phantom open position for every
rejected order — losing the next signal on the same source bar to its own
idempotency check, and surfacing the position as `MISSING_LOCAL_KEPT`
(operator-action-required) at the next reconciliation pass. This is a
direct money-loss path: lost trades on the entry side, recurring operator
alerts on the housekeeping side.

The remaining findings are MEDIUM / LOW — one inconsistency in
reconciliation event metadata, one narrow disk-write-then-restart race,
and a handful of polish items in tests.

**Recommendation:** **APPROVE WITH CONDITIONS** — fix C1 below before
merging to `develop`. The remaining items can land in a follow-up.

---

## Findings

### C1 (CRITICAL — money-loss) — `_parse_deal_confirmation` reads `status`, not `dealStatus`

**File:** `src/feed/ig_rest/positions.py:212-218`

```python
raw_status = str(raw.get("status") or "").strip().upper()
if raw_status == "REJECTED":
    status: Any = "REJECTED"
else:
    status = "ACCEPTED"
```

IG's `/confirms/{dealReference}` payload (which `trading_ig.create_open_position`
fetches internally via `fetch_deal_by_deal_reference`, see
`.venv/lib/python3.12/site-packages/trading_ig/rest.py:867-868`) carries
**two** orthogonal status fields:

| field        | semantics                                            |
|--------------|------------------------------------------------------|
| `dealStatus` | `"ACCEPTED"` or `"REJECTED"` — the canonical outcome |
| `status`     | position lifecycle: `"OPEN"`, `"UPDATED"`, `"AMENDED"`, `"CLOSED"`, `"DELETED"` |

For a rejected deal, IG's documented shape is `dealStatus: "REJECTED"`,
`reason: <code>`, and **`status` is absent or `null`** (the position never
reached a lifecycle state). The current parser reads `status`, sees `""`,
takes the `else` branch, and stamps the result `"ACCEPTED"`.

Empirically (probe of `_parse_deal_confirmation` with realistic IG
payloads):

```
Real IG REJECTED (status absent, dealStatus=REJECTED):
  parsed status:      'ACCEPTED'   ← BUG
  parsed deal_status: 'REJECTED'
  parsed reason:      'MARKET_OFFLINE'

Real IG REJECTED (status=null, dealStatus=REJECTED):
  parsed status:      'ACCEPTED'   ← BUG

Test-shape REJECTED (status=REJECTED, no dealStatus):
  parsed status:      'REJECTED'   ← only the synthetic test fixture trips this
```

End-to-end blast radius — probed by feeding a real-shape REJECTED payload
through `Executor.open_from_signal`:

```
Executor result when IG rejects (real-shape payload):
  success:          True
  deal_id:          REJ_DEAL_1
  rejection_reason: None
  Registered in mgr: 1 positions
  *** A REJECTED deal got registered as an active position ***
```

Downstream effects of one missed REJECTED:

1. `Executor` returns `success=True`. Caller treats the order as filled.
2. `PositionManager.upsert` records a phantom `ExecutionPosition` with the
   would-have-been SL. The signal's `(pair, strategy, source_candle_ts)`
   triple is now consumed — any retry of the same signal is silently
   suppressed by the idempotency check, **losing the entry permanently**
   for that source bar.
3. `SLManager` will eventually emit a BE-move amend for the phantom
   position (`evaluate_sl_amend` looks at the local `current_pnl_r` only,
   without consulting broker state). The amend lands at IG with a
   non-existent `deal_id`, producing a broker error.
4. `Reconciliation.reconcile` finds the phantom in local state but not in
   the broker payload. With no entry in `deal_confirmations_log`, the
   handler in `reconciliation.py:251-265` emits `MISSING_LOCAL_KEPT` at
   `ALERT` severity and **deliberately does not remove the local
   position**. The phantom persists across reconciliation passes,
   producing a recurring operator alert.
5. Spread / volatility error from IG (the most common reject reason on
   spreadbet accounts during news) becomes invisible — the operator never
   sees that the trade failed and the bot believes itself fully invested.

**Why the existing tests don't catch this**

`tests/unit/test_ig_rest_positions.py:166-175`:

```python
def test_parse_deal_confirmation_rejected_preserves_reason() -> None:
    raw = {
        "dealReference": "REF1",
        "status": "REJECTED",          # ← real IG never puts REJECTED here
        "reason": "MARKET_OFFLINE",
    }
    confirm = _parse_deal_confirmation(raw)
    assert confirm.status == "REJECTED"
```

The fixture invents a payload shape IG never emits. It passes only because
both the parser and the fixture share the same wrong assumption about
which field carries reject information. `test_parse_deal_confirmation_accepted`
(lines 144-163) is internally inconsistent — it sets *both* `status: "OPEN"`
*and* `dealStatus: "ACCEPTED"`, so the parser would happily flag ACCEPTED
either way and the test wouldn't notice if the canonical field changed.

**Fix outline (do not implement in this review)**

1. In `_parse_deal_confirmation`, treat `dealStatus` as canonical:
   ```python
   raw_deal_status = str(raw.get("dealStatus") or "").strip().upper()
   raw_lifecycle = str(raw.get("status") or "").strip().upper()
   if raw_deal_status == "REJECTED" or raw_lifecycle == "REJECTED":
       status = "REJECTED"
   elif raw_deal_status == "ACCEPTED":
       status = "ACCEPTED"
   else:
       # Conservative: missing dealStatus on a 200-OK confirm is itself
       # a fault — treat as rejected, log, escalate.
       status = "REJECTED"
   ```
   The `dealStatus`-absent branch should err on the side of *not* marking
   a position open. A confirm payload with no decisive accept signal is a
   library or API regression, not a green light.
2. Fix the `Executor` rejection-message format (`executor.py:174-177`) —
   it currently composes `status=...,deal_status=...,reason=...`, which
   gives `status=ACCEPTED,deal_status=REJECTED,...` for a real rejection.
   That's actively misleading in logs; collapse to `dealStatus` once
   that becomes the source of truth.
3. Rewrite `test_parse_deal_confirmation_rejected_preserves_reason` to use
   the real IG shape (`dealStatus: "REJECTED"`, no `status`). Add a
   regression test asserting the parser returns `REJECTED` even when
   `status` is missing entirely.
4. Add a test that the `Executor` *registers no position* and returns
   `success=False` for a real-shape REJECTED confirmation.

---

### M1 (MEDIUM) — `ReconciliationEvent.pair` is sometimes the pair, sometimes the epic

**File:** `src/execution/reconciliation.py:188-201`

In the broker-orphan branch:
```python
events.append(
    ReconciliationEvent(
        ...
        pair=broker.epic,                  # ← IG epic, e.g. "CS.D.GBPUSD.TODAY.IP"
        ...
    )
)
```

Every other path (`_handle_sl`, `_handle_missing_broker`, stale-check)
sets `pair=local.pair`, i.e. `"GBPUSD"`. The dataclass declares `pair:
Optional[str]` with no contract for which form.

Consequences:

- Phase 7's alerts module will group / filter events by `pair`. A
  `CS.D.GBPUSD.TODAY.IP` event won't sit next to a `GBPUSD` event in any
  per-pair summary, and an operator-side `count_for_pair("GBPUSD")` over
  the JSONL log silently misses orphan rows.
- The jsonl record is the canonical audit trail per `data/execution/reconciliation_events.jsonl`
  per `EXECUTION_RECONCILIATION_LOG_PATH`. Mixed field shapes degrade
  any downstream tooling.

**Severity:** MEDIUM. Doesn't lose money but produces silent data
inconsistency that will only surface when Phase 7 starts reading the log.

**Fix:** resolve the epic → pair once (IG epic-to-pair lookup is a single
mapping at v1 since GBPUSD is the only pair; Phase 7 will own the real
resolver) and set `pair=resolved_pair_or_None`. Until the resolver
exists, set `pair=None` for orphans and record the epic in `debug`
instead.

---

### M2 (MEDIUM) — Idempotency relies on a persisted upsert; a save failure leaves a window for desync

**File:** `src/execution/executor.py:182-193` + `position_manager.py:91-98`

The open-from-signal flow:

1. Submit to broker → broker accepts → returns `DealConfirmation`.
2. `self._positions.upsert(position)` → `state.upsert` (in-memory) +
   `state.save_if_dirty()` (disk).
3. Return `TradeResult(success=True, ...)`.

If step 2's disk write raises (`OSError` from `_save_atomic` — disk full,
permission denied, ENOSPC mid-fsync), the position is in memory but never
persisted, **and the `OSError` propagates out of `Executor.open_from_signal`
without being caught**. The executor's two `try/except` blocks
(`executor.py:139-167`) only wrap the broker call, not the manager
update.

Within the same process the next signal will hit the idempotency key
(in-memory state survived), so no double-open in the same session. But:

- Process restart between the failed save and any subsequent action
  loses the position from local state entirely. The broker still has it
  open; reconciliation will surface it as `BROKER_ORPHAN` (ALERT,
  operator action required).
- The caller (Phase 7 loop) sees an `OSError` it didn't expect from
  `open_from_signal`. The bot's outer loop may crash-loop on a single
  bad disk before getting to reconcile.

**Severity:** MEDIUM. Disk-write failures on a healthy server are rare,
but the failure mode is silent (no logged trade) and the recovery path
is operator-driven (BROKER_ORPHAN). At minimum the executor should
catch and log so the caller gets a structured failure instead of an
exception.

**Fix outline:** wrap `self._positions.upsert(position)` in try/except
that logs the broker's deal_id (so the operator knows what to reconcile)
and returns `TradeResult(success=False, deal_id=confirmation.deal_id,
rejection_reason="state_persist_failed:...")`. A follow-up could
introduce a write-ahead deal-log so any persisted confirmation is
itself an idempotency anchor.

---

### L1 (LOW) — Sleep budget is wasted on rejected-status retries

**File:** `src/execution/executor.py:245-251`

The amend retry loop sleeps for `EXECUTION_AMEND_RETRY_DELAY_S` (2 s)
between attempts, but a broker `REJECTED` status is not transient — IG
won't accept the same amend two seconds later. The current code
nevertheless retries on rejected-status, costing one extra REST call
plus the 2 s wait. The existing test `test_apply_amend_rejected_status_returns_failure`
already covers this path — and the second attempt is wasted.

**Suggested fix:** break out of the loop immediately on
`status == "REJECTED"` without a retry, and reserve the retry budget for
network exceptions / `AllowanceExceeded`. (Note: once C1 is fixed,
`status == "REJECTED"` will mean what it says.)

**Severity:** LOW. Wastes an allowance slot during news-rejection bursts;
no money loss.

---

### L2 (LOW) — `_parse_deal_confirmation` `direction` allow-list is order-sensitive

**File:** `src/feed/ig_rest/positions.py:220-225`

```python
raw_direction = raw.get("direction")
direction = (
    str(raw_direction).upper()
    if isinstance(raw_direction, str) and raw_direction.upper() in ("BUY", "SELL")
    else None
)
```

Fine for IG (IG uses BUY/SELL), but a different format (e.g. `LONG`/`SHORT`)
would silently produce `direction=None`. `_parse_position` raises
`ValueError` in the same case (`positions.py:172-173`) — inconsistent
strictness across two parsers in the same module. Confirmed by probing:
`_parse_position` rejects `LONG`/`SHORT`, `_parse_deal_confirmation`
quietly returns `None`.

**Severity:** LOW. IG never returns these forms; flagged for consistency.

---

### L3 (LOW) — Allowance backoff schedule isn't read from env at runtime

**File:** `src/feed/ig_rest/allowance.py:40`

```python
ALLOWANCE_BACKOFF_SCHEDULE: tuple[int, ...] = (60, 90, 120, 180)
```

The other tunables expose `IG_ALLOWANCE_RPM`. The schedule is hard-coded
— operators tuning during a real-world rate-limit incident can't change
it without a redeploy. Worth a `IG_ALLOWANCE_BACKOFF_SECONDS` env that
takes a comma-separated list.

**Severity:** LOW. Doesn't bite under normal operation.

---

### L4 (LOW) — `_handle_sl` "manual move" classifier rounds at 5 decimals

**File:** `src/execution/reconciliation.py:328-331`

```python
history_levels = {
    round(a.to_price, 5) for a in local.sl_history
} | {round(local.current_sl_price, 5)}
return round(broker.stop_level, 5) not in history_levels
```

For GBPUSD (pip = 0.0001, sub-pip price quote is 5 decimals) this is
exactly right. For JPY pairs (pip = 0.01, sub-pip quote = 3 decimals)
the same rounding is fine because the broker SL is always quantised to
the quote. **But** `pip_size_for` already exists and would adapt
automatically when v2 introduces JPY pairs. Worth converting to a
pip-aware tolerance — `abs(history - broker) <= pip_size_for(pair)
* 0.5` — for forward-compat. Not a v1 issue.

**Severity:** LOW.

---

### L5 (LOW) — Test fixture `test_parse_deal_confirmation_accepted` is internally inconsistent

**File:** `tests/unit/test_ig_rest_positions.py:144-163`

Sets `status: "OPEN"` (lifecycle field) and `dealStatus: "ACCEPTED"`
(canonical accept) simultaneously. Reads both, but neither test asserts
which one the parser is keying off. With C1 fixed, this test should be
split into:

- `test_parse_deal_confirmation_uses_dealStatus_canonically`
- `test_parse_deal_confirmation_accepts_when_status_absent_but_dealStatus_ok`
- `test_parse_deal_confirmation_rejects_when_dealStatus_rejected_regardless_of_status`

**Severity:** LOW. Test-quality issue, surfaced by C1.

---

## Per-question responses

The brief listed eleven scrutiny areas. Findings keyed to each:

### 1. trading_ig wrapper integrity

- **Token handling / auth:** `src/feed/ig_rest/auth.py` no longer carries
  any token-extraction code. The legacy `_extract_tokens` shim
  (referenced in the brief) is absent — `trading_ig 0.0.16` handles
  CST/X-SECURITY-TOKEN internally and `create_session()` is the only
  call needed. ✅ Clean. (Verified via grep — no CST,
  X-SECURITY-TOKEN, `_extract_tokens` anywhere in `src/`.)
- **`fetch_deal_confirmation` parse:** **C1** — the parser uses
  `status` not `dealStatus`. See above.
- **`close_position` direction:** `positions.py:85` correctly computes
  the opposite direction. ✅
- **`return_dataframe=False`:** explicitly set in `create_ig_service`
  (`auth.py:172`). ✅ No DataFrame-shape parsing in the shim.

### 2. AllowanceTracker

- **Rolling-window eviction:** `_evict` uses strict `<` against
  `now - window` (`allowance.py:143-144`). Probed: at exactly t = window,
  the oldest request is treated as elapsed (`oldest + window - now = 0`
  inside `should_backoff`). Correct.
- **Escalating backoff:** confirmed by `test_throttle_escalates_through_schedule`
  and `test_throttle_caps_at_longest_schedule_entry`. ✅
- **Raise rather than sleep:** `IGClient._gate` raises
  `AllowanceExceeded`, the caller (`Executor.open_from_signal`) maps
  it to a `TradeResult(success=False)` instead of blocking. ✅ Locked
  decision honored.
- **No remainingAllowance header parsing:** the tracker doesn't read
  IG's response headers — it's a local-count-only proxy. Acceptable
  for v1 (the trading_ig library doesn't expose response headers), but
  worth noting that the bot can't react to a server-side counter
  reset.
- **Hard-coded `ALLOWANCE_BACKOFF_SCHEDULE`:** **L3** — schedule isn't
  env-overridable.

### 3. PositionManager state

- **Idempotency triple-key:** `(pair.upper(), strategy_name, source_candle_ts)`
  in `_index_add` (`position_manager.py:128-130`) — matches the locked
  spec. ✅
- **Index lock-step with primary:** `_index_remove` runs before the
  state mutation in `upsert` if a previous entry existed; the index
  rebuilds from `state.values()` on `__init__`. Probed:
  `test_upsert_replacement_refreshes_indices` and
  `test_remove_clears_position_and_indices` cover it.
- **Persistence-on-mutation:** every `upsert` / `remove` calls
  `state.save_if_dirty`. **M2** — but if the disk write raises the
  in-memory state still has the position, and the caller gets an
  unhandled exception. See M2 above.
- **Pair case-insensitivity:** `for_pair` / `count_for_pair` upper-case
  the input; `_index_add` upper-cases the stored key. ✅
- **`signal_source_candle_ts` collision risk:** the key is a `datetime`,
  not a string — equality is tz-aware. Loaded-from-JSON datetimes
  preserve `tzinfo=UTC` (probed at `positions_state.py:283-285` —
  `datetime.fromisoformat` round-trips the `+00:00` offset). ✅

### 4. SLManager BE calculation

- **BE-at-+1R math:** probed for LONG and SHORT — exactly 1 pip above
  entry on LONG, 1 pip below on SHORT, with pip computed via
  `pip_to_price`. ✅
- **Spec adherence:** §6.2 says "move to break-even"; this implementation
  adds the 1-pip buffer, which is documented in `constants.py:75-81`
  and in the locked Phase 6 decisions. ✅ Acceptable deviation.
- **Defensive "don't widen on BE":** `sl_management.py:94-96` —
  `_improves` check prevents the BE move from loosening a tighter SL
  already in place. ✅
- **Trail gates on `trail_active`:** `evaluate_sl_amend` returns `None`
  when `not position.trail_active`, even past +1R, until the BE-move
  amend has confirmed. ✅ Locked decision honored.
- **Conservative-pick math:** `_pick_conservative` chooses the candidate
  closer to current price (max for LONG, min for SHORT). Correctly
  named "conservative" — it gives back the **least amount of gain to
  the trader** = safest. ✅
- **EMA20 wrong-side rejection:** `_candidate` returns `None` when EMA20
  is above current on a LONG (it'd put the SL above price = instant
  stop-out). ✅
- **Min-delta + FP noise tolerance:** the comparison
  `delta_pips + 1e-3 < EXECUTION_SL_AMEND_MIN_DELTA_PIPS` correctly
  handles 4-decimal subtraction noise. ✅

### 5. Executor logic

- **Idempotency check before broker call:** `executor.py:101-114` —
  short-circuits on existing position with same signal source. ✅
- **Translation `Signal → TradeOrder → OrderRequest`:** uses
  `EXECUTION_DEFAULT_SIZE_UNITS` (1.0 for v1) for size; carries SL/TP
  through. ✅
- **`AllowanceExceeded` → `TradeResult(success=False)`:** rejection
  reason includes the recommended sleep (`executor.py:149-154`). ✅
- **Broker-rejected interpretation:** **C1** — the parser feeds the
  wrong status into `confirmation.status`, so this branch never fires
  for real IG rejections. Same code on REAL ACCEPTED works fine.
- **Amend retry once:** loop count = `EXECUTION_AMEND_RETRY_COUNT + 1`
  = 2 attempts. **L1** — wastes a retry on REJECTED-status responses
  (not transient).
- **`_build_position` deal_id fallback:** uses `confirmation.deal_id
  or ""`. If real IG returns no `dealId` on an accepted deal (rare but
  observed for delayed processing), the position is keyed on `""`,
  which then collides with any other empty-deal-id entry. Worth a
  defensive check, but with C1 fixed this path effectively requires a
  partial-confirm bug from IG itself.

### 6. Reconciliation

- **Conservative defaults:**
  - Never auto-import orphan broker positions — `reconciliation.py:174-202`
    emits an event but no action. ✅
  - Never auto-delete local positions without a close-confirmation —
    `_handle_missing_broker` only schedules a removal when
    `deal_confirmations_log[deal_id]` exists. ✅
- **SL drift / silent update:** drift below `pip_size_for(local.pair) *
  0.5` is no-op; small drift → INFO + apply; large drift → WARNING +
  apply. Threshold:
  `EXECUTION_SL_DRIFT_WARN_PIPS=5.0` (default). ✅
- **`MANUAL_SL_MOVE` heuristic:** matches against `sl_history` plus
  `current_sl_price`. **L4** — 5-decimal rounding is GBPUSD-specific.
- **JSONL events log:** path defined in `constants.py:99-102`. No
  writer in the source tree — Phase 7 wires the writer. ✅
- **`ReconciliationEvent.pair`:** **M1** — mixed pair / epic semantics.
- **`_handle_sl` returns `None` when `broker.stop_level is None`:** silent
  no-op. The local SL stays at whatever it was; the next reconciliation
  pass tries again. This is the right call for IG's partial-confirm
  case but worth documenting.

### 7. Test quality

- **Coverage:**
  - `test_execution_executor.py` — 11 tests covering happy path, short
    direction translation, rejection, allowance, idempotency, retry-once,
    double-failure, rejected-status. ✅
  - `test_execution_sl_management.py` — 14 tests covering BE LONG/SHORT,
    trail conservative pick, swing-vs-ema20 priorities, wrong-side
    rejection, never-widen, min-delta, gate-on-be-moved. ✅
  - `test_execution_reconciliation.py` — 10 tests covering all kinds
    (OK_NO_OP, drift INFO/WARNING, MANUAL_SL_MOVE, MISSING_LOCAL_KEPT,
    POSITION_CLOSED, BROKER_ORPHAN, STALE_POSITION, mixed). ✅
  - `test_execution_position_manager.py` — 11 tests covering indices,
    idempotency lookup, persistence roundtrip. ✅
  - `test_execution_types.py` — 9 tests covering frozen-ness, R math,
    transition correctness. ✅
  - `test_ig_rest_*.py` — 60+ tests covering tracker, auth, client,
    positions parsers + dispatch.
- **Synthetic-payload bias:** **L5** + **C1**. The deal-confirmation
  rejected fixture uses a shape IG never emits. The test suite gives
  false confidence here.
- **No fixtures based on real IG responses:** would be valuable. Even
  a single canned `tests/fixtures/ig_confirm_rejected.json` lifted
  from IG's REST docs would have caught C1.

### 8. Type safety / dataclass usage

- All public types are `@dataclass(frozen=True)`:
  - `OrderRequest`, `AmendRequest`, `CloseRequest`, `DealConfirmation`,
    `BrokerPosition`, `MarketInfo` (`feed/ig_rest/types.py`).
  - `ExecutionPosition`, `SLAmendment`, `AmendOrder`, `AmendResult`,
    `TradeOrder`, `TradeResult`, `ReconciliationEvent`,
    `ReconciliationReport`, `ReconciliationActions`,
    `ReconciliationOutcome` (`execution/types.py`).
- `Direction`, `RegimeLabel` enums imported directly — no string-typing
  on the trading verbs.
- `Literal` types used appropriately: `direction: Literal["BUY", "SELL"]`,
  `status: Literal["ACCEPTED", "REJECTED"]`, `order_type: Literal[...]`.
- `Protocol` for the IG service stub (`auth.py:110-117`). ✅
- One `# type: ignore[misc]` in `position_manager.py:67` is necessary
  (filtering `None` out of a `list[Optional[T]]` comprehension).

### 9. SL amend retry

- Single retry with 2 s delay (`EXECUTION_AMEND_RETRY_COUNT=1`,
  `EXECUTION_AMEND_RETRY_DELAY_S=2.0` — `constants.py:91-92`). ✅
- Retries on both network exceptions and `AllowanceExceeded`. **L1** —
  the loop also retries on REJECTED-status, which is not transient.
- Local state untouched on amend failure
  (`test_apply_amend_double_failure_returns_failure`). ✅ Next
  reconciliation pass resolves any drift.

### 10. Spec adherence (`docs/v1_architecture.md` §6.x)

- §6.2 BE at +1R + 1-pip buffer: ✅ (deviates with a buffer, locked
  decision documented in `constants.py:75-81`).
- §6.3 trail table:
  - `bb_reclaim`: ema20 primary, swing secondary. ✅
  - `ema_continuation`: swing primary, ema20 secondary. ✅
  - `liquidity_sweep`: swing primary, ema20 secondary. ✅
- §6.11 locked decisions:
  - Trail gates on `be_moved`: ✅ (see `evaluate_sl_amend:67-71`).
  - Conservative = closer to price: ✅.
  - Never-widen: ✅ (BE + trail both `_improves`-checked).
  - EMA20 wrong-side rejection: ✅.
  - 1-pip BE buffer: ✅.
  - Idempotency triple-key: ✅.
  - JSONL reconciliation events log location: configured ✅; no writer
    yet (Phase 7).
  - Conservative reconciliation (never silent delete / import): ✅.
  - Allowance raises rather than sleeps: ✅.

### 11. Operational concerns

- **State paths under `data/execution/`:** ✅, gitignored by convention.
- **Atomic JSON writes:** `tempfile.mkstemp` + `fsync` +
  `os.replace` (`positions_state.py:166-188`). ✅ Crash-safe.
- **Fail-open load:** corrupt/missing → empty state with a WARNING.
  ✅ (sound for v1; later phases may want a "halt and require human"
  mode for prod).
- **`save_if_dirty`** failure: **M2** — uncaught `OSError` on disk
  failure leaks out of `Executor.open_from_signal`.
- **Restart safety:** `entry_time_utc` round-trips correctly. ✅ Stale
  threshold (8h) re-applies after restart.
- **Reconciliation cadence:** `EXECUTION_RECONCILIATION_INTERVAL_MIN=10`
  default (`constants.py:58-60`). Documented trade-offs in the
  docstring. ✅
- **No live network in tests:** all tests use injected fakes /
  `service_factory`. ✅
- **Log noise:** the `allowance` regex match (`client.py:174-180`) is
  case-insensitive and conservative. ✅

---

## Recommendation

**APPROVE WITH CONDITIONS.**

Block on:
- **C1** — `_parse_deal_confirmation` must read `dealStatus` as canonical
  before merge. The test fixtures must be rewritten to use real IG
  shapes. This is a money-loss bug; trading on the current parser would
  silently register every rejected order as a phantom open position.

Land in follow-up:
- **M1** — `ReconciliationEvent.pair` consistency.
- **M2** — `Executor.open_from_signal` should catch state-persist failures.
- **L1–L5** — polish.

Everything else (allowance math, idempotency, BE/trail logic, reconciliation
state machine, persistence) is solid and matches the locked Phase 6
spec. The integration shape — composable `IGClient` + free-function
`positions` module + position manager + reconciliation engine — is
clean and testable.

Tests: **565 passed in 2.19s**.

---

# Addendum — re-review after C1 / M2 / M1 fixes (commit `acae469`)

- **Date:** 2026-05-14 (same day, post-fix).
- **Commit reviewed:** `acae469` (`fix(execution): C1 ... M2 ... M1 ...`).
- **Scope:** verify the three fixes; check for regressions and new issues.
- **Test status:** `573 passed in 2.10s` — full suite green, +8 net tests.

## Headline

All three blocking findings are fixed correctly. The C1 fix uses the
documented IG canonical field (`dealStatus`) with a conservative
fallback that only flips a hypothetical edge case (empty / whitespace
`dealStatus`) into REJECTED + WARNING — and real IG payloads never
contain those. The M2 fix wires up the emergency-close path with both
required CRITICAL log entries firing in both branches. The M1 fix
normalises orphan-event `pair` via `pair_from_epic` and preserves the
raw epic in `debug`.

Two new LOW-severity observations surface (R1, R2 below). Neither
blocks merge.

## Verification of each fix

### C1 — `_parse_deal_confirmation` decision tree

**File:** `src/feed/ig_rest/positions.py:197-271`

New decision tree, walked through end-to-end:

| input shape                                                  | result    | warns? |
|--------------------------------------------------------------|-----------|--------|
| `dealStatus="ACCEPTED"`, `status="OPEN"` (real accepted)     | ACCEPTED  | no     |
| `dealStatus="ACCEPTED"`, `status="AMENDED"` (real amend)     | ACCEPTED  | no     |
| `dealStatus="ACCEPTED"`, `status="UPDATED"`                  | ACCEPTED  | no     |
| `dealStatus="ACCEPTED"`, `status="CLOSED"` (real close)      | ACCEPTED  | no     |
| `dealStatus="REJECTED"` (no `status`) — **real C1 case**     | REJECTED  | no     |
| `dealStatus="REJECTED"`, `status=null` — **real C1 case**    | REJECTED  | no     |
| `dealStatus="accepted"` (lowercase)                          | ACCEPTED  | no     |
| `status="REJECTED"`, no `dealStatus` (legacy/synthetic)      | REJECTED  | yes    |
| `dealStatus="ACCEPTED"`, `status="REJECTED"` (conflict)      | ACCEPTED  | no     |
| empty dict                                                   | REJECTED  | yes    |
| `status="OPEN"`, no `dealStatus`                             | REJECTED  | yes    |

The conflict case (`dealStatus=ACCEPTED` + `status=REJECTED`) correctly
prefers `dealStatus` — `status` is the lifecycle field, not the
verdict.

**Q: Does the conservative-REJECTED fallback flip real ACCEPTED responses?**

No. The fallback fires only when `dealStatus` is empty or whitespace.
IG's REST contract guarantees `dealStatus ∈ {"ACCEPTED", "REJECTED"}`
on every well-formed `/confirms` response (which `trading_ig`'s
`create_open_position` always fetches internally — see
`.venv/lib/python3.12/site-packages/trading_ig/rest.py:867-868`). The
two flip-paths I found probing:

- `dealStatus=""`  → REJECTED + WARNING.
- `dealStatus="  "` (whitespace) → REJECTED + WARNING.

These are not real IG shapes; the previous parser silently treated
them as ACCEPTED (because `status="OPEN"` doesn't equal `"REJECTED"`).
The new behavior is strictly more conservative and the WARNING surfaces
the anomaly. ✅

**End-to-end probe (Executor + parser):**

```
Real-IG REJECTED payload through Executor.open_from_signal:
  result.success=False
  manager has 0 positions
  by_signal_source lookup returns None
  → idempotency key NOT consumed; signal can retry next cycle
```

**Idempotency unblocked**: probed a sequence of `[REJECT, ACCEPT]`
calls with the same `(pair, strategy, source_ts)` triple — second
attempt succeeds because the rejection didn't register a phantom
position. This is the desired correction.

### M2 — Persistence failure triggers emergency close

**File:** `src/execution/executor.py:189-249`

Both branches probed:

**CASE A — persist fails, close succeeds:**
```
OSError("disk full") re-raised
open_calls=1, close_calls=1
CRITICAL log records: 2
  [CRITICAL] Persist failed after broker accepted open: deal_id=D1, pair=GBPUSD, ...
  [CRITICAL] Emergency close submitted for deal_id=D1 after persist failure.
```

**CASE B — persist fails, close ALSO fails:**
```
OSError("disk full") re-raised (NOT the RuntimeError from close)
open_calls=1, close_calls=1
CRITICAL log records: 2
  [CRITICAL] Persist failed after broker accepted open: deal_id=D1, pair=GBPUSD, ...
  [CRITICAL] Emergency close ALSO FAILED for deal_id=D1 after persist failure: ... MANUAL INTERVENTION REQUIRED ...
```

Both CRITICAL logs fire in both branches; the original `OSError` is
re-raised, not the secondary close exception (correct — root cause
must surface first). The exception chain is preserved via the natural
re-raise (no `raise ... from None`).

**Direction mapping on emergency close:** verified correct. A BULLISH
open emits `CloseRequest(position_direction="BUY")`, which the
positions wrapper flips to broker `direction="SELL"` to close the
position. The unit-test assertion at the executor boundary stops at
`position_direction == "BUY"` because the wrapper isn't under test in
that case.

### M1 — `pair_from_epic` on orphan events

**File:** `src/execution/reconciliation.py:180-211`

`pair_from_epic` (from `config/pair_config.py:87-94`) returns a
**non-None string** for every input — split on "." and take index 2,
else upper-case the input. Probed:

| epic                          | resolved pair    |
|-------------------------------|------------------|
| `CS.D.GBPUSD.TODAY.IP`        | `"GBPUSD"`       |
| `IX.D.SPDOW.DAILY.IP` (index) | `"SPDOW"`        |
| `"GBPUSD"` (bare)             | `"GBPUSD"`       |
| `"weird-style"`               | `"WEIRD-STYLE"`  |
| `""`                          | `""`             |

**Q: Does `pair_from_epic` return `None` and cause downstream issues?**

No — the function's return type is `str` (never `Optional[str]`). The
downstream `ReconciliationEvent.pair: Optional[str]` field can therefore
hold a malformed-looking string (`"WEIRD-STYLE"`, `""`) but never
`None` from this path. No `NoneType` errors are reachable.

For non-forex epics (FTSE/SPDOW etc.), the "pair" field carries the
index symbol — which v1's Phase 7 alerts can't group sensibly because
v1 only trades forex. Two mitigating facts:

1. An orphan broker position on a non-forex epic is itself anomalous
   and surfaces as an ALERT-severity orphan event — the operator will
   investigate, not auto-aggregate.
2. The raw IG epic is preserved in `debug["broker_epic"]` for full
   diagnostic context.

The test (`test_broker_orphan_emits_alert_no_action`) now asserts both
the normalised pair and the preserved raw epic. ✅

### Test fixture realism

The new fixtures match IG's documented `/confirms` payload shape:

- `test_parse_deal_confirmation_real_ig_accepted_shape` — `dealStatus=ACCEPTED`
  + `status=OPEN` (the real-world standard shape).
- `test_parse_deal_confirmation_real_ig_rejected_shape` — `dealStatus=REJECTED`
  with **no `status` field** (documented as "no status field — that's
  what real IG sends for rejects" in the test docstring).
- `test_parse_deal_confirmation_real_ig_rejected_status_null` — variant
  with `status=null` on the wire.
- `test_parse_deal_confirmation_amended_lifecycle_still_accepted` —
  `dealStatus=ACCEPTED` + `status=AMENDED` for amend confirms.

These match the schema described by trading_ig's `fetch_deal_by_deal_reference`
(`/confirms/{deal_reference}` endpoint) and the IG REST API
documentation. Not based on docstring examples — derived from
empirical knowledge of IG's wire format. The `_FakeService` payloads
in the other tests were also updated to include `dealStatus="ACCEPTED"`
alongside the existing lifecycle status, so they're more faithful to
real responses now. ✅

### Synthetic test removal

The previously-passing synthetic-shape test `test_parse_deal_confirmation_rejected_preserves_reason`
was **rewritten with a new name** (`test_parse_deal_confirmation_real_ig_rejected_shape`),
not commented out. The rewrite:

- Renames the function so a casual `git log -L` or `grep` for the old
  name returns nothing — no risk of resurrection.
- Replaces the synthetic payload (`status: "REJECTED"`) with the real
  IG shape (`dealStatus: "REJECTED"`, no `status`).
- Adds an explicit docstring naming the C1 review.
- Adds an in-code comment in the fixture: `# NOTE: no "status" field —
  that's what real IG sends for rejects.`

The old shape lives on as a backwards-compat coverage in the new
`test_parse_deal_confirmation_falls_back_to_status_with_warning` test
— but explicitly framed as the fallback path, with a WARNING assertion
to keep it honest. ✅

## New observations introduced by the fixes

### R1 (LOW — observation) — In-memory state desync on persist failure

**File:** `src/execution/position_manager.py:91-98`

`PositionManager.upsert` updates in-memory state **before** calling
`save_if_dirty()`:

```python
def upsert(self, position):
    previous = self._state.get(position.deal_id)
    if previous is not None:
        self._index_remove(previous)
    self._state.upsert(position)        # in-memory mutation
    self._index_add(position)
    self._state.save_if_dirty()         # raises on disk failure
```

When the save fails, the in-memory state already holds the position
and both indices reflect it. The executor's M2 path then catches the
exception, emergency-closes at the broker, and re-raises. The
in-memory state is now **desynced** from the broker (broker closed it)
**and** from disk (disk never saw the write).

**Why this is acceptable:**

- The plan and the M2 commit explicitly re-raise so the caller (Phase
  7 loop) crashes loudly. On crash, the in-memory state is gone.
- On restart, `PositionManager.load_from_path` reads from disk —
  which never received the write — and the loaded state matches the
  (now-closed) broker state.
- Therefore the desync window exists only during the brief gap
  between raise and process death.

**Why it's worth flagging:**

If a future caller catches the `OSError` and continues without
restarting, the in-memory state will stay desynced from disk and
broker indefinitely. The next `reconciliation` pass would re-find the
phantom (local has it, broker doesn't) as `MISSING_LOCAL_KEPT` — but
that recovery is alert-driven, not automatic.

**Fix outline (Phase 7 timing):** the executor's persist-failure block
could also call `self._positions.remove(confirmation.deal_id)` after
the emergency close, to scrub in-memory state before re-raising. Doing
so safely requires `remove` to be infallible (it touches disk too); a
simpler approach is to clear in-memory state directly via a new
`PositionsState.discard_in_memory(deal_id)` that doesn't attempt to
save. Not needed for v1 if Phase 7 commits to crash-on-OSError.

**Severity:** LOW. Doesn't block merge; document the expected
caller behavior.

### R2 (LOW — observation) — Conservative-REJECTED fallback path is silent in production logs by default

**File:** `src/feed/ig_rest/positions.py:251-269`

The two conservative-fallback branches log at `WARNING` level. If
the production log configuration is set to `ERROR` or higher (common
in busy bots), these warnings disappear — and the bot silently
reports REJECTED on a payload that may merit operator inspection.

**Severity:** LOW. The behavior is still safe (REJECTED, not phantom
ACCEPTED), but observability is degraded. Worth ensuring the bot's
final root-logger config keeps `feed.ig_rest.positions` at WARNING
or above. Could be promoted to `ERROR` level if Phase 7 wires alerts
to the IG shim — these are anomaly events, not normal-path warnings.

## Status of pre-fix findings

| ID | Severity | Status         | Notes                                              |
|----|----------|----------------|----------------------------------------------------|
| C1 | CRITICAL | **FIXED**      | Parser keys on `dealStatus`; tests realistic.      |
| M1 | MEDIUM   | **FIXED**      | `pair_from_epic` + `debug["broker_epic"]`.         |
| M2 | MEDIUM   | **FIXED**      | Emergency close + dual-CRITICAL logging.           |
| L1 | LOW      | not addressed  | Amend retry wastes attempt on REJECTED status.     |
| L2 | LOW      | not addressed  | `_parse_deal_confirmation` direction allow-list.   |
| L3 | LOW      | not addressed  | Backoff schedule not env-overridable.              |
| L4 | LOW      | not addressed  | `_is_manual_move` 5-decimal rounding GBPUSD-only.  |
| L5 | LOW      | **FIXED**      | Subsumed by C1 fixture rewrite (7 tests now).      |
| R1 | LOW      | new this pass  | In-memory state desync on persist failure.         |
| R2 | LOW      | new this pass  | Conservative-fallback WARNING visibility.          |

## Final test status

```
$ .venv/bin/python -m pytest tests/ -q
............................................................................. (573 dots)
573 passed in 2.10s
```

8 net new tests (573 - 565), zero warnings, zero skips, zero errors.
The targeted re-verification of the changed files:

```
tests/unit/test_ig_rest_positions.py::test_parse_deal_confirmation_real_ig_accepted_shape PASSED
tests/unit/test_ig_rest_positions.py::test_parse_deal_confirmation_real_ig_rejected_shape PASSED
tests/unit/test_ig_rest_positions.py::test_parse_deal_confirmation_real_ig_rejected_status_null PASSED
tests/unit/test_ig_rest_positions.py::test_parse_deal_confirmation_dealStatus_canonical_when_status_disagrees PASSED
tests/unit/test_ig_rest_positions.py::test_parse_deal_confirmation_falls_back_to_status_with_warning PASSED
tests/unit/test_ig_rest_positions.py::test_parse_deal_confirmation_no_decisive_signal_treated_as_rejected PASSED
tests/unit/test_ig_rest_positions.py::test_parse_deal_confirmation_amended_lifecycle_still_accepted PASSED
tests/unit/test_execution_executor.py::test_open_from_signal_real_ig_rejected_shape_does_not_register_position PASSED
tests/unit/test_execution_executor.py::test_open_from_signal_persist_failure_emergency_closes_and_raises PASSED
tests/unit/test_execution_executor.py::test_open_from_signal_persist_failure_close_also_fails_still_raises_original PASSED
tests/unit/test_execution_reconciliation.py::test_broker_orphan_emits_alert_no_action PASSED (with new M1 assertions)
```

## Final recommendation

**APPROVE FOR MERGE.**

The three blocking findings (C1, M2, M1) are fixed correctly with
realistic test coverage. The two new observations (R1, R2) are
LOW-severity and do not block merge — they are documentation of
expected behavior and observability hygiene, not bugs. The remaining
LOW items (L1-L4) from the original review can land in follow-up
PRs as agreed.

Phase 6 is ready for merge into `develop`.
