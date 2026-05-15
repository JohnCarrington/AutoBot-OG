# bot

Phase 8 main loop — the orchestrator that wires Phase 1-7 into a
running trading process.

## Owns

- `types.py` — `BotState` enum, `BotRuntimeConfig`, `FailureCounter`,
  `PreFlightReport` / `CheckResult`.
- `constants.py` — failure thresholds, reconciliation cadence,
  shutdown-drain timeouts, log defaults. No EOD constant — Phase 4
  owns that.
- `logging_setup.py` — `setup_logging()`; idempotent root-logger
  configuration with third-party noise suppression.
- `preflight.py` — `run_static_checks()` (env vars, disk writability,
  IG auth) and `verify_subscriptions()` (post-`start_live` poll).
- `loop.py` — `BotLoop` class. State machine, BAR_CLOSE pipeline,
  inline scheduler (reconciliation, force-close), failure counters,
  in-flight broker call tracking for the graceful shutdown drain.
- `main.py` — `main()` entrypoint. Loads `.env`, runs pre-flight,
  installs SIGTERM/SIGINT handlers, builds the runtime tree, blocks
  on the shutdown event, drives the drain.

## Pipeline per BAR_CLOSE

1. Always-on: update indicators → structure → regime (even on
   gap-fill bars, so historical state stays consistent).
2. Periodic: `_maybe_reconcile()` (every 10 min) and
   `_maybe_force_close_orders()` (every BAR_CLOSE; Phase 4 owns the
   "fire once per day" guard).
3. State-machine RESUMING → NORMAL on the first live BAR_CLOSE, in
   case GAP_FILLED never arrives (no gap, or gap > window).
4. Signal gate: skip the strategy/risk/execution pipeline during
   `STALE` / `RESUMING` / `SHUTTING_DOWN` and on gap-fill bars.
5. SL evaluation per open position on the pair (BAR_CLOSE cadence —
   not BAR_UPDATE, not a separate timer).

## State machine

```
STARTING → NORMAL (after start_live)
NORMAL → STALE (FEED_STALE)
STALE → RESUMING (FEED_RESUMED)
RESUMING → NORMAL (GAP_FILLED OR first live BAR_CLOSE)
*  → SHUTTING_DOWN (SIGTERM/SIGINT or failure threshold trip)
```

## Failure isolation

Two independent 5-strike counters (`event_failures`,
`periodic_failures`). Each trip triggers shutdown independently. A
flaky 10-min reconciliation doesn't kill the bot on the next live
bar, and vice versa.

## Graceful shutdown

On SIGTERM/SIGINT:

1. `BotLoop.request_shutdown()` flips state to `SHUTTING_DOWN`.
2. Main thread leaves its `shutdown_event.wait()` and calls
   `BotLoop.stop(inflight_timeout_sec=5)`.
3. `stop()` waits up to 5 seconds for `_inflight_count` to reach 0
   (in-flight broker calls finishing) — prevents mid-amend state
   desync.
4. `position_manager.save_if_dirty()` flushes state.
5. `feed_manager.stop()` releases LS connection.

Exit codes: `0` clean, `1` pre-flight failure, `2` runtime crash.

## Does NOT own

- Indicator math (`indicators/`)
- Structure detection (`structure/`)
- Regime classification (`regime/`)
- Risk rules and EOD policy (`risk/`)
- Order placement and SL evaluation (`execution/`)
- Market data ingress and persistence (`feed/`)
- Broker REST connectivity (`feed/ig_rest/`)

Phase 8 is pure composition + scheduling — every domain decision
belongs to its respective module.

## v1 simplifications

- `AccountState.balance` defaults to 10000 GBP and `realized_pnl_today_r`
  to 0.0. Phase 9+ will pull balance from the broker and maintain a
  realized-PnL ledger via reconciliation events.
- H1 candles are resampled from the M5 rolling buffer on every
  BAR_CLOSE (`df_m5.resample("1h")`). v1's `RollingBuffer` is M5-only;
  storing H1 separately is Phase 9+.
- Single-pair regime engine per pair (no cross-pair correlation
  logic; that's v2).
