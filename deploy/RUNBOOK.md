# AutoBot-OG deployment + operations runbook

Operator-facing reference for deploying, validating, and recovering
AutoBot-OG on a fresh DigitalOcean droplet. Six sections:

1. [Droplet provisioning](#1-droplet-provisioning)
2. [systemd installation](#2-systemd-installation)
3. [SHADOW_MODE first-run validation](#3-shadow_mode-first-run-validation)
4. [Going live](#4-going-live)
5. [Recovery procedures](#5-recovery-procedures)
6. [Common issues and resolutions](#6-common-issues-and-resolutions)

---

## 1. Droplet provisioning

**Target:** Ubuntu 24.04 LTS, single-CPU droplet (1GB RAM minimum, 2GB
recommended), 25GB disk. Region near IG's London POP for lowest LS
latency.

```bash
# As root on the fresh droplet:
apt update && apt upgrade -y
apt install -y python3.12 python3.12-venv git curl ca-certificates

# Create the dedicated bot user (no shell access required for the
# service, but a usable shell helps with manual operator tasks).
useradd -r -s /bin/bash -m -d /opt/autobot-og autobot

# Switch to the bot user for the rest of the install.
su - autobot
```

```bash
# As autobot in /opt/autobot-og:
git clone git@github.com:JohnCarrington/AutoBot-OG.git .
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# Required directories — created at first run by the relevant module
# (PositionsState, CircuitBreakerState, CandleArchive) but pre-create
# so first-run state files land in the expected location.
mkdir -p data/{candles,risk,execution,logs}
```

**Configure `.env`** in `/opt/autobot-og/.env` (mode 600, owner
`autobot`):

```bash
# IG account
IG_USERNAME=...
IG_PASSWORD=...
IG_API_KEY=...
IG_ACC_TYPE=DEMO          # DEMO or LIVE — drives LS endpoint host

# Telegram (no-op alerts if either is missing)
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# Bot configuration
BOT_PAIRS=GBPUSD          # comma-separated, optional (defaults to PAIRS)
BOT_LOG_LEVEL=INFO
BOT_LOG_FILE=             # empty = log to journal only
BOT_SHADOW_MODE=true      # MUST be true for first deployment (see §3)
```

```bash
chmod 600 /opt/autobot-og/.env
```

---

## 2. systemd installation

```bash
# As root:
cp /opt/autobot-og/deploy/systemd/*.service  /etc/systemd/system/
cp /opt/autobot-og/deploy/systemd/*.timer    /etc/systemd/system/
systemctl daemon-reload
```

**Enable the healthcheck timer** (independent of the bot service —
runs whether or not the bot is up):

```bash
systemctl enable --now autobot-og-healthcheck.timer
systemctl list-timers autobot-og-healthcheck.timer
# Expect "next" to be the next weekday 05:45 UTC.
```

**Do NOT enable the bot service yet** — start manually first to
verify the SHADOW_MODE STARTUP banner before letting systemd manage
restarts. See §3.

```bash
# Run the healthcheck manually to verify it passes:
sudo -u autobot /opt/autobot-og/.venv/bin/python -m bot.healthcheck
echo "exit=$?"
# Expect: 0 (pass) or 2 (warn — first-run state files absent).
# If 1 (fail): check the Telegram chat for the HEALTHCHECK_FAILED
# alert and resolve before continuing.
```

---

## 3. SHADOW_MODE first-run validation

**Purpose:** Verify signal generation, risk gating, and the alerts
pipeline against real market data without trading. SHADOW_MODE
intercepts at `BotLoop._evaluate_and_execute` AFTER risk gating —
every decision the bot would make is exercised, only the broker
call is replaced with a `SHADOW_TRADE` Telegram alert.

**Pre-requisite (zero-position state).** SHADOW_MODE only intercepts
new opens. Pre-existing positions (from a prior live session, a
state-file copy during droplet migration, or a partial rollback)
would still trigger real broker `apply_amend` and `close_position`
calls via the SL-evaluation and force-close paths. The bot enforces
this with a startup guard: if `BOT_SHADOW_MODE=true` AND
`data/execution/positions.json` reports any open positions, the bot
logs CRITICAL, dispatches a `STARTUP_ABORTED` Telegram alert, and
exits with code 3 (refused-to-start).

**To prepare for SHADOW_MODE on a droplet that previously ran live:**

```bash
# 1. Confirm no open positions on the IG account itself.
#    Use the IG web UI: Positions tab should be empty.
#    OR query via the bot's session helper:
sudo -u autobot /opt/autobot-og/.venv/bin/python -c "
from feed.ig_rest.auth import create_ig_service
from feed.ig_rest.client import IGClient
ig = IGClient(session=create_ig_service())
print('open positions:', ig.fetch_open_positions())
"

# 2. Confirm the local position state is empty.
sudo -u autobot test -f /opt/autobot-og/data/execution/positions.json && \
  cat /opt/autobot-og/data/execution/positions.json
# Expect: file absent, OR positions list empty (e.g. {"version": 1, "positions": []}).

# 3. If non-empty: close positions via IG web UI FIRST, then either
#    delete or empty the local file:
sudo -u autobot rm /opt/autobot-og/data/execution/positions.json
# (next bot start will re-create as empty.)
```

If you skip these and start with `BOT_SHADOW_MODE=true` anyway, the
bot will refuse to start and you'll see this in Telegram:

```
⛔ BOT STARTUP ABORTED
Cannot start in SHADOW_MODE with N existing position(s): [...].
SHADOW_MODE only intercepts new opens; pre-existing positions
would trigger real broker amends and force-closes...
```

That's the layer-1 safety guard working as designed. Resolve the
state, then start again.

```bash
# Confirm BOT_SHADOW_MODE=true in /opt/autobot-og/.env
grep ^BOT_SHADOW_MODE /opt/autobot-og/.env
# Expect: BOT_SHADOW_MODE=true

# Start the bot manually (so you see startup logs in real time):
systemctl start autobot-og.service
journalctl -u autobot-og.service -f
```

**Watch for the STARTUP Telegram alert.** It should read:

```
🤖 BOT STARTUP [SHADOW MODE]
Account: DEMO
Pairs: 1 (GBPUSD)
Hydration: <N> cached, <M> REST
Build: <hash> (<branch>)
```

The `[SHADOW MODE]` suffix is your confirmation that no real
trades will fire.

**Observation period:** 24-48 hours of live trading bars. During
this window:

- Every signal that passes risk gating fires a `👻 [SHADOW] ...`
  Telegram alert with planned entry, SL, strategy, and regime.
- All other alerts (FEED_STALE / FEED_RESUMED / BROKER_ORPHAN if any
  exist on the demo account / FAILURE_THRESHOLD_TRIPPED if anything
  goes wrong) work normally — the SHADOW_MODE intercept is narrow.
- TRADE_OPENED / TRADE_CLOSED / AMEND_FAILED / AMEND_PERSIST_FAILED
  alerts will NOT fire (no real trades).

**Validation checklist:**

- [ ] STARTUP alert received with `[SHADOW MODE]` suffix
- [ ] At least one `SHADOW_TRADE` alert observed (or logged "no
      signals generated" reason in journalctl if the market is quiet)
- [ ] Each `SHADOW_TRADE` body matches what you would expect from a
      manual review of the M5 chart at that timestamp
- [ ] No CRITICAL alerts (`FAILURE_THRESHOLD_TRIPPED`,
      `AMEND_PERSIST_FAILED`, `HEALTHCHECK_FAILED`)
- [ ] `journalctl -u autobot-og.service --priority=err --since '24
      hours ago'` returns no surprising errors (transient broker
      reconnects are normal; persistent failures are not)

**If validation fails:** stop the service (`systemctl stop
autobot-og.service`), investigate, fix, restart from manual run.
Do NOT enable the unit for autostart.

---

## 4. Going live

**Pre-flight:**

- [ ] §3 validation checklist complete
- [ ] IG account funded and ready
- [ ] No open positions on the IG account (broker truth is empty)
- [ ] `data/execution/positions.json` either absent or holds an empty
      positions list

```bash
# Stop the shadow run.
systemctl stop autobot-og.service

# Edit .env: flip the mode.
sudo -u autobot sed -i 's/^BOT_SHADOW_MODE=true/BOT_SHADOW_MODE=false/' /opt/autobot-og/.env
grep ^BOT_SHADOW_MODE /opt/autobot-og/.env
# Expect: BOT_SHADOW_MODE=false

# Enable the unit for boot-time autostart.
systemctl enable autobot-og.service
systemctl start autobot-og.service

# Tail the journal for the live STARTUP alert.
journalctl -u autobot-og.service -f
```

**Confirm the live STARTUP banner** in Telegram:

```
🤖 BOT STARTUP                    ← NO [SHADOW MODE] suffix
Account: DEMO                      (or LIVE if .env set IG_ACC_TYPE=LIVE)
Pairs: 1 (GBPUSD)
Hydration: <N> cached, <M> REST
Build: <hash> (<branch>)
```

The first real signal will surface as a `TRADE_OPENED` alert (not
`SHADOW_TRADE`). When the position closes you should see
`TRADE_CLOSED`. SL amends fire `AMEND_FAILED` only on broker
rejection.

---

## 5. Recovery procedures

### Bot exits non-zero

`systemctl status autobot-og.service` shows `failed` and the unit is
stopped. **Auto-restart is deliberately disabled** (`Restart=no`)
because automatic recovery would mask the
`FAILURE_THRESHOLD_TRIPPED` CRITICAL alert by silently respawning
the process before the operator notices.

```bash
# 1. Check Telegram for the most recent CRITICAL alert.
#    Common: FAILURE_THRESHOLD_TRIPPED with a reason string like
#    "5 consecutive event failures" or "5 consecutive periodic
#    failures". Other CRITICALs: AMEND_PERSIST_FAILED, SHUTDOWN
#    (after crashed=True).

# 2. Pull the journal for context.
journalctl -u autobot-og.service --since '1 hour ago' --priority=warning

# 3. Identify root cause:
#    - Event-failure trip: investigate per-bar dispatch errors
#      (broker API surface change? Lightstreamer protocol shift?
#      data corruption in the rolling buffer?).
#    - Periodic-failure trip: usually reconciliation or
#      force-close path; check the broker reachability and that
#      data/execution/positions.json is well-formed.
#    - AMEND_PERSIST_FAILED: STATE DIVERGED — broker has the new SL,
#      local has the old. Reconcile the position manually in IG's
#      web UI before any restart.

# 4. Fix the root cause. Do NOT restart blindly.

# 5. After fixing:
systemctl start autobot-og.service
# Verify Telegram shows STARTUP (not [SHADOW MODE] unless you
# deliberately re-enabled shadow for further validation).
```

### Healthcheck fires HEALTHCHECK_FAILED

The pre-market healthcheck (`Mon..Fri 05:45 UTC`) shipped a
CRITICAL alert summarising one or more failed checks. Resolve every
listed failure before the next trading session.

```bash
# Re-run the healthcheck manually after fixing:
sudo -u autobot /opt/autobot-og/.venv/bin/python -m bot.healthcheck
echo "exit=$?"
# 0 = clean; 2 = warns only (acceptable); 1 = still failing.
```

The healthcheck timer keeps firing even when the bot service is
down — that's intentional ("is the next session ready to start" is
a different question from "is the current session running").

### Crashed shutdown but no Telegram CRITICAL

Two possibilities:

- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` not set in `.env` — the
  alerter ran in no-op mode. Fix the env, restart.
- The crash happened before the alerter was constructed (e.g.
  preflight failure). Check `journalctl -u autobot-og.service` for
  the actual exit reason.

---

## 6. Common issues and resolutions

| Symptom | Likely cause | Resolution |
|---------|--------------|------------|
| `IG auth failed` in healthcheck or STARTUP | Token rotated; password expired; `.env` corrupt | Re-issue IG API key; update `.env`; verify with `sudo -u autobot .venv/bin/python -c "from feed.ig_rest.auth import create_ig_service; print(create_ig_service().acc_type)"` |
| `Disk space <1GB` healthcheck fail | Candle CSVs growing; logs accumulating | `du -sh data/* logs/*`; archive or rotate old per-pair CSVs; expand droplet |
| `Lightstreamer endpoint unreachable` | IG demo/live LS host outage; droplet egress firewall | Check IG status page; `nc -vz $(if grep -q LIVE /opt/autobot-og/.env; then echo apd; else echo demo-apd; fi).marketdatasystems.com 443` |
| Repeated `FEED_STALE` then `FEED_RESUMED` cycles | LS heartbeat instability — usually IG side | Watch over a few hours; if persistent, escalate to IG support; the bot itself handles the cycles correctly |
| `BROKER_ORPHAN` alert on every reconciliation | Position opened outside the bot (manual web-UI fill, leftover from previous deployment) | Confirm in IG web UI; either close the orphan manually or accept the recurring alert until next session |
| `MISSING_LOCAL_KEPT` alert | Position closed outside the bot (manual close in web UI, stop-out the bot didn't observe) | Verify in IG; if closed, add the deal to `data/execution/positions.json` removal manually OR restart bot to re-hydrate state from broker |
| `Position state corrupt` after disk full | `positions.json` truncated mid-write | Restore from a recent backup if you have one; else accept BROKER_ORPHAN alerts as the manual-reconciliation path |
| `journalctl_errors > 20` healthcheck fail | Investigate the actual lines: `journalctl -u autobot-og.service --priority=err --since '24 hours ago'` | Most common: persistent broker reconnect storm — escalate to IG; transient: lower threshold or wait for the 24h window to roll |

### Future hardening (not v1)

- `MemoryMax=` / `CPUQuota=` on the systemd unit
- Auto-restart with backoff (`Restart=on-abnormal`,
  `RestartSec=300`) once we have confidence the failure-threshold
  trips are rare enough not to need manual triage
- Off-host log shipping (Loki / CloudWatch / etc.) so a droplet
  failure doesn't lose the journal
- Position-state backup (cron rsync to a separate location)
- LIVE-account dry-run period (start with smaller size; ramp)

---

## Quick reference

```bash
# Start / stop
systemctl start  autobot-og.service
systemctl stop   autobot-og.service
systemctl status autobot-og.service

# Logs
journalctl -u autobot-og.service -f                       # live
journalctl -u autobot-og.service --since '1 hour ago'     # historical
journalctl -u autobot-og.service --priority=err           # errors only

# Healthcheck
sudo -u autobot /opt/autobot-og/.venv/bin/python -m bot.healthcheck
systemctl list-timers autobot-og-healthcheck.timer
journalctl -u autobot-og-healthcheck.service -n 100

# Toggle shadow mode (requires service restart)
sudo -u autobot vi /opt/autobot-og/.env   # flip BOT_SHADOW_MODE
systemctl restart autobot-og.service
```
