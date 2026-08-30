# Owner OS wake/doorbell — canonical handoff (2026-08-30)

One source of truth for the wake loop. Supersedes the wake sections of earlier
reports; those remain the evidence.

## Verdict

The wake loop was **not broken end to end**. Actionable stops were delivering
correctly the whole time. **Non-actionable stops could never be delivered at
all** — which is exactly the class the owner had to compensate for with manual
"CHECK".

## Architecture (unchanged — nothing was redesigned)

`event` -> `should_wake()` (audited in `wake_audit`) -> `pending_wake()` ->
companion -> `claim_send()` (global choke point, `wake_send`) -> `cdp_composer`
submit -> `wake_submitted` latch at the composer-cleared boundary ->
`wake_delivery` verdict -> `acknowledge()` on proven delivery ->
closed-loop-watch deregisters when the pane is working.

Routing is per-project through `wake_route` (11 bindings, owner- or
auto-discovery-bound, each audited with `bound_by`/`note`). **No binding was
changed, guessed, or hand-edited.**

## Root cause

`claim_send()` has two lanes. The actionable lane scopes its lookback to
actionable sends:

```sql
... WHERE allowed=1 AND COALESCE(actionable,0)=1 AND <route> ORDER BY id DESC
```

The non-actionable lane scoped to **nothing** — so every actionable claim reset
the 900s non-actionable window. With actionable wakes arriving every 60-90s, a
non-actionable event needed a 900s gap with no send of any kind, which never
occurred. Not delayed: **starved**.

Which types that silences (measured, not assumed):

| Reaches owner | Starved |
| --- | --- |
| `agent_waiting_input`, `agent_prompt_needs_response`, `owner_decision_required`, `agent_crash_loop`, `agent_needs_response`, `wake_loop_*` | **`work_stopped_incomplete`, `task_completed`, `agent_process_failed`, `agent_dead`, `notifications_red`, `notification_dead_letter`** |

A stage completing, or a process dying, produced **no doorbell**. That is the
whole reason manual "CHECK" was still needed.

Impact from `wake_expire_audit`: 87 events expired undelivered, **75 of them
`critical` or `owner_action_required=1`**, each aging out at ~10,800s. ~70 were
non-actionable; 53 were the `notification_dead_letter` alarm.

## Changes deployed (`851d95b`)

**One production file: `core/wake_bridge.py`.** No schema, config, credential, or
routing change.

1. **Starvation fix** — the non-actionable lane now looks back at non-actionable
   sends only, mirroring the actionable lane. The rate limit is unchanged; it
   simply stops being reset by a lane it does not share.
2. **Abandonment record** — a wake that was latched (composer observed cleared)
   but never confirmed now lands in `wake_abandoned` instead of vanishing, and is
   surfaced in `health()`. It is never re-offered, so the no-duplicate invariant
   holds by construction.
3. **Skew watched-files** — `_WORKER_WATCHED_FILES["wake_companion"]` now includes
   `tools/cdp_composer.py`, `tools/wake_companion.py` and `closed_loop_wake.py`.
   It previously watched only `wake_bridge.py`/`wake_routes.py`, so a fix to the
   actual delivery code raised no skew.
4. **Lookback indexes** — `ix_wake_send_lookback`, `ix_wake_audit_lookback`
   (wake_audit had 104k rows, no index; worst-case lookback measured 23ms).

## E2E PROOF — real production, not a test

**Non-actionable class (the defect's class):**

```
event 13762  notifications_red  severity=critical  owner_action_required=1
  before: 115+ logged refusals, reason not_claimed:global_cooldown_active:*
  after : delivered wake for event 13762 [route owner-os]
          -> submitted_and_assistant_started_generating   (delivered=1)
```

A real event that had been starved for hours, delivered through the production
pipeline within seconds of the fix going live, to its correct bound chat, with the
assistant confirmed starting. First non-actionable `claimed` row in `wake_send`.

**Actionable class (already working, re-verified live the same night):**

```
event 13863 [gaika-extension] -> submitted_and_assistant_started_generating
event 13868 [owner-os]        -> submitted_and_assistant_started_generating
event 13860 [mess]            -> submitted_and_assistant_started_generating
closed-loop-watch: deregistered gaika-server:0.0        for 13863 — pane_alive_and_working
closed-loop-watch: deregistered owner-os-opus-windows:0.0 for 13868 — pane_alive_and_working
closed-loop-watch: deregistered mess-qa-final-sonnet:0.0  for 13860 — pane_alive_and_working
```

Three different projects, three different bound chats, correct routing, assistant
started, continuation loop closed. **No wrong-chat routing observed.**

## Deploy record

* Backup: `backups/predeploy_wake_p0_20260830T015140Z/` — `control_plane.db`,
  `agent_control.db`, `runtime_jobs.db`, `configs/.env` snapshot, and the systemd
  units + drop-ins. Tag `rollback/pre-wake-p0-20260830T015140Z` -> `2e4c137`.
* Merged four staged branches; two test-file conflicts (both branches appended
  blocks) resolved by keeping both sides.
* Gate before deploy: **243 passed** across `wake_bridge`, `wake_pipeline_health`,
  `wake_delivery_verification`, `wake_routes`, `zero_human_ping`,
  `wake_actionable_transitions`, `control_plane_delivery`.
* Pushed `2e4c137..851d95b`; local == remote.
* **Restarted BOTH services** — `ai-runtime.service` AND
  `owner-os-wake-companion.service`. The second is the target the 2026-08-29
  deploy missed; every change here lives in a module the companion imports, so it
  would otherwise have sat stale. Verified: one process each, `Result=success`,
  `NRestarts=0`, zero errors, both re-registered in `wake_worker`, `worker_skew()`
  empty.
* The 29 unrelated dirty `reports/*` are byte-identical throughout.

## Rollback

```sh
git checkout rollback/pre-wake-p0-20260830T015140Z -- core/wake_bridge.py
systemctl restart ai-runtime.service owner-os-wake-companion.service
```
One file. No schema or config to unwind. Never `git reset --hard` — it would
discard owner WIP.

## Remaining gates (NOT fixed here)

1. **Telegram `owner_push` is dead** — `Bad Request: chat not found`, 2,565 of
   2,567 notifications dead-lettered. Credential/config-gated; needs the owner's
   chat ID. `api.requeue_dead_letters()` exists (dry-run default, refuses an
   unhealthy channel) to recover them once it is fixed.
2. **`cto_inbox` has never been read** — `cto_cursor` is empty for the life of the
   database. Reporting is now honest (`cto_inbox_never_read`); reachability is
   not fixed, and needs a decision on what consumes it.
3. The wake path does not depend on either: it delivers through ChatGPT.

## What acceptance still needs

A `work_stopped_incomplete` / `task_completed` / `agent_process_failed` event must
occur naturally on a managed agent and be observed delivering. The mechanism is
now proven for the non-actionable class via 13762, but those specific types have
not yet fired since the deploy. They cannot be manufactured without mutating a
product agent, which the directive forbids.
