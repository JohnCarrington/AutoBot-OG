# Phase 9 integration commit 2a (`feature/alerts-integration`) — adversarial review

- **Branch reviewed:** `feature/alerts-integration` — **uncommitted on
  the branch**. The work is staged-but-not-committed on top of
  `develop` @ `ef95671`.
- **Scope:** alerts module refinements only. M1–M5, L1–L5, N1 from
  the Phase 9 commit 1 adversarial review. No integration changes
  (Phase 6/7/8 callers untouched).
- **Date:** 2026-05-15
- **Reviewer:** AutoBot-OG (read-only audit pass)
- **Test status:** `808 passed in 4.06s` — matches the commit
  message's claim (`776 + 32 new`).

---

## Headline

**One HIGH severity finding and one MEDIUM that both originate from
the same root cause: the N1 logging filter handles message bodies but
not exception tracebacks, and the L1 timestamp formatter doesn't
normalise to UTC.** Together they re-introduce the exact failure
modes the items were supposed to defend against.

- **H1 (HIGH — defense-in-depth incomplete):** the new
  `_TokenScrubFilter` in `src/alerts/__init__.py` operates on
  `record.getMessage()` only. The cached traceback text appended by
  `Formatter.formatException` (via `record.exc_info` →
  `record.exc_text`) is NOT scrubbed. Empirically verified — a
  `logger.exception()` call with a token-bearing exception still
  leaks the token through the traceback. The whole point of N1 was
  to defend the `logger.exception("TelegramClient.send raised
  unexpectedly")` path in `alerter.py:265`. The implementation as
  shipped does not cover that path.

- **M1 (MEDIUM — L1 timezone correctness):** `_format_timestamp_suffix`
  unconditionally appends the literal string `"UTC"` after
  `alert.timestamp.strftime('%H:%M:%S')` without converting to UTC.
  If a caller passes a non-UTC timestamp (e.g. a future Phase 10
  health-check piping in a broker-local datetime), the operator
  reads a wrong-but-confidently-labelled-UTC time.

Everything else verifies. The M1 / M2 / M5 / L5 / N1 work landed as
specified: severity is in the coalesce key, the clock-backwards
defence flushes on negative elapsed, control characters are
sanitised cleanly, send-after-close is properly gated, and the
filter installation is idempotent and correctly scoped.

The CRITICAL-bypass prefix-match fix is **correct and well-tested** —
the WARNING+CRITICAL same-subtype scenario flushes both severities
ahead of the CRITICAL, preserving timeline. Cross-pair isolation is
preserved (a CRITICAL on GBPUSD does not sweep a pending EURUSD of
the same subtype). The CRITICAL-after-elapsed scenario from M4 is
explicitly tested.

**Recommendation: APPROVE WITH CONDITIONS** — fix H1 (a 5-line
addition to the filter to scrub `record.exc_text` after
pre-formatting the exception) before merging. M1 is a small fix that
should land in the same commit; it's a behavioural correctness issue
on operator-facing output. The LOW items below can defer to commit 2b.

There is also one process finding (P1) about the uncommitted state
of the branch — please commit before merge.

---

## Findings

### H1 (HIGH — token leak via `logger.exception` traceback) — the N1 filter ignores `record.exc_info` / `record.exc_text`

**File:** `src/alerts/__init__.py:96-110`

```python
class _TokenScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:
            return True
        scrubbed = _scrub_exception_text(text)
        if scrubbed != text:
            record.msg = scrubbed
            record.args = ()
        return True
```

`record.getMessage()` returns `record.msg % record.args` — the
**formatted message body only**. It does NOT include the traceback
that `logger.exception()` (or any `logger.X(..., exc_info=True)`)
attaches via `record.exc_info`. The traceback is rendered later by
`Formatter.format()`:

```python
# Stdlib logging/__init__.py (simplified)
def format(self, record):
    s = self.formatMessage(record)              # uses record.getMessage()
    if record.exc_info:
        if not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        s = s + "\n" + record.exc_text
    return s
```

At the time the filter runs, `record.exc_text` is `None`. After
filters complete, `Formatter.format` populates `record.exc_text` from
`record.exc_info` — bypassing the scrub.

Empirically reproduced:

```
ERROR alerts.alerter: upstream call raised
Traceback (most recent call last):
  File "<stdin>", line 13, in <module>
RuntimeError: failed at /bot12345:LIVETOKEN/sendMessage

--- Token in output: True
```

**The whole point of N1** was to defend the
`alerter.py:265` defense-in-depth path:

```python
try:
    ok = self._client.send(text)
except Exception:
    # TelegramClient.send already swallows; this is defense in
    # depth in case a future change makes it raise.
    logger.exception("TelegramClient.send raised unexpectedly")
```

A future regression that removes the try/except in
`TelegramClient.send` would re-raise a `requests.exceptions.ConnectionError`
through this `logger.exception` — and the filter as implemented
would NOT scrub the URL/token in the traceback. **H1 from the
original review re-surfaces verbatim**.

The original review's suggested fix included `record.exc_text`
handling explicitly:

```python
if record.exc_text:
    record.exc_text = _scrub_exception_text(record.exc_text)
```

That line is missing from the implementation. But at filter time
`exc_text` is usually `None` (not yet formatted), so the fix has to
**pre-format** the exception and store the scrubbed result:

**Suggested fix:**

```python
def filter(self, record: logging.LogRecord) -> bool:
    try:
        text = record.getMessage()
    except Exception:
        return True
    scrubbed = _scrub_exception_text(text)
    if scrubbed != text:
        record.msg = scrubbed
        record.args = ()
    # Pre-format any attached exception so we can scrub the traceback
    # before Formatter.format would otherwise inline it.
    if record.exc_info and not record.exc_text:
        import traceback
        formatted = "".join(traceback.format_exception(*record.exc_info))
        record.exc_text = _scrub_exception_text(formatted)
    elif record.exc_text:
        # A handler already cached exc_text — scrub it in place.
        record.exc_text = _scrub_exception_text(record.exc_text)
    return True
```

Add a regression test parallel to the existing `test_filter_redacts_token_in_log_message`:

```python
def test_filter_redacts_token_in_logger_exception_traceback(caplog) -> None:
    log = logging.getLogger("alerts.alerter")
    try:
        raise RuntimeError("failed at /bot1234:SECRET/sendMessage")
    except Exception:
        log.exception("upstream raised")
    # The cached exc_text on the captured record should be scrubbed.
    rec = [r for r in caplog.records if "upstream raised" in r.getMessage()][0]
    assert "1234:SECRET" not in (rec.exc_text or "")
    assert "<redacted>" in (rec.exc_text or "")
```

(Note: `caplog` may need a `propagate=True` handler attached to
trigger the format pass that populates `exc_text`. The test should
flush the handler explicitly via `caplog.handler.format(rec)` to
force formatException.)

---

### M1 (MEDIUM — L1 timezone correctness) — formatter labels every timestamp `"UTC"` without converting

**File:** `src/alerts/formatter.py:73-79`

```python
def _format_timestamp_suffix(alert: Alert) -> str:
    if alert.timestamp is None:
        return ""
    return f" ({alert.timestamp.strftime('%H:%M:%S')} UTC)"
```

`strftime('%H:%M:%S')` honours the datetime's tzinfo for its rendered
hour/minute/second — but only as the LOCAL representation in that
tz. The string `"UTC"` is appended unconditionally, so a non-UTC
timestamp renders as "wrong hour but labelled UTC".

Empirical reproduction:

```
JST timestamp 22:00 JST (= 13:00 UTC):
  rendered: 'ℹ️ TRADE_OPENED — bullish (22:00:00 UTC)'
  → header says 22:00 UTC, but the actual UTC time is 13:00
```

The `Alert` dataclass docstring states "All time fields are
timezone-aware datetime in UTC" — so this is "callers SHOULD pass
UTC". But the formatter does not enforce or convert, and a future
Phase 10 callsite (health check, broker-time correlations) could
trivially trip this without realising. Operator reads the wrong time
during incident triage.

**Suggested fix:** force UTC inside the formatter.

```python
from datetime import timezone

def _format_timestamp_suffix(alert: Alert) -> str:
    if alert.timestamp is None:
        return ""
    ts_utc = alert.timestamp.astimezone(timezone.utc)
    return f" ({ts_utc.strftime('%H:%M:%S')} UTC)"
```

Edge case: a tz-NAIVE datetime would raise from `.astimezone()` in
Python ≥3.6 (it assumes local tz on naive inputs, which is wrong
here). Detect and either coerce-as-UTC or fail loudly:

```python
ts = alert.timestamp
if ts.tzinfo is None:
    # Convention: naive == UTC. Coerce to be safe.
    ts = ts.replace(tzinfo=timezone.utc)
ts_utc = ts.astimezone(timezone.utc)
```

Add a test that constructs a non-UTC `Alert.timestamp` and verifies
the rendered suffix is the UTC equivalent.

---

### M2 (MEDIUM — `tick()` after `close()` silently no-ops while `send()` rejects loudly) — observability asymmetry

**Files:** `src/alerts/alerter.py:208-213` (`tick`), `:175-187` (`send`)

```python
def send(self, alert):
    if not self._enabled or self._coalescer is None:
        return
    if self._coalescer.closed:
        logger.warning("TelegramAlerter.send called after close - alert dropped...")
        return
    ...

def tick(self):
    if not self._enabled or self._coalescer is None:
        return
    try:
        batches = self._coalescer.tick()
    except Exception:
        logger.exception(...)
        return
    for batch in batches:
        self._deliver(batch)
```

`send()` after `close()` logs a WARNING; `tick()` after `close()`
silently no-ops (the coalescer's pending dict is empty post-drain,
so `tick()` finds nothing to flush and returns `[]`). For
observability parity, `tick()` should also surface a `closed` state
— either via the same WARNING, or at minimum a DEBUG log.

Severity is MEDIUM not LOW because a Phase 8 BotLoop that calls
`tick()` after `close()` is the realistic shutdown-race pattern:
the LS reader thread fires one final FEED_STALE between the
BotLoop's `alerter.close()` call and the LS subscriber's actual
disconnect. The current code silently swallows; the operator has no
"alerter was closed when this fired" breadcrumb.

**Suggested fix:**

```python
def tick(self):
    if not self._enabled or self._coalescer is None:
        return
    if self._coalescer.closed:
        logger.debug("TelegramAlerter.tick called after close - no-op")
        return
    ...
```

DEBUG (not WARNING) is appropriate here — tick is a heartbeat call,
not an alert-bearing one. The post-close case is benign.

---

### M3 (MEDIUM — em-dash audit incomplete) — `coalescer.py` docstrings still use em-dashes

**Files:** `src/alerts/coalescer.py:5-9, 53-58, 132-138, 168-172,
181-184, 213-221`

The commit description says "em-dash to hyphen audit, log strings
converted, formatter user-visible kept". Spot-check:

- `telegram_client.py` log strings → ASCII hyphen ✓ (diff verified)
- `alerter.py` log strings → ASCII hyphen ✓
- `coalescer.py` **docstrings** still contain em-dashes (line 6:
  "...locked in the Phase 9 plan refinement) — so a single..."; line
  168: "Returns the list of newly-ready batches in arbitrary order
  (dict iteration order, which is insertion order). Called
  opportunistically by the alerter — typically from...").

Docstrings aren't user-visible / log-visible — they only appear in
help() / pydoc output. So this isn't a correctness issue. But the
commit message implies a complete sweep, and the sweep is partial.
Either complete it or scope the claim ("log strings only").

**Suggested fix:** run `grep -n "—" src/alerts/` and either replace
all remaining em-dashes with `--` or `-`, or note in the commit
message that the sweep was scoped to log strings (not docstrings).

---

### M4 (MEDIUM — filter does not auto-install for future submodules) — `_SCRUBBED_LOGGER_NAMES` is a static list

**File:** `src/alerts/__init__.py:55-61`

```python
_SCRUBBED_LOGGER_NAMES = (
    "alerts",
    "alerts.alerter",
    "alerts.coalescer",
    "alerts.formatter",
    "alerts.telegram_client",
)
```

A future `alerts.X` submodule (e.g. a `slack_client.py` for
multi-channel routing) would get its own logger
(`logging.getLogger("alerts.slack_client")`) that is NOT covered by
the static list. Loggers inherit from parents in propagation but
filters are scoped to the logger they're attached to — propagated
records DON'T re-run parent filters.

Wait — Python logging actually says filters on the parent logger are
NOT applied to child records. So a record on `alerts.slack_client`
that propagates to the `alerts` logger would be filtered by the
`alerts` logger's handlers — but the `alerts` filter only fires for
records logged TO `alerts` directly, not for child records that
propagate up. Per [the stdlib docs](https://docs.python.org/3/library/logging.html#logging.Logger.addFilter):
"Filters are only applied to the immediate logger, not to its
children".

Wait, that's actually not quite right either — the propagation model
is: child logger emits → child handlers fire → record then propagates
to parent → parent's handlers fire (parent's filters DON'T apply to
propagated records).

So: a filter on the `alerts` parent logger does NOT cover child
loggers. The filter must be on the SPECIFIC logger where the record
originates. The static list is the right approach — but new
submodules require manual registration.

This is a process risk, not a current bug. Worth a sentinel test
that fails if a new `alerts.*` module is added without updating the
list — but that test is meta-programmatic and brittle. Better: a
single comment in `__init__.py` explaining the requirement, plus a
PR-template checklist item.

**Suggested fix:** add an explicit comment near
`_SCRUBBED_LOGGER_NAMES`:

```python
# Every new alerts.* submodule that calls logger.exception or
# logs URLs MUST add its logger name here. The filter is scoped
# to specific loggers (filters on parents don't apply to propagated
# child records, per stdlib logging semantics).
```

---

### M5 (MEDIUM — DEL (0x7F) not stripped by sanitiser) — control character escapes the M5 scrub

**File:** `src/alerts/formatter.py:53-58`

```python
_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
```

The pattern explicitly stops at `0x1F`. DEL (`0x7F`) is a C0 control
character that survives. Empirical:

```
0x7f (DEL): survives=True
mix: 'GBPUSD\x7f'
```

The M5 motivation (per docstring) was to prevent ANSI escapes and
bell chars from reaching Telegram. ANSI escapes start with ESC
(`0x1B`) so the current pattern catches them. DEL is less of an
attack vector — most Telegram clients render it as nothing or as a
replacement char — but it's a control char by spec and survives the
"sanitise control characters" pass.

**Severity is MEDIUM (not LOW)** because the docstring says "ASCII
control chars" and "control" by Unicode definition includes DEL.
The implementation diverges from the documented contract.

**Suggested fix:** add `\x7f` to the pattern:

```python
_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
```

Add a one-line test:

```python
def test_format_single_strips_delete_char() -> None:
    text = AlertFormatter.format_single(_alert(full_text="be\x7ffore"))
    assert "\x7f" not in text
```

---

### P1 (PROCESS — branch state) — Phase 9 commit 2a is uncommitted on `feature/alerts-integration`

**File:** the entire diff lives in the working tree, not in a commit.

```
On branch feature/alerts-integration
Changes not staged for commit:
  modified:   src/alerts/__init__.py
  modified:   src/alerts/alerter.py
  modified:   src/alerts/coalescer.py
  modified:   src/alerts/formatter.py
  modified:   src/alerts/telegram_client.py
  modified:   src/alerts/types.py
  modified:   tests/unit/test_alerts_alerter.py
  modified:   tests/unit/test_alerts_coalescer.py
  modified:   tests/unit/test_alerts_formatter.py
  modified:   tests/unit/test_alerts_types.py

Untracked files:
  tests/unit/test_alerts_init.py
```

The Phase 8 review flagged exactly this — "no feature branch, no PR
artifact, no rollback target" — as a HIGH-severity process finding.
This time the branch exists, but the commit doesn't. Same downstream
effect: a future regression has no commit boundary to bisect against.

**Suggested fix:** commit the working tree as `feat(alerts): commit 2a
— bundle M1-M5/L1-L5/N1 from Phase 9 review` before merging to
`develop`. Same pattern as every prior phase.

Severity tagged as PROCESS rather than HIGH because it's about
workflow hygiene, not code correctness — but it does affect
reviewability and ops rollback in the same ways the Phase 8 H2
finding did.

---

### L1 (LOW — filter doesn't scrub `args` for non-string elements) — defensive but partial

**File:** `src/alerts/__init__.py:96-110`

The filter scrubs `record.getMessage()` (which formats `msg % args`)
but if a custom handler bypasses `getMessage()` and reads
`record.args` directly, it would see the pre-scrub values. The
filter sets `record.args = ()` only when `scrubbed != text` —
otherwise the original args survive.

For records that DIDN'T contain a token, the args are kept (correct).
For records that DID, the args are cleared (correct — the scrubbed
text is already pre-formatted into `record.msg`). So a non-standard
handler reading args would either see clean args (no token) or empty
args (token-scrubbed) — never the leaking case.

Probably fine. Worth a sentence in the filter docstring confirming
this is intentional.

---

### L2 (LOW — `_truncate_for_log` doesn't truncate at exactly `max_len`) — off-by-three in the boundary

**File:** `src/alerts/alerter.py:66-71`

```python
def _truncate_for_log(text: str, max_len: int = _DELIVERY_LOG_MAX_LEN) -> str:
    one_line = text.replace("\n", "\\n")
    if len(one_line) <= max_len:
        return one_line
    return one_line[: max_len - 3] + "..."
```

Returns `max_len` chars total (max_len-3 char prefix + 3-char "...").
Standard truncation pattern. The newline-to-`\n` replacement happens
BEFORE the length check — so the displayed length is correct, but a
1-char `\n` becomes a 2-char `\\n` literal. A text that was 161 chars
with one `\n` becomes 162 chars after replacement, which triggers
truncation. Edge case but worth a docstring note.

---

### L3 (LOW — DEBUG log emits even on `close()` deliveries) — observability noise

**File:** `src/alerts/alerter.py:267-277`

The DEBUG "delivered" log fires inside `_deliver`, which is called
from `send`, `tick`, AND `close`. So on shutdown the close drain
produces a DEBUG line per batch. Probably fine — DEBUG is
suppressed in production — but it does mean shutdown logs include
N alert-delivery records that operators might not expect.

Worth a one-line note in the close docstring that delivery logs fire
during the drain.

---

### L4 (LOW — coalescer `close()` semantics imply "no more send" but `tick()` allowance is undocumented)

**File:** `src/alerts/coalescer.py:206-214`

The `close()` docstring says "the alerter will refuse subsequent
`send()` calls" — but doesn't address `tick()` or `drain_all()`.
After `close()`, a caller could theoretically still call `tick()`
(which would no-op against an empty pending dict) or `drain_all()`
(which would also no-op). The behaviour is benign but undocumented.

**Suggested fix:** extend the docstring:

```python
def close(self) -> None:
    """Mark the coalescer closed. Idempotent.

    After close():
    - send() via the alerter is rejected with a WARNING.
    - tick() and drain_all() are still callable but no-op (pending
      is already drained by the alerter's close() sequence).
    """
```

---

### L5 (LOW — `_DELIVERY_LOG_MAX_LEN = 160` is a magic number) — could share with `_MAX_TEXT_LOG_LEN = 200`

**File:** `src/alerts/alerter.py:61` (`_DELIVERY_LOG_MAX_LEN`)
vs `src/alerts/telegram_client.py:36` (`_MAX_TEXT_LOG_LEN`)

Two truncation limits in two different files for similar purposes:

- `_DELIVERY_LOG_MAX_LEN = 160` — DEBUG success log
- `_MAX_TEXT_LOG_LEN = 200` — WARNING failure log

Different values (160 vs 200) for similar purposes. Either justify
the asymmetry in comments or unify to one constant in `constants.py`.

---

## Spec walkthrough

| Item | Status | Verified by |
|------|--------|-------------|
| M1 — severity in coalesce key tuple | ✓ | `test_coalesce_key_tuple_shape`, `test_coalesce_key_distinguishes_severity`, `test_same_subtype_different_severity_does_not_coalesce` |
| M1 — CRITICAL bypass prefix-sweep | ✓ | `test_critical_flushes_same_prefix_pending_across_severities` + empirical 3-batch walk |
| M1 — cross-pair CRITICAL isolation | ✓ | `test_critical_does_not_flush_unrelated_subtype_pending` + empirical GBPUSD/EURUSD walk |
| M2 — clock-backwards flush | ✓ | `test_clock_backwards_treated_as_elapsed_flushes_pending` + empirical 1-day jump |
| M3 — test comment fixed | ✓ | `test_format_batch_uses_first_alerts_severity_in_header` docstring rewritten |
| M4 — CRITICAL after same-key elapsed | ✓ | `test_critical_arriving_after_same_key_window_elapsed` (2 batches in correct order, pending=0) |
| M5 — control-char sanitisation | partial (see M5 finding above re: DEL 0x7F) | `test_format_single_strips_ascii_control_chars`, others |
| L1 — timestamp surfacing | partial (see M1 finding above re: timezone) | `test_format_single_appends_timestamp_when_present` |
| L2 — TelegramClient docstring | ✓ | docstring updated |
| L3 — DEBUG success log | ✓ | `test_deliver_logs_debug_on_successful_send`, `test_deliver_does_not_log_debug_on_client_returning_false`, `test_deliver_debug_log_collapses_newlines_for_single_line` |
| L4 — em-dash to hyphen (logs) | ✓ | log strings converted (formatter user-visible kept correctly) |
| L5 — send after close | ✓ | `test_send_after_close_is_rejected_and_does_not_raise`, `test_close_drains_then_marks_closed`, `test_close_is_idempotent_after_first_call` |
| N1 — token-scrub filter | partial (see H1 above) | `test_filter_*` covers message body; no test covers exc_info traceback |

---

## Test-quality observations

- **`test_alerts_init.py`** imports private names
  (`_SCRUBBED_LOGGER_NAMES`, `_TokenScrubFilter`,
  `_install_token_scrub_filter`) — couples the test to internal
  identifiers. Acceptable since the test sits inside this package's
  test suite, but should be documented as "internal" in the
  filenames.
- **Test collection order:** alphabetical, so `test_alerts_init.py`
  runs *after* `test_alerts_alerter.py`/`coalescer.py`/`formatter.py`.
  The filter is installed at `import alerts` (first import), so
  earlier tests see the filter installed. No state leakage observed.
- **No test for the filter idempotency under module reload** —
  `importlib.reload(alerts)` would call `_install_token_scrub_filter()`
  again, which the sentinel correctly guards. Worth a one-line test.
- **No test for the L3 DEBUG log path when the alerter is in no-op
  mode** — but the code path returns before `_deliver`, so this is
  trivially correct.
- **No test for the M2 backwards-clock case via `add()`** with a
  CRITICAL alert in the same call — but the code path is the same as
  the `tick()` case, which is tested.

---

## Final recommendation

**APPROVE WITH CONDITIONS.**

Fix H1 (the missing `record.exc_text` scrubbing — 5-line addition
to the filter plus one regression test) and M1 (the formatter's
unconditional `"UTC"` label — 2-line `.astimezone(timezone.utc)`
plus one test) before committing 2a and merging to `develop`. Both
are small, contained fixes that complete the work the items
purported to do.

The MEDIUM items M2/M3/M4/M5 are quality-of-life or partial-
implementation issues that can land in commit 2b's integration pass.
The LOW items (L1–L5) are polish.

P1 (the uncommitted branch state) is a process issue — please
commit before merge. The Phase 8 review surfaced the same pattern;
maintaining feature-branch + commit + review hygiene matters for
ops rollback and bisect.

The work delivered is otherwise sound: M1 severity-in-key is right,
the CRITICAL prefix-match fix is correct and tested, M2 clock-skew
defence is well-implemented, M5 control-char sanitisation works
(modulo DEL), L5 send-after-close gating is clean. Once H1 and M1
land, this is a clean APPROVE FOR MERGE.
