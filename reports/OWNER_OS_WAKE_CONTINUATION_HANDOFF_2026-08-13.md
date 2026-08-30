# Owner OS wake continuation — crash/quota-safe handoff (2026-08-13)

Purpose: if this session dies, the next one resumes from this file alone. Diagnosis is
proven, not hypothesised. No secrets in this document.

## 1. Current tree state (verified read-only at session start)

Branch `ai-runtime/182-retry-fix-wake-continuation-star`, HEAD `b1a9f0a`.

`core/wake_bridge.py` is **CLEAN**. The prior 93%-context agent was killed while reverting
its own partial edit to that file; the revert **did land**. `git status` shows no entry for
it and `git stash list` is empty. Nothing to recover, nothing to undo.

Dirty files that are **pre-existing and unrelated — preserve exactly, never reset/stash**:

| File | State |
| --- | --- |
| `reports/OWNER_OS_WAKE_BRIDGE_REPAIR_2026-08-11.md` | modified |
| `reports/phase3_postfix_soak.jsonl` | modified |
| `reports/OWNER_OS_WAKE_REBIND_STATUS_2026-08-08.md` | untracked |

## 2. Proven incident facts (2026-08-13 ~03:58–04:10 UTC)

Bound wake target: `https://chatgpt.com/c/6a7d37d0-02dc-83ed-9ef4-d26156937c57`. The rebind
is correct and `owner-os-wake-companion` is running. `payorch-sbp-resumed` repeatedly
entered `waiting_input` and the owner had to ping ChatGPT by hand.

**Defect A — backlog starvation (real, historical).** `wake_bridge.pending_wake()` selects
`ORDER BY a.id ASC`, i.e. strictly oldest-first, and `claim_send` enforces one send per
global `COOLDOWN_SECS` (900s). The queue therefore drains one *ancient* event per 15
minutes: event 3746 from Aug 11 was delivered at 04:09:57 while fresh events waited behind
it.

**Defect B — the stall itself was invisible.** Event 3920 at ~03:58:09Z was
`new_agent_discovered`, severity `info`, skipped with reason `cooldown_active` and also
below the wake severity threshold. It was **not** a `waiting_input` event. The actual
`working → waiting_input` transition of an ALREADY-LIVE agent emitted **no** durable
actionable event at all:

* `core/agent_watcher.py::transition_event` does produce `agent_waiting_input`, but
  `core/agent_orchestrator.py:727` routes it only to `agent_control.record_commander_event`
  (the legacy commander log). It never reaches `cto.emit`, so the CTO inbox never gets it
  and `wake_bridge` is never consulted.
* `agent_waiting_input` is absent from `wake_bridge.WAKE_EVENT_TYPES`, so even if it were
  emitted it would not be eligible.
* `event_pipeline.SIGNIFICANT_KINDS` only covers `waiting_owner`, which is an owner
  decision gate — not a live agent prompt needing a response.

So Owner OS currently has **no durable actionable event for an existing live agent entering
`waiting_input` / prompt-needs-response**.

## 3. Required fix (authorized scope)

1. Emit a durable **edge-triggered actionable** event when a live agent transitions
   `working`/`idle` → `waiting_input` (or explicit prompt-needs-response), deduped by
   **target + conversation/progress fingerprint**. Steady waiting must not re-emit; new
   progress followed by waiting must emit again.
2. Wake selection prioritises actionable waiting events over historical generic backlog.
3. Coalesce/supersede older equivalent generic wakes — one generic wake means "check all
   current Owner OS events" — with a **durable audit of superseded ids**, never silent
   deletion.
4. The global 900s generic anti-spam cooldown must **not** suppress a NEW actionable
   waiting transition. Per-event/state dedupe and the `wake_submitted` latch stay intact.
5. Regressions: working→waiting emits once; steady waiting no re-emit; progress→waiting
   emits again; fresh actionable outranks multi-day backlog; generic cooldown cannot
   suppress a new actionable; historical coalesce is audited; the submission latch still
   prevents duplicates.
6. Focused tests, then a safe non-destructive live E2E against the bound chat.
7. No unrelated production behaviour. No push.

## 4. Relevant code / prior art

| Path | Role |
| --- | --- |
| `core/wake_bridge.py` | wake eligibility, cooldown, target pointer, submission latch |
| `core/control_plane/cto.py:70` | the only place the wake bridge is consulted |
| `core/control_plane/event_pipeline.py` | significant-event publisher (`SIGNIFICANT_KINDS`) |
| `core/agent_watcher.py:151` | `transition_event` — produces `agent_waiting_input` today |
| `core/agent_orchestrator.py:727` | routes transitions to the legacy commander log only |
| `core/control_plane/state_estimator.py` | canonical state estimation / false-idle guard |
| `tools/wake_companion.py`, `tools/cdp_composer.py` | delivery path, `claim_send` choke point |

Prior commits/reports that must stay intact:

* `df24ecf` + `reports/OWNER_OS_WAKE_BRIDGE_REPAIR_2026-08-11.md` — wake idempotency;
  the `wake_submitted` latch **must remain**.
* `461e8c9` + `docs/OWNER_OS_CHAT_REBIND.md` — rebind mechanism.
* PR #15 was opened on `ai-dev-runtime` before this diagnosis — **do not merge**.

## 5. Unrelated, deliberately out of scope

Runtime fallback job **#72** failed only because a full `pytest` run timed out at 300s, and
a later retry provider smoke returned HTTP 404. Recorded here for continuity. Do **not**
broaden into fixing Runtime until the wake fix is complete.

## 6. Outcome — implemented and proven live (2026-08-13 04:40–04:41 UTC)

### What was built

| File | Change |
| --- | --- |
| `core/control_plane/waiting_transitions.py` | **new** — edge-triggered actionable event, deduped by `H(target, conversation_id, progress)`, fingerprint stored per target so a restart mid-wait does not re-announce |
| `core/wake_bridge.py` | actionable class, its own short cooldown, class persisted in `wake_audit`, actionable-first selection, `coalesce_generic_backlog` + `wake_coalesce_audit`, class-aware `claim_send` |
| `core/agent_orchestrator.py` | the existing transition edge now also publishes the CTO event (the commander mirror alone never reached wake selection) |
| `core/control_plane/cto.py` | passes `event_type` into `wake_bridge.record` so the class is auditable |
| `tools/cdp_composer.py`, `tools/wake_companion.py` | carry the class through to the claim |
| `tests/test_wake_actionable_transitions.py` | **new** — 13 regressions, one per proven failure |

Edge vs level is the core of it: waiting is TRUE on every tick, so emitting on the state
would be a poke loop. The event fires on a change of the PROGRESS fingerprint, so steady
waiting announces once and waiting again after new progress announces again.

Two schema migrations run on first touch (`ALTER TABLE` guarded by `PRAGMA table_info`),
because the live DB predates the columns and `CREATE TABLE IF NOT EXISTS` would not add them.

### Test results

* `tests/test_wake_actionable_transitions.py` — **13/13 pass**.
* Pre-existing wake/control-plane suites — **148 pass** (`test_wake_bridge`,
  `test_wake_delivery_verification`, `test_rebind_chat`, `test_agent_watcher`,
  `test_event_pipeline`, `test_control_plane_discovery`, `test_control_plane_state_estimator`).
* Broader related sweep (`-k "orchestrator or wake or continuation or owner or queued or
  watcher or notif"`) — **536 passed**, 0 failed, 190s.

A FULL `pytest` run is not part of this evidence: it exceeds the 300s budget (the same
timeout that failed Runtime fallback job #72, section 5).

### Live E2E against the bound chat

1. **Backlog, before**: 49 pending generic wakes, oldest `3754` from Aug 11 10:52. The
   companion log showed the starvation verbatim, one refusal per poll:
   `not delivered for event 3757; stays pending (not_claimed:global_cooldown_active:848s)`.
2. **Coalesced**: 47 rows superseded into event `3934`, each with a `wake_coalesce_audit`
   row naming the absorbing id. Pending went **47 → 1**. Nothing deleted.
3. **Fresh actionable probe** (`e2e-wake-probe:0.0`, labelled as a probe in its payload —
   no live agent was in `waiting_input`, and fabricating a state for a real agent would have
   put a false record on a real target): `working → waiting_input` emitted event **3937**,
   `event_type=agent_waiting_input`, `actionable=1`.
4. **Anti-spam**: the same block observed again returned
   `unchanged_waiting_fingerprint`, no second event.
5. **Selection**: `pending_wake()` returned **3937** ahead of the generic **3934**.
6. **Delivery**: the actionable wake was claimed as `claimed_actionable` and delivered in
   **~2 seconds**, verified — `submitted_and_user_turn_appeared`. Seven seconds earlier the
   generic path had been refused with `global_cooldown_active:848s`, which is the cooldown
   bypass proven end to end rather than merely unit-tested.
7. **Idempotency**: `wake_submitted` latch set, `acknowledged=1`, exactly **one** delivery
   logged for 3937 and no re-send on subsequent polls.

### Companion restart is REQUIRED for the fix to take effect

`tools/wake_companion.py` imports `core.wake_bridge` once in `main()` and then loops, so a
running companion keeps the old code in memory: at 06:41:12 local it was still grinding the
backlog one event per 900s. `systemctl restart owner-os-wake-companion` was run and the
actionable event was delivered 2 seconds later. **Note this restart loads the working tree**,
so the changes above are live on this host while still uncommitted at the time of the E2E.

### Residue left deliberately

The probe event 3937 and the `agent_waiting_fingerprint` row for `e2e-wake-probe:0.0` remain
in the live DB. They are labelled as a probe in the payload and are not deleted: the event
log is append-only, and quietly removing evidence of a test is the habit this whole audit
trail exists to prevent.

## 7. Constraints

English only. No broad `reset`/`stash`/`checkout`. No push, no external publishing, no
credential/payment/DNS/provider changes. Live E2E must be non-destructive.
