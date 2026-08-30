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

---

# P0 acceptance canaries — live run (2026-08-30 02:0x-02:1x UTC)

## Harness blocker found and fixed first

`cp-canary:0.0` — the project's own disposable canary, registered in
`config/managed_sessions.yaml` with `live_actuation: true` — had been
**quarantined since 2026-08-07** (`crash loop: 3 recoveries within 21600s`), and
**no quarantine release path existed anywhere in the code**. `recover()` writes
the latch; nothing removed it. A safety brake with no release is a broken brake,
and it is why no safe E2E canary could run.

Fixed in `fa142aa` + `2cf0994`: `session_recovery.release_quarantine()`, scoped so
only a target present in the managed registry can be released (`payment:0.0` is
absent and stays unreleasable), audited through the same `_log` sink as every
recovery decision, historical audit rows untouched. 4 tests; gate 175 passed;
registry guard mutation-verified.

A bug in the first cut was caught before use: it iterated
`load_registry()["sessions"]` as a list when it is a dict keyed by target, and the
test fixture repeated the same wrong shape — making the guard vacuously
permissive. Both fixed and re-mutation-verified.

Released for the canary only; `mess-qa-automation:0.0` verified still quarantined.

## Canary revival — real managed path

`session_recovery.recover('cp-canary:0.0', explicit=True)` (not raw tmux).
Observed live by the production path: `agent_list()` -> `is_agent: True`,
`alive: True`, cwd `/root/cp-canary-v2`, pid 904921. Durable event **13911**
`agent_recovered` (severity high) emitted by the observer; control-plane registry
records `lifecycle_state: recovered`.

## Scenario A — `agent_waiting_input`: **FULL PASS (continuation proven)**

Work delivered through the production actuation path
(`agent_control.agent_send`, idempotency key `p0-canary-A-waiting-input`,
`delivered=True submitted=True queued=False`) — one substantial block, confined to
`/root/cp-canary-v2`, no external effect. The canary worked, then stopped.

| Leg | Evidence |
| --- | --- |
| Observer event | **13926** `agent_waiting_input`, `agent_id=cp-canary:0.0`, `project_id=cp-canary-v2`, severity high, `owner_action_required=1`, 02:12:17Z |
| Decision | `wake_audit` **104750** — decision `wake`, `actionable=1`, `actionable_waiting_transition` |
| Claim | `wake_send` 02:12:50 `allowed=0 actionable_cooldown_active:23s`; 02:13:18 `allowed=1 claimed_actionable` |
| Route | `owner-os` — fallback with `route_reason: unmapped_route:cp-canary-v2`, conversation `6a7d37d0-...` |
| Delivery | `wake_delivery` **delivered=1**, `submitted_and_assistant_started_generating` |
| Exactly-once | `wake_submitted` count **1**, source `companion` |
| Retire | `wake_audit.acknowledged=1` at 02:13:20 |

**Bounded-retry proof, incidentally:** the first claim was refused by the
actionable cooldown and the second succeeded 28s later — the retry path works and
did not lose a still-actionable wake.

**Route note (not a defect, but recorded):** `cp-canary-v2` has no
`wake_route` binding, so it correctly falls back to the `owner-os` control chat
with an explicit `unmapped_route` reason. No URL was guessed and no binding was
created; the canonical rebind flow was not needed because the fallback is the
intended behaviour for an unbound project.

### A — continuation leg PROVEN

| Leg | Evidence |
| --- | --- |
| Continuation delivery | `deliveries` idempotency key **`cp-canary-13926-continue-safe-20260830-0414`** -> `cp-canary:0.0`, `agent_send`, `delivered=true submitted=true queued=false duplicate=false pane_changed=true`, 02:14:33Z |
| Latency | **75s** after the wake landed (02:13:18 -> 02:14:33) |
| Who | `delivery_attribution`: `actor=api:bearer`, `source=172.20.0.6 ua=python-httpx/0.27.0` — the awakened ChatGPT side calling the Owner OS API, i.e. it re-read live state rather than trusting the wake payload |
| Causality | the idempotency key **carries the wake event id 13926**. That is what makes this causal rather than coincidental — the same standard the 2026-08-27 report used for event 9997 |
| Same agent, no duplicate | exactly **one** `cp-canary` tmux pane; `agent_created: false`; canary returned `waiting_input` -> `working` and emitted **13927** `work_report_published` |

**A is a complete closed loop:** real stop -> observer event -> decision -> claim
(with a bounded cooldown retry) -> correct bound chat -> exactly-one submission ->
assistant started -> ChatGPT re-read Owner OS -> same canary continued, no
duplicate.

## NOT YET PROVEN

* Scenarios **B** (`work_stopped_incomplete`), **C** (completion), **D**
  (`agent_process_failed`) not yet run.
* Dedupe negative proof (no repeated wake after resolution) not yet run.

**Acceptance is therefore NOT GREEN.**

---

# Acceptance run, part 2 — second root cause found BY the canaries

## Root cause #2: the DECISION gate was starved as well as the send gate

`claim_send` (send gate) was fixed earlier. `should_wake` (decision gate) carried
the **identical** asymmetry — actionable branch scoped to `actionable=1`,
non-actionable branch scoped to nothing — so every actionable *decision* reset the
non-actionable floor.

Fixing only the send gate was not enough: an event skipped at the decision gate
never becomes a `wake` row at all, so it can never reach a claim, and
`_redecide_cooldown_skips` re-runs the same query and gets the same skip.
**Permanent silence, not a delay.**

**Found by canary B, not by inspection.** Event 13946 (`work_stopped_incomplete`,
cp-canary) sat in `skip/cooldown_active` while the owner-os route's last
NON-actionable claim was 2230s old — far outside its own 900s window.

Deployed `84dc207`. Two tests; 55 wake tests; gate 187 passed; mutation-verified.
Backup + rollback tag first; both importing workers restarted; skew empty.

## Scenario A — `agent_waiting_input`: **FULL PASS, twice**

* **13926** — full closed loop with causally-keyed continuation (above).
* **13950** — second independent pass: `delivered=1`,
  `submitted_and_assistant_started_generating`, route `owner-os`, then
  `closed-loop-watch: deregistered cp-canary:0.0 for event 13950 —
  pane_alive_and_working`.

## Scenario B — `work_stopped_incomplete`: class PROVEN, canary instance QUEUED

**The event type now reaches a wake decision, which was impossible before:**

| Event | Evidence |
| --- | --- |
| **13946** | `work_stopped_incomplete`, `cp-canary:0.0`, emitted by the real `work_evidence.scan()` after the canary wrote a report its own words mark `BLOCKED` / `NOT STARTED` and went idle |
| **13775** | `work_stopped_incomplete`, `arbitrage2-opus:0.0`, `oar=1` — **decided `wake`** at 02:51:56 (non-actionable), claim counting down `120s -> 95s -> 63s` |

Before the two fixes, no `work_stopped_incomplete` could obtain a wake decision at
all. That gate is now open and demonstrably passing real traffic.

**Why the canary's own 13946 has not delivered yet — and it is not a bug:**

The non-actionable lane is deliberately **one wake per 900s per route** (the rate
limit was kept; only the wrong lane resetting it was fixed).
`_redecide_cooldown_skips` drains oldest-first and correctly excludes events past
`MAX_WAKE_AGE_SECS`. Measured live:

```
non-actionable events competing within the 3h window: 86
lane capacity: 1 per 900s per route
=> ~21.5 hours to drain
```

13946 is behind most of that queue by FIFO. The backlog is the three-week
accumulation the starvation bug itself created.

**This is a capacity question, not a correctness one, and it is an owner decision**
because draining faster means more owner-facing wakes per hour. Options:
raise non-actionable throughput temporarily to clear the backlog; retire the
pre-fix backlog as historical; or accept the ~21.5h drain.

## Status: **NOT GREEN**

| Scenario | Status |
| --- | --- |
| A `agent_waiting_input` | **PASS** (13926 with continuation proof, 13950) |
| B `work_stopped_incomplete` | class proven to decision+claim (13775); canary 13946 queued behind an 86-event backlog |
| C completion / `task_completed` | not run |
| D `agent_process_failed` | not run |
| dedupe negative proof | not run |

Both root causes found during this run were found **by** the canaries, not by
review: the missing quarantine-release path (which had made the harness unusable
since 2026-08-07) and the decision-gate half of the starvation defect.

---

# Acceptance part 3 — everything not behind the backlog gate

Backlog **preserved untouched**: nothing retired, deleted, rewritten or
accelerated; no rate limit or route changed.

## Dedupe / exactly-once — **PASS**

Proven on both fully-closed actionable loops:

| Check | 13926 | 13950 |
| --- | --- | --- |
| wake decisions | 1 | 1 |
| `wake_submitted` rows | 1 | 1 |
| deliveries (delivered=1) | 1 | 1 |
| allowed claims / attempts | 1 / 2 | 1 / 1 |
| acknowledged | yes 02:13:20 | yes 02:32:50 |
| repeat wake after resolution | **0** | **0** |
| claims after ack | **0** | **0** |
| closed-loop deregistration | exactly 1 | exactly 1 |

The 2-attempt figure for 13926 is a cooldown refusal followed by success — the
bounded retry, not a duplicate. **No canary event was ever delivered twice**
(`having sum(delivered)>1` returns empty across every canary event).

## Scenario A — **PASS ×8**

Every `agent_waiting_input` from the canary this run reached
decision -> claim -> delivery with exactly-once semantics:
**13926, 13936, 13950, 13957, 13977, 13983, 13990, 13995** — all `wake`,
`claims=1`, `delivered=1`. 13926 additionally carries the continuation proof.

## Scenario C — `task_completed` — **PASS end-to-end (real production event)**

| Leg | Evidence |
| --- | --- |
| Event | **13799** `task_completed`, agent `mess-qa-final-sonnet:0.0` |
| Decision | `wake`, non-actionable, `urgent_event_not_yet_signalled` |
| Claim | `allowed=1 claimed`, route **`mess`** |
| Delivery | **delivered=1** `submitted_and_assistant_started_generating`, route `mess` |
| Exactly-once | `wake_submitted` = 1 |

A real completion event on a real agent, routed to that project's **own** bound
chat (not the fallback), delivered with the assistant started. Nothing was
manufactured to produce it.

## Scenario D — `agent_process_failed` — decision obtained, delivery queued

Event **13794** (`chemmy-fast:0.0`, `owner_action_required=1`) holds a `wake`
decision; its claim is queued in the non-actionable lane behind the backlog.
**No product-agent failure was manufactured**, per instruction.

## Scenario B — canary instances queued

`work_stopped_incomplete` from the canary: **13946, 13967, 13969, 13971** — all
emitted by the real `work_evidence.scan()`, all decided, all queued. Class already
proven to reach decision + claim via 13775.

## Class semantics verified in isolation

`task_completed`, `agent_process_failed`, `agent_dead`, `work_stopped_incomplete`:
all `WAKE_EVENT_TYPES=True`, `is_significant=True`
(`severity_at_wake_threshold`), all correctly non-actionable.

Gate: **129 passed** (`wake_bridge`, `wake_actionable_transitions`,
`wake_pipeline_health`, `session_recovery_false_resurrection`).

## Production health

Both workers `active`, `Result=success`, `NRestarts=0`, **exactly one process
each** (an earlier "2" was `pgrep` matching its own shell). `worker_skew()` empty.
**29 unrelated WIP files byte-identical to session start**, verified by diffing the
porcelain list.

8 log errors since the restart are all `cdp_error:WebSocketTimeoutException` — the
pre-existing transient CDP class (37 over the prior 3 days), **not introduced by
this deploy**; events 13936 and 13957 hit it and subsequently delivered, so the
retry path absorbed them.

## Status

| Scenario | Result |
| --- | --- |
| A `agent_waiting_input` | **PASS ×8**, one with full continuation proof |
| B `work_stopped_incomplete` | class proven to decision+claim; canary instances queued |
| C `task_completed` | **PASS end-to-end**, correct project route |
| D `agent_process_failed` | decision obtained; delivery queued |
| dedupe / exactly-once | **PASS** |
| no wrong-chat routing | **PASS** (owner-os, mess, gaika-extension all correct) |

**The ONLY remaining blocker is owner-gated:** 86 non-actionable events compete for
one slot per 900s per route (~21.5h drain), a backlog created by the starvation
bug itself. B and D deliveries sit behind it. Clearing it faster, or retiring it,
changes owner-facing wake volume or discards queued alerts — an owner decision,
deliberately not taken. Everything not behind that gate now passes.
