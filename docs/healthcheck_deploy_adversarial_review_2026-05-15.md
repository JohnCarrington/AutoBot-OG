# Phase 10 (`feature/healthcheck-deploy`) — adversarial review

- **Branch reviewed:** `feature/healthcheck-deploy` — uncommitted on
  the branch (working-tree state on top of `develop @ 0d6c96a`).
- **Scope:** healthcheck (`src/bot/healthcheck.py` +
  `healthcheck_checks.py`), `SHADOW_MODE` wiring (`src/bot/loop.py`,
  `src/bot/main.py`), alert-type additions (`src/alerts/types.py`),
  systemd units, RUNBOOK, and four new test files.
- **Date:** 2026-05-15
- **Reviewer:** AutoBot-OG (read-only audit pass)
- **Test status:** `926 passed in 4.11s` — full suite green, matches
  the diff-summary claim (846 → +80).

---

## Headline

**Two HIGH findings; the rest is polish.** Both HIGHs sit in the
gap between the SHADOW_MODE design contract ("operator turns it on,
nothing real happens at the broker") and the implementation ("only
the new-position open path is intercepted; pre-existing positions
get amends + force-closes against the real broker"). Neither is a
code bug per se — both follow directly from the locked plan ("All
other broker operations operate normally on real account
(zero-position state)"). But the *design assumes operator
discipline* (zero-position state when shadow_mode is enabled) and
the implementation *enforces nothing*. A first-time operator who
flips `BOT_SHADOW_MODE=true` on a droplet that still carries
positions from a prior live run will silently fire real broker calls
on those positions — exactly the failure mode the SHADOW_MODE
feature exists to prevent.

The second HIGH is a systemd-semantics gap: the healthcheck unit
returns exit 2 on the locked "warn" path, but the `.service` file
has no `SuccessExitStatus=0 2`, so systemd reports the unit as
`failed (Result: exit-code)`. Operator monitoring on `systemctl
is-failed` (the standard pattern) will alert on every weekday warn
exit — alert fatigue that erodes trust in the healthcheck's
non-warn signals.

The SHADOW_MODE intercept itself is correctly placed and not
bypassable from any current code path (verified by exhaustive
grep over `executor.open_from_signal` / `IGClient.open_position`
call sites). The healthcheck check functions are clean; the
exit-code aggregator is correct; the alert dispatch (when creds
are present) flows through the same hardened path the bot uses.

**Recommendation: APPROVE WITH CONDITIONS** — fix H1 + H2 before
merging. Both are 1-2 line patches plus one regression test each.
The MEDIUMs and LOWs can land in a follow-up cleanup commit on
`develop`.

---

## Findings

### H1 (HIGH — design-vs-implementation gap) — `SHADOW_MODE=true` does NOT prevent real broker calls on pre-existing positions

**Files:** `src/bot/loop.py:1035-1062` (`_run_sl_evaluation`),
`src/bot/loop.py:850-893` (`_execute_force_close`),
`deploy/RUNBOOK.md` §4 pre-flight.

The locked design says SHADOW_MODE intercepts only
`executor.open_from_signal`. The runbook §4 "Going live" pre-flight
explicitly requires zero-position state when shadow_mode is true:

> - [ ] No open positions on the IG account (broker truth is empty)
> - [ ] `data/execution/positions.json` either absent or holds an
>       empty positions list

But the bot itself enforces nothing. If an operator boots with
`BOT_SHADOW_MODE=true` on a droplet that still carries positions
from a prior live run (or copies state from the old droplet during
migration), the BAR_CLOSE pipeline still runs:

1. `_run_sl_evaluation` (`loop.py:1035`) iterates
   `self._positions.for_pair(pair)` — local state — and calls
   `self._executor.apply_amend(amend)` for any position whose
   trailing-SL evaluation suggests a move. **Real broker
   `amend_position` call.**
2. `_maybe_force_close_orders` (`loop.py:850`) → RiskGuard returns
   close orders for any position the EOD/regime rules flag →
   `_execute_force_close` calls `self._client.close_position(...)`.
   **Real broker `close_position` call.** Emits `TRADE_CLOSED` alert
   (no `[SHADOW]` marker — the operator's Telegram chat shows what
   looks like a normal closure).

Empirical reproduction (mental walk-through):

```
.env: BOT_SHADOW_MODE=true
data/execution/positions.json: 1 GBPUSD long @ 1.30050, SL 1.29900
Broker: same position open
Bot starts → STARTUP alert "🤖 BOT STARTUP [SHADOW MODE]"
(operator: "good, shadow mode is on, nothing real will happen")
First BAR_CLOSE → _run_sl_evaluation suggests trail-SL move → real broker amend ✗
EOD reached → RiskGuard returns force-close → real broker close ✗
TRADE_CLOSED alert fires (no SHADOW marker)
```

The runbook documents the expected pre-flight state but
documentation-only enforcement is the weakest form. Two well-known
deployment scenarios hit this:

- **Migration from old droplet:** operator copies `data/` over
  before flipping shadow_mode on for re-validation.
- **Resumed validation after a partial live run:** operator went
  live, opened a position, decided to roll back to shadow for a
  config change.

**Suggested fix (option A — hard refusal):** In `BotLoop.__init__`
when `shadow_mode=True`, if `self._positions.all()` is non-empty,
log CRITICAL and raise. Force the operator to clear local state
first. Same idea at the broker-fetch level on first reconciliation
— if shadow_mode AND broker reports any position, log + raise.

**Suggested fix (option B — gate the other paths too):** Wrap
`_run_sl_evaluation`'s `apply_amend` call and
`_execute_force_close`'s broker `close_position` call in the same
shadow-mode check. Emit `SHADOW_AMEND` / `SHADOW_FORCE_CLOSE`
alerts in their place. New event subtypes; symmetric to
`SHADOW_TRADE`.

**Suggested fix (option C — runbook-only):** Make the runbook §3
checklist literal command-blocks the operator must run (`systemctl
status` showing zero positions; `cat positions.json` showing empty
list) instead of bullet-checkboxes. Cheaper, less safe.

I'd recommend option A. The shadow-mode contract should be
defensive against operator error, not a polite request. The patch is
~10 lines + 2 tests.

Severity HIGH because the failure mode is "real money moves while
operator believes shadow mode is in effect" — the exact scenario
SHADOW_MODE was built to prevent.

---

### H2 (HIGH — systemd semantics) — healthcheck `.service` file lacks `SuccessExitStatus=0 2`; warn exit reports as failed unit

**File:** `deploy/systemd/autobot-og-healthcheck.service`.

The locked plan distinguishes exit 2 (warn — first-run state files
absent, no Telegram alert) from exit 1 (fail — alert dispatched).
The intent is "warn = systemd considers this success, just an
operator awareness signal."

But systemd's default `SuccessExitStatus=` is `0` only — every
non-zero exit is `failed (Result: exit-code)`. The healthcheck unit
file as-written:

```ini
[Service]
Type=oneshot
...
ExecStart=/opt/autobot-og/.venv/bin/python -m bot.healthcheck
...
TimeoutStartSec=120
```

…has no `SuccessExitStatus=` directive. Empirical consequence: a
weekday healthcheck that exits 2 (e.g., first-run, state files
absent) shows up in `systemctl status autobot-og-healthcheck.service`
as `failed`. Standard ops monitoring (`systemctl is-failed
autobot-og-healthcheck.service` returning non-zero, or
`OnFailure=…` on a parent unit, or any external uptime tool that
reads `ActiveState`/`Result`) will fire on every warn run.

This breaks the plan's stated semantic and creates exactly the
alert-fatigue pattern Phase 9 worked hard to avoid. The operator
who set up the bot to be quiet under normal operation now sees
"healthcheck failed" notifications every weekday during the first
days of deployment (when state files are still being created).

**Suggested fix:** add one line to the `[Service]` block:

```ini
SuccessExitStatus=0 2
```

This whitelists exit 2 as "success" (systemd reports the unit as
`active (exited)` with status code 2, but `is-failed` returns
non-zero only for genuine failure). Add a comment line above
explaining the warn-vs-fail split so a future maintainer doesn't
delete it.

Add a regression test: pytest can't reasonably exec systemd, but
add a simple assertion in `tests/unit/test_healthcheck_systemd_units.py`
that parses the unit file and verifies the directive is present:

```python
def test_healthcheck_service_whitelists_exit_2():
    text = Path("deploy/systemd/autobot-og-healthcheck.service").read_text()
    assert "SuccessExitStatus=0 2" in text, (
        "warn exit (2) must be whitelisted; otherwise systemd reports "
        "the unit as failed and alert-fatigues the operator"
    )
```

Severity HIGH because the locked semantic ("warn exit = systemd
success") is silently violated; the operator's experience is the
opposite of what the design promised.

---

### M1 (MEDIUM — observability) — healthcheck CRITICAL alert silently dropped if Telegram creds missing

**File:** `src/bot/healthcheck.py:73-85` (the `EXIT_FAIL` alert
dispatch path).

The `_emit_healthcheck_failed_alert` flow:

```python
alerter = TelegramAlerter()  # no-op mode if creds missing
try:
    _emit_healthcheck_failed_alert(alerter=alerter, results=results)
finally:
    alerter.close()
```

If `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` is unset (e.g.,
operator hasn't filled the .env yet, or rotated tokens and forgot
to update one), `TelegramAlerter()` runs in no-op mode and logs a
WARNING at construction. The subsequent `.send(critical_alert)`
returns silently. The CRITICAL `HEALTHCHECK_FAILED` alert is
never delivered.

The operator's failure-mode experience: `systemctl status
autobot-og-healthcheck.service` shows `failed (Result: exit-code)`
(if H2 is fixed) or just an "exit code 1" (if H2 isn't fixed and
they're not monitoring systemd specifically). No Telegram. They
walk into market open thinking everything is fine.

**Suggested fix:** add a `check_telegram_creds_present` check that
warns (not fails — Telegram-less ops is a valid mode for some
deployments) when either env var is missing. Run it FIRST in the
sequence so any subsequent fail has at least the warn line above
it in the journalctl output, alerting the operator that the
healthcheck won't be able to dispatch.

Alternatively (or additionally): in `main()`, if `EXIT_FAIL` AND
TelegramAlerter is in no-op mode, log CRITICAL with the full
healthcheck summary in the message body — at least journalctl
captures the structured failure details even when Telegram can't
deliver.

Severity MEDIUM: the CRITICAL signal goes silent in a known-bad
operator-error mode (missing/rotated Telegram creds). Not
high-risk in steady state but high-impact when it bites.

---

### M2 (MEDIUM — IG session lifecycle) — `check_ig_auth` creates a session but never logs out

**File:** `src/bot/healthcheck_checks.py:181-208`
(`check_ig_auth`).

```python
def check_ig_auth(*, session_factory=None):
    if session_factory is None:
        from feed.ig_rest.auth import create_ig_service as _default_factory
        session_factory = _default_factory
    try:
        session = session_factory()
    except Exception as exc:
        return CheckResult(name="ig_auth", status="fail", ...)
    acc_type = getattr(session, "acc_type", None)
    return CheckResult(name="ig_auth", status="pass", ...)
```

The session is created, its `acc_type` is read for the message
body, and then the function returns. The session object goes out
of scope — Python GC eventually collects it, but no explicit
`logout()` call. IG's session limits are documented (max
concurrent sessions per account); the bot's main process also
holds a session. A weekday healthcheck creates one extra session
per run, on top of the bot's persistent session.

In practice IG's session limit is generous (the demo account has
a higher limit than live). And sessions naturally expire after a
few hours. So this isn't an immediate bug — but it's a slow leak
that compounds if (a) the operator runs the healthcheck manually
multiple times during debugging, (b) a future Phase increases
healthcheck cadence (every hour? every 15min?), or (c) IG tightens
session limits.

**Suggested fix:** wrap the session in a try/finally that calls
the IG service's logout method (the v1 `IGSession`'s underlying
`trading_ig` library has a `logout()` method). Or use a
context-manager pattern if `IGSession` supports `__enter__` /
`__exit__`.

Verify against the actual `IGSession` API in
`src/feed/ig_rest/auth.py` — if it doesn't expose `logout()`,
flag as a "future cleanup" comment and pin the assumption with
a TODO referencing this finding.

Severity MEDIUM because it's a slow leak that today doesn't bite
but will under Phase 10+1 cadence changes.

---

### M3 (MEDIUM — calibration) — `journalctl_errors` threshold of 20 may false-fire on a healthy bot

**File:** `src/bot/healthcheck_checks.py:60`
(`HEALTHCHECK_JOURNALCTL_THRESHOLD: int = 20`).

A healthy bot emits ERROR-priority lines during routine operations
that aren't actual problems:

- LS reconnect cycles (each cycle: 1-3 ERROR lines from urllib3 /
  trading_ig, ~5 cycles in 24h on a typical day = 5-15 lines).
- IG REST 503/timeout → retry → success (1 ERROR per occurrence,
  several per week).
- Reconciliation transient failures that the periodic-failure
  counter shrugs off.

Threshold = 20 means a bot in normal-but-busy operation will
occasionally cross the threshold during high-noise periods (DST
transitions, IG maintenance windows, bad-network days). False
HEALTHCHECK_FAILED alerts erode operator trust.

**Suggested fix (option A — raise threshold):** bump default to 50
or 100. The signal is "is this dramatically worse than usual," not
"is there any error at all." Detecting an order-of-magnitude shift
is the actually-useful check.

**Suggested fix (option B — finer filter):** narrow the journalctl
query to `--priority=crit` (CRITICAL only). The bot emits CRITICAL
on FAILURE_THRESHOLD_TRIPPED, AMEND_PERSIST_FAILED, and SHUTDOWN
(crashed). Those are the lines that should trigger HEALTHCHECK_FAILED;
ERROR-level noise doesn't need to.

I'd recommend option B + lower threshold (e.g., 0 — any CRITICAL
in 24h is concerning). Catches the cases that actually matter and
ignores the noise.

Severity MEDIUM because the calibration is unverified against
real bot behaviour; will likely false-fire during early operations.

---

### M4 (MEDIUM — coverage gap) — healthcheck doesn't probe the bot service's own state

**Files:** `src/bot/healthcheck_checks.py` (no probe),
`deploy/systemd/autobot-og.service` (no `WantedBy=` integration
to the healthcheck).

The healthcheck verifies state files, candle archives, IG auth,
disk, journalctl, and LS endpoint — but never asks "is the bot
service supposed to be running, and is it?" Two failure modes
slip past:

1. **Bot service is `failed` but operator hasn't noticed.**
   Healthcheck shows everything else green; operator gets warn or
   pass; bot is actually down. Walks into market open thinking
   they're trading.
2. **Bot service was disabled accidentally** (`systemctl disable
   autobot-og.service` instead of `stop`). Healthcheck doesn't
   detect.

**Suggested fix:** add `check_bot_service_running` that runs
`systemctl is-active autobot-og.service` (or reads via
`systemctl show -p ActiveState`) and:

- `active` → pass.
- `inactive` → fail (bot should be running but isn't).
- `failed` → fail with the unit's last exit reason.
- `systemctl` not available (dev env) → warn.

Same dev-fallback pattern as `check_journalctl_errors`. About 30
lines + 4 tests.

This closes the most operationally common failure mode the current
suite misses.

Severity MEDIUM because it's a coverage gap, not a code bug. But
it's the gap that would catch the most real-world incidents.

---

### M5 (MEDIUM — test coverage gap) — `_build_runtime` shadow_mode threading is untested

**Files:** `tests/unit/test_bot_main_shadow_startup.py` (covers
env-read + STARTUP banner only), `src/bot/main.py:162-172`
(`_build_runtime` accepts `shadow_mode` and threads it to
`BotLoop`).

The test surface for SHADOW_MODE in `bot.main` covers:

- `_read_shadow_mode_env` (16 parametrized cases).
- `_emit_startup_alert(shadow_mode=False|True)` body shape.

What's not tested: `_build_runtime(config, *, alerter=None,
shadow_mode=False)` actually passes `shadow_mode` through to
`BotLoop.__init__`. A future refactor that drops the parameter
(or accidentally passes the wrong default) would silently break the
end-to-end shadow path — the main flow constructs the bot with the
default `shadow_mode=False` and the `[SHADOW MODE]` banner would
fire (because that's separately wired) while the actual intercept
never engages. Trades flow real.

**Suggested fix:** can't easily test `_build_runtime` end-to-end
(hits real IG SDK and Lightstreamer factory, per the existing
`test_bot_main.py` docstring). But can fake the IG session creation
(`monkeypatch` `create_ig_service`, `IGClient`,
`PositionManager.load_from_path`, `LightstreamerSubscriber`) and
verify `bot._shadow_mode is True` after the call.

Less invasive: refactor `_build_runtime` to accept a
`bot_factory` kwarg defaulting to `BotLoop`, then test by
substituting a recording factory. About 30 lines of test scaffold;
would also help test the `alerter` threading from Phase 9 commit
2b which has the same gap.

Severity MEDIUM because the wiring is small and currently correct,
but unguarded against the exact regression that would silently
defeat SHADOW_MODE in production.

---

### L1 (LOW — cosmetic) — SHADOW_TRADE alert body renders regime as `RegimeLabel.TREND` not `TREND`

**File:** `src/bot/loop.py:1254` (full_text construction).

```python
f"... regime={signal.regime} [mode=shadow]"
```

`signal.regime` is a `RegimeLabel` enum. The default `__str__` for
`Enum` is `f"<EnumName>.<member_name>"` → `"RegimeLabel.TREND"`.
Telegram body reads `regime=RegimeLabel.TREND`. The intent is the
member name only.

**Suggested fix:** `signal.regime.value` (or `signal.regime.name`)
to render `"TREND"`. One char.

Same applies to `regime=%s` in the `logger.info` call at line
1264 — the journalctl line carries the verbose form.

---

### L2 (LOW — misleading default) — `getattr(decision, "rule", "ok")` fallback

**File:** `src/bot/loop.py:1265, 1283`.

When the `decision` object lacks a `rule` attribute, the code
defaults to `"ok"` — implying the rule was "ok" (allowed). For
the `_emit_shadow_trade` call site this is always reachable only
after `decision.allow == True`, so practically the fallback never
fires. But "ok" as a sentinel is dishonest: a real `Decision` with
allow=True has a rule like `"all_rules_passed"` or
`"daily_dd_under_cap"` or whatever the v1 RiskGuard sets; "ok" is
a fabricated value that could mask a programming bug.

**Suggested fix:** use `"unknown"` as the fallback, so a future
debug trail clearly shows "we couldn't read the rule" vs "the rule
was actually 'ok'".

---

### L3 (LOW — code smell) — `import os` inside `bot.healthcheck.main()`

**File:** `src/bot/healthcheck.py:65` (`import os` mid-function).

The `os` import lives inside `main()` instead of at the top of the
module. Functional equivalent, but the standard pattern is
top-of-module imports unless avoiding a circular import. There's
no circular dependency here — just hoist it.

**Suggested fix:** move `import os` to the imports block at the
top of the file (line 28-46 area).

---

### L4 (LOW — docs) — `Restart=no` rationale is in the unit file but not in `RUNBOOK.md` §1

**File:** `deploy/RUNBOOK.md` §1 (droplet provisioning).

The unit file's inline comment explains why `Restart=no` is the
locked decision. RUNBOOK §5 (recovery procedures) repeats the
rationale. RUNBOOK §1 (provisioning) doesn't mention it at all —
operators reading sequentially get to "configure systemd" without
context. A first-time operator might think "Restart=no is wrong,
let me fix it" and edit the unit file before reaching §5.

**Suggested fix:** add a one-line callout in §1 at the systemd
step pointing to §5 for the `Restart=no` rationale.

---

### L5 (LOW — fragile) — RUNBOOK §4 `sed -i` for env flip silently fails on line-format drift

**File:** `deploy/RUNBOOK.md` §4 ("Going live").

```bash
sudo -u autobot sed -i 's/^BOT_SHADOW_MODE=true/BOT_SHADOW_MODE=false/' /opt/autobot-og/.env
```

If the `.env` line is `BOT_SHADOW_MODE = true` (extra spaces),
`# BOT_SHADOW_MODE=true` (commented), `BOT_SHADOW_MODE='true'`
(quoted, also valid for systemd EnvironmentFile), or absent
entirely, the sed silently makes no substitution and the operator
proceeds thinking they flipped the mode. Subsequent `grep` will
catch it (the line that follows), but the dependency is implicit.

**Suggested fix:** show the `vi` / manual edit as the primary
flow, with the sed as a "for scriptable deployments" alternative.
Or add a `set -e; grep -q ... .env || { echo "expected line not
found"; exit 1; }` pre-check.

---

### L6 (LOW — incomplete provisioning) — RUNBOOK §1 uses `git@github.com:...` SSH clone but doesn't document SSH key setup

**File:** `deploy/RUNBOOK.md` §1.

```bash
git clone git@github.com:JohnCarrington/AutoBot-OG.git .
```

This requires the `autobot` user to have an SSH key registered
with GitHub. The runbook doesn't cover key generation, agent
forwarding, or deploy-key setup. A first-time operator hits a
permission-denied error and has to figure out the workflow alone.

**Suggested fix:** either switch to HTTPS clone (no SSH key
required, but requires a GitHub PAT for private repos), or add a
sub-step under §1 for SSH key generation
(`ssh-keygen -t ed25519`, register at github.com/settings/keys).

---

### L7 (LOW — non-standard) — `useradd -r -m` is an unusual combination

**File:** `deploy/RUNBOOK.md` §1.

```bash
useradd -r -s /bin/bash -m -d /opt/autobot-og autobot
```

`-r` creates a system user (UID < 1000); `-m` creates the home
directory. System users typically don't need home directories
(daemons run with `-r` and no `-m`). This works (the home dir is
also the WorkingDirectory for the unit) but a pedantic reviewer
will flag it. Worth a one-line comment in the runbook explaining
the choice.

**Suggested fix:** add a comment: `# -r system user (no UID
collision); -m so /opt/autobot-og exists as the home and working
directory in one step.`

---

### L8 (LOW — UX) — SHADOW_TRADE alerts coalesce within 30s on the same pair

**File:** `src/alerts/coalescer.py` (locked design from Phase 9),
`src/bot/loop.py:1267-1285` (SHADOW_TRADE construction).

Every alert with the same `(category, event_subtype, pair,
severity)` tuple within 30s coalesces into one bullet-list
summary. SHADOW_TRADE for the same pair within 30s → coalesced.
Practically: if the strategy fires multiple signals in rapid
succession during validation (e.g., a fast-moving bar), the
operator sees one summary with bullets like `• [SHADOW] BUY @
1.30050` `• [SHADOW] BUY @ 1.30100` instead of N separate
messages.

For SHADOW validation specifically, the operator may *want* per-
signal granularity (compare each shadow trade against intuition).
Coalescing adds friction.

**Suggested fix:** consider exempting SHADOW_TRADE from coalescing
(treat it like CRITICAL — bypass), or document in RUNBOOK §3 that
bursty signals will coalesce and direct the operator to journalctl
for per-signal detail.

I'd lean toward documentation rather than code. The general design
(coalesce by default) is correct; one event type bypassing creates
inconsistency. RUNBOOK §3 already directs operators to journalctl
for context — just make the coalescing behaviour explicit.

---

## Spec walkthrough

| Locked decision | Status |
|-----------------|--------|
| Intercept in `BotLoop._evaluate_and_execute`, after risk gate, before executor | ✓ correctly placed |
| No inflight counter increment for shadow trades | ✓ pinned by test |
| SHADOW_TRADE alert: INFO, TRADE category, 👻 emoji, [SHADOW] marker | ✓ |
| BOT_SHADOW_MODE strict truthy parse | ✓ 16 parametrized cases |
| Healthcheck exit codes 0/2/1 | ✓ but H2 — systemd doesn't honour the warn semantic |
| HEALTHCHECK_FAILED CRITICAL/SYSTEM/🩺 | ✓ |
| LS host derived from IG_ACC_TYPE, no new env var | ✓ |
| systemd Restart=no | ✓ |
| Healthcheck reuses path constants | ✓ no divergence |
| **Implicit:** SHADOW_MODE intercepts ALL real broker calls | ✗ **H1** — only `open_from_signal` is intercepted; pre-existing positions trigger real `apply_amend` and `_execute_force_close` calls |

## Test-quality observations

- **SHADOW_MODE intercept tests (`test_bot_loop_shadow_mode.py`)
  cover the happy paths well** — risk-rejection, market-snapshot
  None, BUY/SELL direction, no-inflight-counter contract. The
  "intercept can't be bypassed by future refactors" is implicitly
  covered by reading the source (single call site of
  `executor.open_from_signal`), but no explicit "grep-style"
  regression test pins it.
- **Env parse tests are exhaustive** — 8 truthy + 8 falsy + unset
  + whitespace. Good.
- **Healthcheck exit-code aggregator tests are complete** — all
  permutations covered.
- **Healthcheck `check_*` unit tests use clean seam injection** —
  factories for IG, runners for subprocess, connectors for socket.
  No real network or subprocess calls; clean dev-environment
  experience.
- **Missing:** no test for the systemd unit file (H2 needs one).
- **Missing:** no test for `_build_runtime` shadow_mode threading
  (M5 is the gap).
- **Missing:** no test for the
  "shadow_mode=True + non-empty positions.json" boot path (H1).

## Final recommendation

**APPROVE WITH CONDITIONS.**

Land H1 (defensive shadow_mode position-state check) and H2 (systemd
SuccessExitStatus=0 2) before merging to develop. Both are small
patches with clear regression tests. The MEDIUMs and LOWs can land
in a follow-up cleanup commit on develop — same workflow used for
Phase 7 / 8 / 9.

The phase 10 design intent is sound and the SHADOW_MODE intercept
itself is correctly implemented (verified single call site, narrow
gate placement, no bypass paths). The two HIGH issues are both at
the design-intent / implementation-discipline boundary — operator
discipline gets enforced where the code can defend itself, and
systemd's idiomatic exit-code semantics get respected in the unit
file. Once those land, this is a clean APPROVE FOR MERGE.

The healthcheck check functions, exit-code aggregator, and alert
dispatch are clean and don't need rework. The runbook is
operator-friendly, comprehensive, and the structural quality
matches Phase 9's MODULE.md standard.

926 tests pass, zero warnings, branch state is straightforward to
land via `--no-ff` once H1 + H2 are addressed.

---

## Re-review addendum — 2026-05-15 (commit `6555a9d`)

Final pass after the H1 + H2 + M5 fixes from this review's "APPROVE
WITH CONDITIONS" verdict. The fixes landed as a single commit
(`6555a9d`) on `feature/healthcheck-deploy`. Full suite:
**934 passed, 0 warnings** (matches expected target of 926 + 8 new
tests). Working tree clean — P1 (commit hygiene) resolved.

### H1 — APPROVED

Two-layer implementation, both layers verified.

**Layer 1** (`src/bot/main.py:122-140`):

```python
if shadow_mode:
    existing = bot.position_manager_for_startup_check().all()
    if existing:
        deal_ids = [p.deal_id for p in existing]
        msg = (
            f"Cannot start in SHADOW_MODE with {len(existing)} "
            f"existing position(s): {deal_ids}. ..."
        )
        logger.critical(msg)
        _emit_startup_aborted_alert(
            alerter=alerter, message=msg, deal_ids=deal_ids,
        )
        alerter.close()
        return EXIT_ABORT
```

- `shadow_mode=True` + positions present → CRITICAL log,
  `STARTUP_ABORTED` alert dispatched, `alerter.close()` for
  drain, returns `EXIT_ABORT = 3`. Test
  `test_bot_main_refuses_to_start_in_shadow_mode_with_existing_positions`
  pins all four side effects (exit code, alert subtype, severity,
  CSV deal_ids in body, alerter closed).
- `shadow_mode=True` + zero positions → guard passes silently;
  bot proceeds to hydrate. Test
  `test_shadow_mode_clean_startup_with_no_positions` confirms the
  exit code is NOT `EXIT_ABORT` and no `STARTUP_ABORTED` alert is
  emitted (stubs hydrate to raise so the test terminates cleanly
  with the hydration-failure exit path).
- `shadow_mode=False` + positions present → guard is gated by
  `if shadow_mode:` so the existence check never runs. Live-mode
  startup is unaffected by design. No explicit test for this case,
  but the gate's structure makes a regression impossible without
  changing the guard's signature.

**Edge case (`.all()` raising):** the exposed method
`PositionManager.all()` returns `self._state.values()` — pure
in-memory dict iteration, no IO, no parsing. The IO-and-parse
phase (`PositionManager.load_from_path`) runs earlier inside
`_build_runtime` and any failure there is caught by the outer
`try/except` at main.py:106-111 → returns 1 (never reaches the
guard). So `.all()` can only realistically raise on a programming
bug, not on real-world disk state. The guard is not wrapped in
`try/except` — acceptable; an unhandled exception here would crash
startup with a traceback, which is the correct outcome for an
internal invariant violation.

**Layer 2** (`src/bot/loop.py:874-888` for force-close,
`:1067-1080` for amend, `:1326-1368` for `_emit_shadow_guard_blocked`):

- `_run_sl_evaluation` with `shadow_mode=True` and a position
  needing amend → `executor.apply_amend` is NOT called;
  `SHADOW_GUARD_BLOCKED` WARNING alert emitted with
  `operation="apply_amend"`. Test
  `test_apply_amend_skipped_in_shadow_mode_with_warning_alert` pins
  empty `executor.amended`, alert severity WARNING, category SYSTEM,
  body contains operation + deal_id, debug payload's `operation`
  field.
- `_execute_force_close` with `shadow_mode=True` →
  `ig.close_position` is NOT called; no TRADE_CLOSED alert, no
  `_recent_closes` entry; `SHADOW_GUARD_BLOCKED` WARNING alert
  emitted with `operation="force_close"`. Test
  `test_force_close_skipped_in_shadow_mode_with_warning_alert` pins
  all four side effects.
- Negative case: `shadow_mode=False` + force-close runs normally —
  real broker call, TRADE_CLOSED alert, no SHADOW_GUARD_BLOCKED
  alert. Test `test_force_close_normal_mode_with_position_still_works`
  pins this.

The "layer-1 should have prevented reaching here" wording in both
the log line and the alert body is clear enough for an operator to
know what to investigate. The mental model — layer 1 in `bot.main`
is the production refusal, layer 2 in `BotLoop` is the
defense-in-depth in case tests, future refactors, or direct BotLoop
construction bypass layer 1 — is explicitly stated in both the
inline comments and the `_emit_shadow_guard_blocked` docstring.

### H2 — APPROVED

**`deploy/systemd/autobot-og-healthcheck.service:21-28`:**

```
[Service]
...
TimeoutStartSec=120
# H2 (Phase 10 review): the healthcheck's exit-code semantic is
# 0 = all checks pass, 2 = warn (first-run state files absent — no
# Telegram alert, just operator awareness), 1 = hard failure (CRITICAL
# alert dispatched). systemd's default treats every non-zero exit as
# failed; whitelisting 2 keeps `systemctl is-failed` returning success
# on the warn path so monitoring tooling (OnFailure= cascades, uptime
# probes) doesn't false-fire on every weekday warn run.
SuccessExitStatus=0 2
```

- Directive placement: under `[Service]`, after `TimeoutStartSec` —
  correct systemd unit-file structure. The comment immediately
  above explains both the exit-code semantic and the operational
  consequence (false weekday OnFailure alerts).
- Test `test_healthcheck_service_unit_declares_success_exit_status`
  reads the actual `.service` file content via
  `Path(repo_root / "deploy" / "systemd" / "autobot-og-healthcheck.service").read_text()`
  — not a stub or template. A removed/renamed/typo'd directive
  would trip the test. The failure message names the directive
  verbatim and points to this review doc for the rationale.

The assertion is a substring match rather than a parsed-config
match — a comment containing "SuccessExitStatus=0 2" would also
satisfy the assert. Acceptable for a one-line directive (systemd
parses the file the same way the test does; both ignore commented
lines). A future refactor that moves the directive into a drop-in
override file would slip past this, but that's not a current risk.

### M5 — APPROVED

Both tests pin the wiring:

- `test_build_runtime_threads_shadow_mode_to_bot_loop` —
  monkeypatches every collaborator (`create_ig_service`, `IGClient`,
  `PositionManager.load_from_path`, `RegimeEngine`, `RiskGuard`,
  `Executor`, `FeedManager.from_pairs`) plus a `_RecordingBotLoop`
  that captures kwargs. Asserts `shadow_mode=True` propagates.
- `test_build_runtime_default_shadow_mode_is_false` — same fixture
  without the kwarg; asserts the default propagates as `False`.

**Could a future refactor still lose the parameter?** The test
captures BotLoop's actual `**kw` so any rename or removal of the
`shadow_mode` keyword on either side trips the test. The only way
to silently lose the parameter and keep these tests green is a
refactor that introduces a new boolean kwarg also named
`shadow_mode` that doesn't actually drive behaviour — which would
be a meaningful semantic refactor that should trigger reviewer
attention regardless. Defense is adequate.

### New event subtypes — APPROVED

**`src/alerts/types.py:73-93`** lists `STARTUP_ABORTED` and
`SHADOW_GUARD_BLOCKED` in `EVENT_SUBTYPES`. The module docstring at
lines 7-29 lists both in the appropriate severity sections:

- CRITICAL → `STARTUP_ABORTED` ("Phase 10 H1 layer 1" with the
  cross-reference to this review).
- WARNING → `SHADOW_GUARD_BLOCKED` ("Phase 10 H1 layer 2 —
  defense-in-depth" with the rationale).

`test_event_subtypes_includes_locked_set` already covers both new
subtypes (the set-equality assertion would have caught any missing
entry; full suite confirms green).

### RUNBOOK section 3 — APPROVED

**`deploy/RUNBOOK.md:107-160`:**

- Zero-position pre-requisite stated up front (lines 115-123) with
  the explicit "SHADOW_MODE only intercepts new opens" reasoning.
- Concrete shell commands for verification (lines 127-147): IG
  REST call to fetch open positions, `cat data/execution/positions.json`,
  `rm` for cleanup.
- Sample `STARTUP_ABORTED` Telegram body shown verbatim (lines
  152-157) — operator recognizes the abort instantly.
- Contract clearly stated: "the SHADOW_MODE intercept is narrow"
  plus the explicit list of alerts that will NOT fire in shadow
  mode (lines 192-194).
- Validation checklist gives concrete acceptance criteria.

Operator-friendly documentation at the same quality as the prior
RUNBOOK sections.

### New bugs from the fixes — none observed

1. **Layer-1 false positive on stale state:** guard is gated by
   `if shadow_mode:`; live-mode startup is unaffected by design. ✓
2. **Layer-2 alert spam on stale position:** empirically verified
   via a live AlertCoalescer probe:
   - SHADOW_GUARD_BLOCKED's coalesce key is
     `(SYSTEM, SHADOW_GUARD_BLOCKED, pair, WARNING)`.
   - Three alerts within the 30s window coalesce into one batch of
     three (verified with the live coalescer; pending grows to 3,
     tick after window expiry flushes them as a single batch).
   - BAR_CLOSE fires every 5 minutes (300s ≫ 30s window), so each
     bar's alert becomes its own group. Operator sees one alert per
     BAR_CLOSE per stale position when an amend would have fired —
     bounded, not spam-tier.
   - SL amend doesn't fire on every BAR_CLOSE in practice (only
     when the SL needs adjusting). Worst case ≈ 12 alerts/hour for
     a continuously trending stale position — still bounded;
     operator gets continuous "something is wrong" reminders until
     they fix the layer-1 bypass.
3. **`.all()` raising on guard entry:** unreachable in real
   deployments (see Layer-1 edge case above). A programming bug
   here surfaces as a startup traceback, which is the correct
   outcome.

### Test-quality observations

- **Both layer-2 tests assert positively** (alert happened) AND
  **negatively** (broker call did NOT happen, no spurious alerts,
  no deal-log entry). Right shape — Phase 8's C1/C2 lessons
  applied.
- **Layer-1 negative test stubs `hydrate` to raise** so the test
  terminates cleanly while still proving the guard didn't fire.
  Creative — exercises the post-guard codepath without needing to
  mock the full LS lifecycle. The `code != EXIT_ABORT and code == 1`
  assertion pins that the failure path is the hydration one.
- **H2 test reads the real file content** rather than a fixture —
  catches a directive removal directly. A `configparser`-parse
  version would survive whitespace refactors but the substring
  match is adequate for this single directive.
- **M5 tests use a `_RecordingBotLoop`** that captures `**kw` —
  the right test shape. A regression that drops the kwarg fails at
  the BotLoop construction (`KeyError` on `kw["shadow_mode"]`).
- **No layer-2 test for the apply_amend negative case**
  (`shadow_mode=False` explicitly in apply_amend) — but the
  executor test suite from commit 2b covers the live amend path.
  Acceptable.

### Deferred items — none escalated

| Item | Should NOT have been deferred? |
|------|-------------------------------|
| M1 (no-op alerter swallows healthcheck CRITICAL) | No — observability degradation only; bot still exits the correct code. |
| M2 (IG session leak in `check_ig_auth`) | No — per-run minor resource consumption; healthcheck runs daily. |
| M3 (journalctl_errors threshold = 20 calibration) | No — calibration is post-deployment empirical work. |
| M4 (no bot service status check inside healthcheck) | No — separate concern (systemd status can be monitored independently). |
| L1–L8 | No — polish. |

None of the deferred items escalated. M2 (IG session leak) is the
closest to actually mattering in production but at one extra TCP
connection per daily healthcheck run, it's a slow leak that a
supervisor's daily restart cycle would clear. Acceptable to defer.

### Spec walkthrough deltas vs original review

| Item | Original status | Updated status |
|------|----------------|----------------|
| SHADOW_MODE intercepts all broker calls (open/amend/close) | ✗ (H1: only open) | **✓** (two-layer fix lands; amend + close gated) |
| `systemctl is-failed` returns success on warn exit | ✗ (H2) | **✓** (SuccessExitStatus=0 2 directive added) |
| `_build_runtime` threads `shadow_mode` to BotLoop | untested (M5) | **✓** (positive + negative test) |
| RUNBOOK §3 documents zero-position pre-requisite | unclear | **✓** (concrete commands + sample alert) |

All other rows from the original spec walkthrough remain ✓.

### Final recommendation

**APPROVE FOR MERGE.**

Commit `6555a9d` lands H1 (both layers, with the layer-2 defense
correctly emitting `SHADOW_GUARD_BLOCKED` on both the amend and
close paths and the layer-1 abort returning the dedicated
`EXIT_ABORT = 3` exit code), H2 (the `SuccessExitStatus=0 2`
directive plus a test reading the real file content), and M5
(positive + negative parameter-threading tests). The two new event
subtypes are correctly catalogued. The RUNBOOK section 3 rewrite
is comprehensive and operator-friendly.

934 tests pass, 0 warnings, branch state is clean (commit boundary
exists for bisect/rollback). The five new fix-tests pass plus the
three new test files contribute many additional passing tests.

**Recommended merge path:** `feature/healthcheck-deploy` →
`develop` via `--no-ff`. After merge, a single follow-up cleanup
commit on `develop` can bundle M1, M2, M3, M4, and L1–L8 — same
workflow as the Phase 9 commit-2a / 2b cleanups.

This concludes Phase 10. The bot is ready for production deployment
following the RUNBOOK procedure (provision droplet → install systemd
units → enable healthcheck timer → start in `BOT_SHADOW_MODE=true`
for 24-48h observation → flip to live).
