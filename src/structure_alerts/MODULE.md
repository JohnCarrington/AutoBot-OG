# structure_alerts

Phase 12 — Structure-transition alerting. Produces a closed set of
nine event kinds from the Structure Engine's per-bar `StructureState`,
gates them by severity-aware dedupe cooldowns, and hands them off to
the Phase 9 `TelegramAlerter` for delivery.

## Owns

C-1 ships only the catalogue. Subsequent commits add modules:

- `types.py` — `AlertEventKind` (closed catalogue of nine events) and
  `AlertEvent` frozen dataclass. `severity_for()` is the locked
  kind→severity mapping.
- `constants.py` — cooldown tunables (INFO 2h / WARNING 1h /
  CRITICAL 30m), `STRUCTURE_ALERTS_LOG_PATH`,
  `quantise_price()` (pip-integer quantisation for dedupe keys).
- `diff.py` (C-2) — `compute_structure_diff(prev, curr)`.
- `triggers.py` (C-2) — `changes_to_events(...)` mapping spec §7 A–H.
- `dedupe.py` (C-3) — `DedupeCache` with severity-based cooldowns.
- `persistence.py` (C-3) — append `AlertEvent` to
  `data/alerts/structure_alerts.jsonl`.
- `hydration.py` (C-4) — `load_latest_structure_state_per_pair(path)`
  rehydrating from `data/structure/structure_state.jsonl` so the
  `_previous_structure` cache survives restarts.
- `summary.py` (C-5) — `build_hourly_summary(state)` per spec §11.
- `alert_translator.py` (C-5) — Phase 12 `AlertEvent` →
  Phase 9 `alerts.Alert`.
- `processor.py` (C-5) — orchestrates diff → triggers → dedupe →
  persistence and returns the events that survived dedupe.
- BotLoop wiring (C-6) — `_previous_structure: dict[str, StructureState | None]`
  and `_structure_dedupe: DedupeCache`, populated in `hydrate()` and
  consulted in `_handle_bar_close`.

## Event catalogue

| Kind | Severity | Spec §7 |
|---|---|---|
| `HTF_BIAS_CHANGE` | WARNING | A |
| `STRUCTURE_MODE_CHANGE` | WARNING | B |
| `SUPPORT_ACCEPTANCE_BREAK` | CRITICAL | C |
| `RESISTANCE_ACCEPTANCE_BREAK` | CRITICAL | D |
| `SWEEP_RECLAIM` | WARNING | E |
| `FAILED_RECLAIM` | WARNING | F |
| `NEW_MAJOR_LEVEL` | INFO | G |
| `LEVEL_INVALIDATED` | INFO | H |
| `HOURLY_SUMMARY` | INFO | §11 |

Severity is locked at the kind level — every construction site reads
`severity_for(kind)` so the field on `AlertEvent` cannot drift from
the catalogue. CRITICAL events bypass the Phase 9 coalescer (same
rule that already applied to `FAILURE_THRESHOLD_TRIPPED` in Phase 9)
but still pass through the structure-alerts dedupe cache first.

## Dedupe and cooldowns

`DedupeCache` (C-3) keys on the event's `dedupe_key` string. The
cooldown applied depends on the event's severity:

- INFO — 2 hours
- WARNING — 1 hour
- CRITICAL — 30 minutes

Restart resets the cache; the `_previous_structure` rehydration
(C-4) prevents most spurious post-restart re-fires by ensuring the
first post-restart bar's diff has the right "previous" state to
compare against.

Dedupe-key formats per kind (full table lives in C-2 `triggers.py`
docstrings, anchored here for reviewers):

| Kind | Key format |
|---|---|
| `HTF_BIAS_CHANGE` | `{pair}_HTF_BIAS_{curr_bias}` |
| `STRUCTURE_MODE_CHANGE` | `{pair}_MODE_{curr_mode}` |
| `SUPPORT_ACCEPTANCE_BREAK` | `{pair}_SUPPORT_ACCEPTANCE_{Q(price)}` |
| `RESISTANCE_ACCEPTANCE_BREAK` | `{pair}_RESISTANCE_ACCEPTANCE_{Q(price)}` |
| `SWEEP_RECLAIM` | `{pair}_SWEEP_RECLAIM_{side}_{Q(price)}` |
| `FAILED_RECLAIM` | `{pair}_FAILED_RECLAIM_{side}_{Q(price)}` |
| `NEW_MAJOR_LEVEL` | `{pair}_NEW_LEVEL_{side}_{Q(price)}` |
| `LEVEL_INVALIDATED` | `{pair}_LEVEL_INVALIDATED_{side}_{Q(price)}` |
| `HOURLY_SUMMARY` | `{pair}_HOURLY_SUMMARY_{ISO-hour-bucket}` |

`Q(price)` is `quantise_price(pair, price)` — integer pip count using
`config.pair_config.pip_size_for(pair)`.

## Heartbeat semantics

Hourly summary fires from M5 BAR_CLOSE at `close_time.minute == 0`.
If feed gaps cause the top-of-hour bar to be missed, that hour's
summary will not fire.

Absence of an hourly summary MAY indicate any of:

- Bot outage
- Feed outage
- Missing M5 close at the top of the hour
- Market inactivity (weekends, holidays, broker pauses)
- DST or scheduled maintenance window

Operational rule for investigation:

  Missing summary + no FEED_STALE/GAP_FILLED + no recent trade alerts → investigate.
  Missing summary alone is not a definitive outage signal.

The bot does NOT actively claim health via hourly summary; the
summary is structure observability that doubles as a passive
availability signal. The combined picture across multiple alert
streams (heartbeat summaries, feed-state events, trade lifecycle)
is the real liveness check.

## Integration with Phase 9 alerts

Phase 12 produces `AlertEvent` (its own dataclass). The C-5
translator converts each survived event to `alerts.Alert` with:

- `category = AlertCategory.STRUCTURE` (added to Phase 9 catalogue
  in C-1)
- `event_subtype = kind.value` (added to `EVENT_SUBTYPES` in C-1)
- `severity = severity_for(kind)`
- `timestamp = event.timestamp`
- `debug = {**event.debug, "dedupe_key": event.dedupe_key}`

The Phase 9 coalescer's key is `(category, event_subtype, pair, severity)`,
so STRUCTURE alerts naturally coalesce alongside (but never with)
TRADE / RECONCILIATION / SYSTEM alerts.

## Tests

C-1 ships:

- `tests/unit/structure_alerts/test_types.py` — closed-set
  assertion, severity mapping coverage, frozen dataclass, debug
  dict isolation.
- `tests/unit/structure_alerts/test_constants.py` — cooldown
  defaults, env override, quantisation for four-decimal and
  two-decimal pairs.
- Extension of `tests/unit/test_alerts_types.py` — `STRUCTURE`
  category present, full 25-entry `EVENT_SUBTYPES` set asserted.

Per-kind diff/trigger tests land in C-2; dedupe + persistence tests
in C-3; hydration tests in C-4; processor + translator tests in C-5;
BotLoop integration test in C-6.
