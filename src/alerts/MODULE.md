# alerts

Phase 9 — outbound Telegram notifications for trade lifecycle,
reconciliation findings, and system state changes.

## Owns

- `types.py` — `Alert` frozen dataclass; `AlertSeverity` and
  `AlertCategory` enums; the closed `EVENT_SUBTYPES` tuple.
- `constants.py` — env var names (`TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`), `ALERTS_COALESCE_WINDOW_SEC=30`,
  `ALERTS_HTTP_TIMEOUT_SEC=5`, `ALERTS_MAX_BULLETS_IN_SUMMARY=5`.
- `telegram_client.py` — single-call wrapper around the Bot API
  `sendMessage` endpoint. Best-effort, no retry, never raises.
- `formatter.py` — plain-text rendering (no Markdown/HTML escaping).
  Single-alert and coalesced-batch paths; severity → emoji map.
- `coalescer.py` — `AlertCoalescer` — windowed grouping by
  `(category, event_subtype, pair)`. CRITICAL bypasses.
- `alerter.py` — `TelegramAlerter` — composition + public
  `send/tick/close` surface. No-op when credentials missing.

## Severity (locked)

| Severity | Emoji | Use |
|---|---|---|
| INFO | ℹ️ | TRADE_OPENED, TRADE_CLOSED, STARTUP, SHUTDOWN (clean), FEED_RESUMED; Phase 12 STRUCTURE: NEW_MAJOR_LEVEL, LEVEL_INVALIDATED, HOURLY_SUMMARY |
| WARNING | ⚠️ | AMEND_FAILED, BROKER_ORPHAN, MISSING_LOCAL_KEPT, MANUAL_SL_MOVE, FEED_STALE; Phase 12 STRUCTURE: HTF_BIAS_CHANGE, STRUCTURE_MODE_CHANGE, SWEEP_RECLAIM, FAILED_RECLAIM |
| CRITICAL | 🚨 | FAILURE_THRESHOLD_TRIPPED, SHUTDOWN (after crashed=True); Phase 12 STRUCTURE: SUPPORT_ACCEPTANCE_BREAK, RESISTANCE_ACCEPTANCE_BREAK |

CRITICAL is reserved for **bot-stopping** conditions only — broker
orphans require manual reconciliation but the bot keeps running,
which is why they're WARNING. CRITICAL bypassing coalescing reserves
that immediate-send channel for genuine "the bot is broken" events.

## Coalescing

Bursts of same-key non-CRITICAL alerts within
`ALERTS_COALESCE_WINDOW_SEC` (30s) collapse into one summary message.
Key = `(category, event_subtype, pair)` — bursts across pairs do
NOT collapse (a single GBPUSD TRADE_OPENED + single EURUSD
TRADE_OPENED ship as two messages).

- Single alert per key in window → verbatim
- 2–5 alerts → bullet list with `×N` header
- > 5 alerts → first 5 bullets + `... and (M-5) more`

CRITICAL alerts:

- Bypass coalescing entirely (sent immediately).
- Flush any pending same-key non-CRITICAL alerts first so the
  Telegram timeline reads in event order.

## Lifecycle

- `send(alert)` — caller registers; coalescer emits 0+ ready batches,
  each delivered as one Telegram message.
- `tick()` — drains elapsed pending groups. Called by
  `BotLoop._handle_bar_close` (per M5) and on feed transitions
  (FEED_STALE/RESUMED/GAP_FILLED) so quiet periods don't strand
  pending alerts.
- `close()` — drains every pending group regardless of age.
  Called from `BotLoop.stop` before `feed_manager.stop`.

## No-op mode

If `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` is missing, the alerter
logs one WARNING at construction and short-circuits every public
method. Local dev environments without Telegram credentials don't
need stubs.

## Threading

No internal locks; no background threads. Public methods run on the
caller's thread — typically the LS reader thread inside the Phase 8
BotLoop. v1 accepts the bounded blocking from synchronous HTTP
(`ALERTS_HTTP_TIMEOUT_SEC=5`).

## Failure isolation

The alerter never raises to its caller. Three layers of catch:

1. `TelegramClient.send` swallows HTTP exceptions, returns `bool`.
2. `TelegramAlerter._deliver` wraps the client call in try/except.
3. `TelegramAlerter.send/tick/close` wrap coalescer + formatter calls.

Alerts are observability, not control-flow — a flaky Telegram must
not affect bot correctness.

## Does NOT own

- Deciding *when* to alert — Phase 6/7/8 callers invoke; this
  module formats and ships.
- Persistence of alert history (logs are authoritative).
- Multi-channel routing (Slack, email) — v2+.
- Retry / backoff / queue persistence — v2+.
- Risk-layer circuit breaker alerts — deferred to v2.

## v1 simplifications

- In-memory pending state — bot restart drops un-sent coalesced
  groups. Acceptable because the bot restart itself emits a
  STARTUP alert and any prior unsent context is logged.
- No HTML/Markdown — Bot API call omits `parse_mode` so message
  bodies pass through unescaped. Strategy names contain `_`, pair
  symbols don't conflict with reserved characters.
- Single channel (one chat_id) — multi-chat routing is v2+.
