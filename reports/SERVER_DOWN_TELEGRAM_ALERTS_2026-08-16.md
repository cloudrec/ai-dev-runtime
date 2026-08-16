# Server-down Telegram alerting — build + root cause — 2026-08-16

## Task

Owner: previously provided a Telegram bot token + chat/group ID, expected alerts when
servers go down. The 2026-08-16 NL/FI WireGuard outage produced no Telegram alert.
Reconstruct + finish server-down alerting end-to-end.

## Root cause (two, stacked)

**1. No server-health-to-Telegram monitor existed anywhere in the fleet.**
`payorch-cert-monitor.service` and `payorch-replication-monitor.service` — the only
two existing infra monitors — are both explicit in their own unit files that "no
approved ops channel exists yet" and write only to the journal/state files. Neither
watches host-down at all; cert-monitor watches TLS expiry, replication-monitor watches
Postgres replication lag. The NL/FI incident
(`/opt/payment-orchestrator/reports/INCIDENT_NL_EDGE_WG_504_2026-08-16.md`) was
detected by a human running manual checks, not by any automated alert, because there
was nothing to alert. This is now fixed (see Implementation below).

**2. Owner OS's own Telegram channel (`owner_push`) is currently non-functional —
a standing, previously-documented, owner-only blocker, independent of this task.**
`configs/.env` has real `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` (bot identity
verified live today: `getMe` → `ezzetasecurity_bot`, id `8806300672`, matches the prior
report). But every send — including one live TEST alert sent as part of this task —
fails with `Bad Request: chat not found`. This was already found and documented on
2026-08-06 in `reports/OWNER_OS_NIGHT_SHIFT_CTO_PLAN.md`: *"a bot cannot open a
conversation with a user who has never messaged it. The owner must press Start on the
bot once."* The control-plane `channel` table already records `owner_push` as
`unhealthy` / `chat not found` since this morning (`09:31:26Z`), independently
confirming the same failure this task's own TEST send reproduced at `getChat`/
`sendMessage` level a few hours later.

The credentials in `configs/.env` are unchanged since at least 2026-08-06 (verified by
diffing masked token/chat-id against two independent `.env.bak` snapshots from that
date under `/root/owner-os-backups/`), so this is not a recent regression — the channel
has likely never delivered a message to this chat ID.

**A different chat ID for the same bot token works elsewhere.** The bot
(`ezzetasecurity_bot`) is a shared product bot, not Owner-OS-dedicated (`getUpdates`
returns `409 Conflict` — another service already long-polls it). `/opt/security-qa/.env`
uses the identical `...jgk` bot token with a **different** `SALES_TELEGRAM_CHAT_ID`
(`549...359` vs. Owner OS's `821...695`), and `/opt/security-qa/reports/
ROADMAP_AND_ANSWERS_20260719.md` records that channel as live and working ("Telegram-
канал — работает, лиды и уведомления менеджеру"). This chat belongs to a different
product's lead-notification flow, not verified as the owner's own chat, so **this task
does not repoint Owner OS's alerts at it** — silently redirecting infra-down alerts to
an unverified destination would be a worse failure than the current one. Documented
here as a lead in case the owner wants it investigated, not applied.

**Fix required, and it is entirely on the owner's Telegram client, not in this repo:**
open the bot `@ezzetasecurity_bot` in Telegram and press **Start** (or send it any
message) from the account/group meant to receive alerts, then confirm the chat ID
belongs to that same destination. No code change makes this unnecessary — Telegram's
bot API structurally refuses to open a conversation the human side never initiated.
The moment that happens, the very next 5-minute timer tick delivers real alerts with
no further action needed here.

## What was searched (read-only recon, before concluding blocked)

- Repo-wide grep for `telegram`/`TELEGRAM`/bot/chat across `.py .md .yaml .json
  .service .timer .sh .env*` — found the transport in `core/control_plane/delivery.py`
  and its channel-health bookkeeping in `core/control_plane/store.py`/`api.py`.
- Live env var check (`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`), masked.
- Every `*.env*` file under `/`, `/opt/*`, `/etc/*`, `/root/*` (system-wide) for a
  second/older Telegram config — found `/root/owner-os-backups/*/env-telegram*` and
  `/opt/seo/.env.telegram-backup-*`, both consistent with the current, non-working
  value; and `/opt/security-qa/.env`, which has a different, reportedly-working
  chat ID under the same bot token (see above).
- `systemctl` unit inventory for anything owner/health/alert/monitor/notif-named —
  found `owner-os-wake-companion.service` (the only prior consumer of
  `configs/.env`'s Telegram vars) and the two payorch monitors (no Telegram wiring).
- `crontab -l`, `/usr/local/sbin` — no existing host-down checker anywhere.
- `control_plane.db` `channel` table — confirms `owner_push` already recorded
  `unhealthy`/`chat not found` before this task's own test, independently.
- `docs/INFRASTRUCTURE_TOPOLOGY.md`, `payorch-replication-monitor.sh`,
  `payorch-cert-monitor.service` — canonical fleet roles + addresses, cross-checked
  against each other and against `INCIDENT_NL_EDGE_WG_504_2026-08-16.md`.
- Live `getMe` / `getChat` Telegram API calls (read-only, no message sent for `getChat`)
  to independently reproduce and date-stamp the blocker today, not rely on the
  2026-08-06 report alone.

No token, chat ID, or key material is reproduced above beyond partial masking
(`880...jgk`, `821...695`, `549...359`) sufficient to show two are the same value and
two are different, without exposing either.

## Implementation

New, self-contained, does not touch the Control-Plane-V2 agent-worker DB or its
advisory-lock protocol (not one of those 11 workers — own private JSON state file, no
lock contention possible):

- `config/fleet_topology.yaml` — canonical fleet: management (`84.247.139.105`),
  RU-PROD (`77.110.113.202`), RU-2 (`5.45.82.124`), NL edge (`37.1.216.133`), FI edge
  (`185.75.135.63`), each with role + service-aware checks. Adding a host here is the
  only step needed to monitor it — nothing else hardcodes the fleet list.
- `core/fleet_health.py` — probes (HTTP/HTTPS status via `curl --resolve` with real
  SNI/Host, or TCP connect — never ICMP-only), pure state-machine (`evaluate_host`)
  with 3-consecutive-failure DOWN threshold, 2-consecutive-success RECOVERED
  threshold, 6-hour max reminder cadence while still down, `first_fail_ts`-based
  duration reporting, and `send_telegram()` which reuses
  `core.control_plane.delivery._send_owner_push` — no second bot.
- A host is DOWN only if **every** configured check fails — a host that answers SSH
  but fails an app-level health check is degraded, not down, by design: this is what
  correctly reports NL edge as "up" during a WireGuard-tunnel-only failure (the actual
  shape of the 2026-08-16 incident) rather than crying wolf on the box itself.
- `tools/fleet_health_run.py` — oneshot CLI: exit 0 all-healthy / 1 something-down /
  2 could-not-evaluate (mirrors `payorch-cert-monitor.sh`'s exit-code convention, so a
  down host is visible in `systemctl --failed` even if Telegram itself is unreachable).
- `deploy/fleet_health/` — `owner-os-fleet-health.{service,timer}` (5-min cadence,
  `Persistent=true`, hardened: `NoNewPrivileges`, `ProtectSystem=strict`, syscall
  filtering, no privileged caps), `install.sh` (idempotent, refuses to install without
  Telegram creds present), `fleet_health.env.example`, `RUNBOOK.md`.
- No secrets committed: `config/fleet_topology.yaml` holds only IPs/roles (already
  non-secret — present in `/etc/systemd/system/*.service` `IPAddressAllow=` lines and
  multiple `/opt/payment-orchestrator` reports); `TELEGRAM_BOT_TOKEN`/`CHAT_ID` stay in
  `configs/.env` (mode 600, gitignored, unchanged).

## Tests

`tests/test_fleet_health.py`, 11 tests, all passing:
- topology sanity (5 hosts, every host has ≥1 non-ICMP check)
- below-threshold failures never alert
- threshold-crossing fires exactly one DOWN alert, `first_fail_ts` = first failing
  probe (not the threshold-crossing one)
- dedupe: 19 more consecutive failures inside the reminder window → zero repeat alerts
- reminder fires once the interval elapses while still down
- recovery requires `recovery_threshold` consecutive OK probes and reports correct
  outage duration
- a flap that never reaches threshold resets cleanly with no alert
- `format_alert` contains host/IP/first-failure/failed-probe text
- `run_once` end-to-end against a **mocked** probe/send pair: threshold → DOWN send →
  no resend → recovery → RECOVERED send, state persisted to a tmp JSON file
- an explicit safety test that monkeypatches `socket.create_connection` and
  `subprocess.run` to raise if called, then drives a full simulated DOWN cycle through
  `run_once` purely via the public `probe_fn` seam — proves the "simulated failure"
  path never opens a real socket or shells out, i.e. no real server was ever touched.

Full repo suite: `2167 passed, 0 failed` (`./venv/bin/python -m pytest -q`, 478s).

## Real Telegram delivery test

One live send attempted through the real, configured transport
(`core.fleet_health.send_telegram`, i.e. `delivery._send_owner_push`):

```
ok=False  msg_id=None  err=telegram send failed: Bad Request: chat not found
```

This is a genuine, logged API rejection (visible in `control_plane.db`'s `channel`
table as `owner_push` / `unhealthy`), not a fabricated success — see Root cause #2.
**Delivery cannot be proven end-to-end until the owner completes the one-time Telegram
action above.** Everything upstream of that final hop (probe logic, state machine,
threshold/dedupe/recovery, transport code, systemd wiring) is proven working by the
unit tests and by the live systemd run below.

## Deploy / systemd status (management host only)

```
$ sudo bash deploy/fleet_health/install.sh
Created symlink /etc/systemd/system/timers.target.wants/owner-os-fleet-health.timer → ...
● owner-os-fleet-health.timer - active (waiting)
```

First run hit `203/EXEC` — `ProtectHome=true` (copied from `payorch-cert-monitor`,
which runs from `/usr/local/sbin`) hides `/root` entirely, and this unit's venv/script
live under `/root/ai-dev-runtime`. Fixed to `ProtectHome=read-only` (repo stays
readable; `state/` stays writable via the explicit `ReadWritePaths=`), redeployed.
Second run:

```
ExecMainStatus=0  ActiveState=inactive  Result=success
[fleet-health] management: up — tcp:22 open
[fleet-health] ru_prod: up — http:/ -> 200; tcp:22 open
[fleet-health] ru2: up — tcp:22 open
[fleet-health] nl_edge: up — https:/health/live curl exit 28 (timeout); tcp:22 open
[fleet-health] fi_edge: unknown — https:/health/live curl exit 28 (timeout); tcp:22 failed: timed out
```

`nl_edge` correctly reads **up** (SSH answers; only the app-level health probe times
out — consistent with the WireGuard-tunnel-only nature of the still-open
`INCIDENT_NL_EDGE_WG_504_2026-08-16.md`, not a dead host). `fi_edge` is on its first
failing probe (`consecutive_fail=1`, state `unknown`) — matches the incident report's
"FI edge все порты недоступны"; needs 2 more consecutive 5-minute failures before a
real DOWN alert is due, which is the anti-flap threshold working as designed, not a
gap.

```
$ systemctl list-timers owner-os-fleet-health.timer
NEXT  Sun 2026-08-16 11:40:27 CEST  ...  ACTIVATES owner-os-fleet-health.service
$ systemctl is-enabled owner-os-fleet-health.timer
enabled
```

Enabled (`WantedBy=multi-user.target`/`timers.target`) → survives reboot. Nothing else
was touched: no payment service, DB, Patroni, etcd, DNS, firewall, or WireGuard config
was read or written by install or by the monitor itself (probes are GET/TCP-connect
only).

## Rollback

```
sudo systemctl disable --now owner-os-fleet-health.timer
sudo rm /etc/systemd/system/owner-os-fleet-health.{service,timer}
sudo systemctl daemon-reload
```

Repo files untouched by rollback. `git revert 9b00269` removes the code/config/tests
if a full undo is ever wanted.

## What's left — the one owner action

1. Open Telegram, find `@ezzetasecurity_bot`, press **Start** (or send it a message)
   from the account/group that should receive alerts.
2. Confirm the resulting chat is reflected correctly in `TELEGRAM_CHAT_ID` in
   `configs/.env` (currently `821...695` — masked; unclear if this is even the intended
   destination, since it has apparently never received a successful message).
3. No restart needed after that — the timer already runs every 5 minutes and will pick
   up a working channel on its next tick.

Everything else asked for — recon, root cause, fleet-aware service-level probes,
anti-flap threshold, stateful dedupe, DOWN/RECOVERED/reminder messages with full
context, git-committed code/config/tests/unit/timer/install script/runbook, and a live
systemd deployment on the management host — is done and verified above.
