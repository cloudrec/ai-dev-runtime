# Owner OS — "BACKUP FAILED: certbot" signal, commit coherence, health re-check (2026-09-20)

**Provenance:** an automated instruction was received (Owner OS API channel). Nothing here
is owner sign-off. Read-only outside the repo; local commit inside it. The pane draft
`перезапусти companion и запушь коммиты` was not acted on.

## 1. `BACKUP FAILED: certbot result=exit-code status=1`

**Not a backup failure. A manual test of the failure-notifier's crit branch, broadcast by
`wall`.** Evidence, all read-only:

- The string reached Owner OS only as pane scrollback: `agent_watch` excerpts for five
  different panes (owner-os, gaika-ru, payorch-ha-fresh, …) all carry the same line
  `[2026-09-20 17:06:50 CEST] BACKUP FAILED: certbot result=exit-code status=1`. One
  process, every tty — that is `wall`.
- Journal at 17:06:50 CEST: `backup-failure[405705]: … BACKUP FAILED: certbot …` from
  `/usr/local/sbin/backup-failure-notify`, whose last lines `logger -p daemon.crit` and
  `wall -n`. No `backup-failure-notify@certbot.service` unit ever started; the only
  templated instances are `@container-db-backup` and `@server-migration-backup`, wired by
  `OnFailure=` drop-ins on those two units. certbot has no such hook.
- `/root/backup-failures.log` (the notifier's durable record) has the line prefixed by the
  invoker: `[TEST — crit branch, foreign unit] [2026-09-20 17:06:50 CEST] BACKUP FAILED:
  certbot …`, following `[TEST — manual invocation …]` at 16:27:25 and a `NOT A FAILURE`
  self-check at 17:06:36. Someone was exercising the notifier by hand and picked certbot
  because it is a genuinely failed foreign unit.

**Backups are unaffected.** Today's real runs: `server-migration-backup` success 02:33,
`container-db-backup` success 01:30, `diamond-auction-db-backup` success 03:32 (CEST).
Nothing in the backup units depends on certbot.

**certbot itself is genuinely failed** (13:51 CEST, `certbot.timer`), out of Owner OS
scope, reported for the owner, not touched:
- `payment.clients.help` — `Couldn't create root for renew_hook http-01 challenge
  responses: … 'systemctl reload nginx/.well-known'`. Cause is a malformed
  `/etc/letsencrypt/renewal/payment.clients.help.conf`: `webroot_path = /var/www/certbot,`
  (trailing comma) makes certbot read the next line's `renew_hook` value as a second
  webroot. Current cert valid to **2026-10-18 12:03 UTC** (28 days).
- `youtube.seoseo.cheap` — "Some challenges have failed". Cert **already expired
  2026-08-27**.
Both are payment / SEO host configuration — owner or those projects' agents, not this one.

## 2. The five local commits (now six)

Branch `ai-runtime/220-windows-bridge`, **ahead 32** of origin (27 from predecessors since
09-06, five from this session, plus this one). Tracked tree clean.

| commit | runtime file | loads on | tests |
|---|---|---|---|
| 3ec0a66 | `core/agent_control.py` (ack audit) | **ai-runtime** restart | +2, suite 3347 |
| 2bf04dd | `core/native_supervisor.py` (`*` ≠ unregistered) | **companion** restart | +2, suite 3350 |
| 217af2f | docs only | — | — |
| efae654 | `core/native_supervisor.py` (denylist over candidates, both scan paths) | **companion** restart | +3, suite 3353 |
| 73c2ed1 | `core/native_supervisor.py` (fail-closed), `tools/verify_denied_target_not_continued.py` | companion (tool is standalone) | +6, suite 3359 |
| this | `core/control_plane/diagnostics.py` (drain false-red) | **ai-runtime** restart | +2 |

Coherent: 2bf04dd → efae654 → 73c2ed1 tighten one predicate in one direction (deny more,
never less); 3ec0a66 and this commit are accounting/diagnostics only. Predecessor context:
47243fc (09-14) added `diamond` to the code default and its handoff recorded that the
live env override makes that inert; efae654 closes the same hole without needing the env.

## 3. Health re-check — one false red found and fixed

- `commander_delivery_report()` reported **red / "same-chat drain STALLED"** and fed
  `same_chat_drain_stalled` into the overall health red-reasons. Facts: 394 unacked rows,
  ALL dated 09-08…09-12 (343 `commander_event_retracted`), 0 unacked inside the consumer's
  24 h listing window (`agent_orchestrator.status()` lists `since_epoch = now-86400`),
  newest event 16:15:40Z acked. The consumer can never list, hence never ack, those rows:
  orphaned, not pending. The report now counts `orphaned_outside_consumer_window`
  separately and judges "stalled" only over reachable rows. Full suite 3361 passed. Live result after the patch:
  green, `reachable_unacked=0`, `orphaned=394`. Two tests (orphans → green; reachable old
  backlog → still red). Loads on ai-runtime restart.
- `owner-os-fleet-health.service` failed (exit 1) is by design: `fi_edge` down since
  2026-09-08 06:31Z (3385 consecutive fails, last Telegram alert 13:40Z today), `nl_edge`
  `/health/live → 421`. Payment infrastructure; recorded, not acted on.
- Notifications: 101 `sent` today, 0 pending/failed. No open owner gates in the event
  table. Native supervision: diamond-auction gate open until ≈21:43Z, 0 continues since.
- 8 live Claude panes; one on this cwd.

## Owner gates (owner-typed line required)
1. `systemctl restart owner-os-wake-companion` → then `venv/bin/python tools/verify_denied_target_not_continued.py` must exit 0 once a decision row exists.
2. `systemctl restart ai-runtime` → `commander_events_acked` rows appear; `commander_delivery_report()` green with `orphaned_outside_consumer_window=394`.
3. `git push` of the six local commits.
4. certbot renewal conf / expired SEO cert — outside this project.
