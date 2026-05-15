# Phase 9 commit 1 (`feature/alerts` @ `97617a3`) — adversarial review

- **Branch reviewed:** `feature/alerts` @ `97617a3` — module only, no
  Phase 6/7/8 integration.
- **Base:** `develop` @ `c105057`.
- **Date:** 2026-05-15.
- **Reviewer:** AutoBot-OG (read-only audit pass).
- **Test status:** `776 passed in 3.97s` — full suite green, matches
  the commit claim.

---

## Headline

**One HIGH-severity credential leak; otherwise the module is in
good shape.** The coalescer's semantics are correct (window
inclusive at `>=`, CRITICAL bypass preserves timeline ordering,
elapsed-side-effect drain on every `add()`/`tick()`), the formatter
truncation count math is right, the no-op mode short-circuits cleanly,
and the three-layer exception isolation contract holds — every
failure path I exercised was caught, including the
"client-raises-during-pending-flush-before-CRITICAL" combination.

The one HIGH finding (H1 below): when `requests.post` raises a
real `requests.exceptions.ConnectionError`, the exception's
`__str__` typically contains the **full request URL** including
`/bot{token}/sendMessage`. The TelegramClient logs `"%s: %s" % (type(exc), exc)`
at WARNING — which exposes the bot token in production logs on
every network failure (DNS down, connection refused, max-retries-
exceeded). The test that triggers a `ConnectionError` uses a synthetic
short message that doesn't contain the URL, so the suite doesn't
catch this. Bot tokens grant full posting access to the chat; if log
files are shipped to a central aggregator, a third party (anyone
with read access to logs) can post messages as the bot.

Remaining findings are MEDIUM (severity-vs-event_subtype mismatch
not enforced, no clock-going-backwards defence, comment-vs-code drift)
and LOW (polish items).

**Recommendation: APPROVE WITH CONDITIONS** — fix H1 before
merging, since it's a security-relevant credential leak. The
MEDIUM/LOW items can land as follow-ups in the commit-2
integration pass.

---

## Findings

### H1 (HIGH — credential leak in logs) — `TelegramClient` exception logging includes the bot token

**File:** `src/alerts/telegram_client.py:99-105`

```python
except Exception as exc:
    logger.warning(
        "Telegram delivery failed (%s: %s) — alert text: %s",
        type(exc).__name__,
        exc,                  # ← str(exc) for many requests exceptions contains the URL
        _truncate(text),
    )
    return False
```

For `requests.exceptions.ConnectionError`, the exception's
`__str__` typically renders as:

```
ConnectionError: HTTPSConnectionPool(host=api.telegram.org, port=443):
  Max retries exceeded with url: /bot{TOKEN}/sendMessage (Caused by ...)
```

(verified empirically with a constructed `requests.exceptions.ConnectionError`
whose message contains the URL path.)

Reproduced:

```
ConnectionError: HTTPSConnectionPool(host=api.telegram.org, port=443):
  Max retries exceeded with url: /bot1234567890:ABCDEF/sendMessage
  (Caused by NameResolutionError)
```

Once `requests` is the actual `post_fn` (production path) and a
real network failure happens, the WARNING line emits the
**bot-token-bearing path** into the log file. Telegram bot tokens
grant posting / message-edit / chat-management privileges on any
chat the bot has been added to. Centralised log aggregators
(ELK / Loki / CloudWatch) routinely retain WARNING-level lines
indefinitely; anyone with read access can extract the token and
impersonate the bot.

**Why the tests didn't catch it:**

- `test_send_network_error_returns_false` and
  `test_send_timeout_returns_false` raise hand-constructed
  exceptions whose messages are short strings ("no route to host",
  "upstream timed out") that don't contain URLs.
- `test_send_truncates_long_text_in_failure_log` checks that the
  *alert text* doesn't appear in full, but not that the *exception
  message* is sanitised.

The production failure mode (real `requests` raising with the URL
embedded) is not exercised.

**Suggested fix:** scrub the URL out of the exception's repr
before logging, or just log the exception type without the message:

```python
except Exception as exc:
    logger.warning(
        "Telegram delivery failed (%s) — alert text: %s",
        type(exc).__name__,
        _truncate(text),
    )
    logger.debug(
        "Underlying delivery failure detail (token-scrubbed): %s",
        _scrub_token(str(exc)),
    )
    return False
```

Where `_scrub_token(msg)` replaces the path segment matching
`/bot[^/]+/` with `/bot***/`. Belt-and-braces: bump the noisy
third-party loggers (`urllib3`, `requests`) to WARNING so they
never log retry-with-URL details either. (Phase 8's
`bot.logging_setup` already does this for the prod path, but the
alerts module isn't always in a process that ran `setup_logging` —
imports happen during unit tests too.)

Add a regression test: construct an exception whose message contains
`/bot{TOKEN}/sendMessage`, fire it through the client, assert the
WARNING log does NOT contain the token.

---

### M1 (MEDIUM — design enforcement) — severity is not part of the coalesce key, but the formatter renders only the first alert's severity

**Files:** `src/alerts/types.py:109-111` (key) and
`src/alerts/formatter.py:73-75` (header)

```python
def coalesce_key(self) -> tuple[AlertCategory, str, Optional[str]]:
    return (self.category, self.event_subtype, self.pair)
```

```python
first = alerts[0]
emoji = _SEVERITY_EMOJI[first.severity]
```

The Phase 9 plan locks severity per-event_subtype (TRADE_OPENED →
INFO, FAILURE_THRESHOLD_TRIPPED → CRITICAL, etc.), but the code
does not enforce this. A caller that accidentally sends FEED_STALE
with `AlertSeverity.INFO` and then another FEED_STALE with
`AlertSeverity.WARNING` would coalesce into one batch — but the
batch's header would carry only the *first* alert's emoji and
severity. The second alert's severity is silently lost.

Worse: a CRITICAL-severity FEED_STALE (which by spec should bypass
coalescing) would correctly bypass — but if it landed *after* a
WARNING-severity FEED_STALE was pending, the bypass logic in
`AlertCoalescer.add` correctly flushes the pending. Severity check
is `alert.severity is AlertSeverity.CRITICAL`, which works
per-alert. So the bypass is robust; only the rendering of
mixed-severity batches is wrong.

The test
`test_format_batch_uses_first_alerts_severity_in_header` has a
contradictory comment:

```python
"""Coalesce key includes severity implicitly (same subtype → same sev)
but assert explicitly since the header reads off [0]."""
```

The comment is wrong — the key does NOT include severity. The
"by convention" is the design's defence, not the code's. A unit
test that exercises mixed-severity alerts in a batch would catch
the silent-severity-loss case.

**Suggested fix:** either

1. Add severity to the coalesce key (so mixed-severity alerts
   produce separate batches), or
2. Assert in `coalesce_key()` (or `Alert.__post_init__`) that
   severity matches the expected mapping for `event_subtype`, and
   raise on mismatch — turning "by convention" into "by code".

Option 1 is the v1-safe choice — no caller-side change required.

---

### M2 (MEDIUM — clock-going-backwards) — coalescer pending groups stall indefinitely if the system clock jumps back

**File:** `src/alerts/coalescer.py:184`

```python
if now - group.first_arrival_utc >= self._window:
```

If `now < group.first_arrival_utc` (NTP correction, daylight-saving
shift on a naive clock, container restore with skewed time), the
subtraction yields a negative `timedelta`, which is never `>=` the
positive 30-second window. The group stays pending — possibly for
hours, until the clock catches back up.

Real-world impact: rare. The alerter consumes a timezone-aware
UTC clock (`datetime.now(timezone.utc)`) so DST shifts don't
affect it; NTP smear updates are usually <100ms; only a manual
clock change or container time-travel would trip this. Worth a
defensive check, not a blocker.

**Suggested fix:** also flush when `now < group.first_arrival_utc`
(clock-skew detection):

```python
elapsed = now - group.first_arrival_utc
if elapsed < timedelta(0) or elapsed >= self._window:
    ...
```

---

### M3 (MEDIUM — comment-vs-code drift) — `test_format_batch_uses_first_alerts_severity_in_header` comment misstates the coalesce key

**File:** `tests/unit/test_alerts_formatter.py:133-134`

Already cited in M1. Cross-listing as MEDIUM in its own right
because the **comment** in a test file becomes load-bearing
documentation for future maintainers, and this one is wrong.

**Suggested fix:** replace the comment with the truth:

```python
"""Coalesce key does NOT include severity. Phase 9 relies on the
per-event_subtype severity mapping being applied by callers
(see EVENT_SUBTYPES + MODULE.md severity table). If a caller
violates that convention the header silently shows alerts[0]'s
severity — see M1 in the adversarial review."""
```

---

### M4 (MEDIUM — coalescer test coverage) — no test exercises "CRITICAL same-key whose pending group already elapsed before the CRITICAL hits"

**File:** `tests/unit/test_alerts_coalescer.py`

The CRITICAL bypass test
(`test_critical_flushes_pending_same_key_non_critical_first`) puts
the pending WARNING and the CRITICAL inside the 30s window —
exercising the "same-key pending exists and is within window"
path. Not tested: the case where the same-key WARNING window
elapsed *during* the same `add()` call, was flushed by
`_flush_elapsed`, and the CRITICAL then arrives with `_pending.pop(key, None)`
returning `None`. Behaviour is correct (verified by reading code:
the elapsed batch is in `batches` already, the CRITICAL appends
its own; total = 2 batches in correct timeline order), but
nothing pins it.

**Suggested fix:** add a one-line test:

```python
def test_critical_after_same_key_already_elapsed_orders_correctly() -> None:
    box = [_NOW0]
    c = _coalescer(box)
    c.add(_alert(severity=AlertSeverity.WARNING))         # pending
    box[0] = _NOW0 + timedelta(seconds=35)                # elapsed
    batches = c.add(_alert(severity=AlertSeverity.CRITICAL))
    # elapsed group + critical = 2 batches in order
    assert len(batches) == 2
    assert batches[0][0].severity is AlertSeverity.WARNING
    assert batches[1][0].severity is AlertSeverity.CRITICAL
```

---

### M5 (MEDIUM — formatter does not sanitise control characters in `full_text`) — Telegram may reject; alerter swallows the failure

**File:** `src/alerts/formatter.py:52`

```python
return f"{emoji} {alert.event_subtype} — {alert.full_text}"
```

If a caller embeds a NUL byte, low-ASCII control char, or invalid
UTF-8 surrogate in `full_text`, the resulting `text` payload to
the Telegram Bot API will be rejected with a 400 response. The
TelegramClient logs the 400 at WARNING and returns False — so the
bot doesn't crash, but the operator sees "Telegram API returned
non-2xx" without an obvious cause. The original alert content is
in the WARNING log (truncated to 200 chars), so reconstruction is
possible but tedious.

This is a v1 simplification (`MODULE.md`'s "Plain text only" line
acknowledges no escaping is applied); flagging because the
adversarial review prompt asked about it.

**Suggested fix:** if not now, then in commit 2 — the
`_truncate` helper can also `.encode('utf-8', errors='replace').decode('utf-8')`
the text before send, so unprintable / unencodable chars become
`?` and Telegram accepts the payload. Cost: one allocation per
send.

---

### L1 (LOW — `Alert.timestamp` is stored but never consumed) — dead field

**File:** `src/alerts/types.py:106`

```python
timestamp: Optional[datetime] = None
```

The docstring documents that callers SHOULD stamp at dispatch
time so "the alert order matches the bot's view of time", but
neither the coalescer nor the formatter ever consults this field
— the coalescer's window logic uses its own injected `clock()`,
and the formatter renders no timestamp. The field is effectively
write-only.

**Suggested fix:** either consume it (e.g., include the timestamp
in the formatter's header line for CRITICAL alerts so the
operator can spot replay-vs-live) or remove the field.

---

### L2 (LOW — `TelegramClient` docstring overstates what construction allows) — empty-credential constructor would crash on first send

**File:** `src/alerts/telegram_client.py:46-50`

```python
bot_token, chat_id : str
    Credentials. Empty strings here would still produce a callable
    client — the caller (:py:class:`TelegramAlerter`) is expected
    to handle the no-credentials path before constructing one.
```

True that construction doesn't validate; misleading because the
URL template is `/bot{token}/sendMessage` and an empty token
produces `/bot/sendMessage` — Telegram returns 404, the client
returns False, the test fixture would still pass. So the docstring
is technically true but worth tightening:

```python
"Credentials. The TelegramClient does NOT validate that these are
non-empty — the caller (TelegramAlerter) is expected to short-
circuit the no-credentials path. Constructing with empty strings
will produce 404s on every send."
```

---

### L3 (LOW — no DEBUG-level success log) — operators have no log line confirming alerts were delivered

**File:** `src/alerts/telegram_client.py:79-115`

On success (2xx), the client returns `True` silently. Operators
inspecting "did the bot send the daily PnL alert?" must rely on
the absence of a WARNING (or check the chat). A single
DEBUG-level "Telegram delivered (status=200)" would close the
loop for log-only verification.

**Suggested fix:** add one line:

```python
logger.debug("Telegram delivered (status=%d)", status)
return True
```

DEBUG is suppressed in production by `bot.logging_setup` default
INFO, so this doesn't add noise. Operators running with
`BOT_LOG_LEVEL=DEBUG` for ops verification get the confirmation.

---

### L4 (LOW — formatter em-dash is non-ASCII) — `format_single` separator is `—` (U+2014), not `-`

**File:** `src/alerts/formatter.py:52`

```python
return f"{emoji} {alert.event_subtype} — {alert.full_text}"
```

The em-dash renders correctly in Telegram (UTF-8 is fine) and in
the test fixtures (they look for the alert text after the
separator). Just flagging because a future copy-paste into a
shell, grep, or non-UTF locale could misrender. Non-blocking.

---

### L5 (LOW — alerter `send()` after `close()` re-arms the coalescer silently) — possible alert leak if BotLoop misorders

**File:** `src/alerts/alerter.py:146-163`

`close()` calls `drain_all()` but does not disable the coalescer
afterwards. A subsequent `send()` registers in a fresh pending
group — which sits there forever because no further `tick()` /
`close()` runs. The test
`test_send_during_close_path_does_not_raise` documents this
behaviour explicitly (asserts `a.pending_count == 1`).

For v1's BotLoop wiring (close is the LAST call), this can't
happen — but the contract is implicit. If commit 2's integration
re-orders or adds a graceful-restart path, an alert could leak
silently.

**Suggested fix:** track a `_closed` flag in the alerter and
short-circuit `send()` after close, mirroring the `_enabled`
gate. One line:

```python
def close(self) -> None:
    ...
    self._closed = True

def send(self, alert: Alert) -> None:
    if not self._enabled or self._closed or self._coalescer is None:
        return
    ...
```

---

## Spec walkthrough

| Locked decision | Status |
|-----------------|--------|
| Coalesce key `(category, event_subtype, pair)` | ✓ in `types.coalesce_key()` |
| CRITICAL bypasses coalescing | ✓ tested |
| CRITICAL flushes same-key pending FIRST | ✓ tested with timeline-ordering assertion |
| 30s window inclusive at boundary (`>=`) | ✓ tested at both 29s and 30s |
| Side-effect drain on add()/tick() | ✓ tested |
| drain_all() on close() | ✓ tested |
| No-op mode if creds missing | ✓ tested (token-only, chat-only, neither) |
| Single WARNING at construction in no-op mode | ✓ tested (count == 1) |
| 5s HTTP timeout, no retry | ✓ tested via passed-through `timeout` arg |
| Plain text, no Markdown / no `parse_mode` | ✓ (Bot API POST omits `parse_mode`) |
| Severity → emoji map locked | ✓ tested per severity |
| Truncation at MAX=5 with "... and N more" | ✓ tested at N=8 → "... and 3 more" |
| Three-layer exception isolation | ✓ verified empirically (client raises during CRITICAL flush, both attempts caught) |

## Test-quality observations

- **The fixture-bug fix from commit summary** (pair=GBPUSD literal
  masking separate-message assertion) is in place at
  `test_different_pair_alerts_send_as_separate_messages` —
  the test asserts `set(pairs_in_messages) == {"GBPUSD", "EURUSD"}`
  rather than positional indexing.
- **Coalescer tests drive synthetic time via a `clock_box=[_NOW0]`
  list** — `box[0] = …` mutates in place so the lambda always
  returns the latest value. Clean pattern.
- **Network-error tests** use synthetic short messages — they
  don't exercise the production failure mode where the underlying
  `requests` exception carries the bot token in the URL. See H1.
- **`test_alerter_logs_warning_once_when_creds_missing`** asserts
  the construction warning count is 1; **no** test asserts the
  count stays at 1 after subsequent `send()` calls — but a code
  read confirms the no-op path doesn't log on send.
- **No test for severity-mismatch within a coalesced batch**
  (M1).
- **No test for clock-going-backwards** (M2).
- **No test for the elapsed+CRITICAL ordering edge** (M4).

## Final recommendation

**APPROVE WITH CONDITIONS** — fix H1 (the token-leak in
`TelegramClient` exception logs) before merging to `develop`.
The token-scrub helper + one regression test pinning the
sanitised log shape is a ~10-line patch.

The MEDIUM items (M1 severity enforcement, M2 clock-skew defence,
M3/M4 test gaps, M5 control-character sanitisation) can land
during commit 2's integration pass — none is blocking for
"module-only" use, and several are easier to validate once the
Phase 6/7/8 callers exist to exercise them in context.

The LOW items are polish.

The module's overall design is sound. Coalescer semantics are
provably correct, the formatter's truncation count is right,
the three-layer exception isolation works in practice, and the
no-op fallback removes a class of dev-environment papercuts.
Once H1 lands, this is a clean APPROVE FOR MERGE.
