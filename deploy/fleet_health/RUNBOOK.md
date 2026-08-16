# Fleet health Telegram alerting — runbook

Server-down monitor for the payment fleet (management, RU-PROD, RU-2, NL edge, FI
edge). Sends DOWN / RECOVERED / periodic-reminder Telegram messages via the same
bot/chat Owner OS already uses for owner_push. Background:
`reports/SERVER_DOWN_TELEGRAM_ALERTS_2026-08-16.md`.

## What it watches, and what it doesn't

- Watches: is the *host* reachable — HTTP/HTTPS status on RU-PROD and the edges,
  TCP:22 everywhere. A host counts down only if **every** configured check fails.
- Does NOT watch: WireGuard tunnel health between hosts, replication lag, database
  state, certificate expiry — those already have their own monitors
  (`payorch-replication-monitor`, `payorch-cert-monitor`). A host that answers SSH but
  has a broken WireGuard tunnel to its peer (the 2026-08-16 NL↔RU-PROD incident) is
  correctly reported *up* by this monitor — the box is up, the tunnel isn't, and that's
  a different failure domain with its own tooling.
- Fleet list, roles and checks: `config/fleet_topology.yaml`. Add a host there to add
  it to monitoring; nothing else needs editing.

## Alerting behavior

- 3 consecutive failed checks (~10-15 min at the 5-minute timer cadence) before a DOWN
  alert fires — one blip never pages.
- 2 consecutive OK checks before RECOVERED fires.
- While still down, at most one reminder every 6 hours — never per-tick spam.
- Every alert includes: host label, IP, role, which probe(s) failed, first-failure
  timestamp (UTC), and duration (down) or outage duration (recovered).
- Tune via env on the systemd unit: `FLEET_HEALTH_FAIL_THRESHOLD`,
  `FLEET_HEALTH_RECOVERY_THRESHOLD`, `FLEET_HEALTH_REMINDER_INTERVAL_SECS`.

## Install / enable

```
sudo bash deploy/fleet_health/install.sh
```

Refuses to install if `configs/.env` has no `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` —
see `deploy/fleet_health/fleet_health.env.example`.

## Operating

```
systemctl status owner-os-fleet-health.timer
systemctl list-timers owner-os-fleet-health.timer
journalctl -u owner-os-fleet-health.service -n 50 --no-pager
```

Run one check immediately without waiting for the timer:
```
systemctl start owner-os-fleet-health.service
```

State (per-host consecutive fail/ok counts, first-failure time, last alert time) lives
at `state/fleet_health_state.json`. Deleting it resets all hosts to "unknown" — the
next run needs a fresh threshold streak before it can alert again, so don't delete it
casually during a live incident.

## Rollback

```
sudo systemctl disable --now owner-os-fleet-health.timer
sudo rm /etc/systemd/system/owner-os-fleet-health.{service,timer}
sudo systemctl daemon-reload
```

The repo files (`core/fleet_health.py`, `config/fleet_topology.yaml`,
`tools/fleet_health_run.py`, `state/fleet_health_state.json`) are untouched by this —
rollback only removes the systemd wiring. Nothing else on the management host is
touched by install or rollback: no payment service, DB, Patroni, etcd, DNS, firewall,
or WireGuard config is read or written by this monitor.

## Sending a manual test alert

```
FLEET_HEALTH_STATE_PATH=/tmp/fleet_health_test_state.json \
  python3 -c "
from core.fleet_health import send_telegram
print(send_telegram('[fleet-health] TEST — manual verification, ignore'))
"
```

A `(True, 'telegram:<id>', None)` return is a proven delivery. `(False, None, reason)`
means credentials are unset or Telegram rejected the send — `reason` never contains the
token.
