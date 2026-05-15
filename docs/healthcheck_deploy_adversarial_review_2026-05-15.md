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
