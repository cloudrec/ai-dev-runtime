# OWNER OS — AUTONOMY PHASE 3: CONTINUATION GOVERNOR (LIVE)

**Status: WORKING — natural V8 completion observed; NEEDS_OWNER_PAYLOAD blocker proven live. Remaining acceptance items pending.** Persisted 2026-08-05 before any work, so context
compaction cannot lose it. Phase 3 begins ONLY if the Phase 2 6-hour checkpoint is clean
enough to continue; Phase 2 evidence lives in `OWNER_OS_AUTONOMY_PHASE2_CURRENT.md` and is
kept separate from this file.

## Goal

Eliminate the exact MESS stop-and-wait failure pattern: an agent that is alive but parked,
or holding a queued-but-unsubmitted prompt, while approved work sits waiting in a durable
project queue.

## Requirements (owner)

1. **Detect**, for allowlisted non-Payment agents only: live-but-idle, `waiting_input`,
   `waiting_owner`, and queued-but-unsubmitted tmux input — including the `Pasted text`
   marker, `Press up to edit queued messages`, and plainly visible text in the input line.
2. **Read a durable per-project execution queue + current pointer.** MESS source:
   `/opt/mess/design/v1/REDESIGN_EXECUTION_QUEUE.md` plus `/opt/mess/reports/PROJECT_STATE.md`.
   **Never invent work beyond the persisted queue/spec.**
3. **Act exactly once.** A visibly queued but unsubmitted approved task → submit once and
   verify pane/conversation modification. A completed stage with a grounded next queue item
   → send it once. No duplicate prompts, panes, agents, or cwd collisions.
4. **Handle `/clear` / compaction:** verify the new conversation starts in the same
   tmux/cwd, then send the exact durable resume instruction and confirm the agent rereads
   state before working.
5. **Missing design payload → genuine owner blocker** naming the exact missing fields.
   Never fabricate design, never spin.
6. **Boundaries unchanged:** Payment excluded. Arbitrage2 paper-only. No real orders,
   payments, credentials, publishing, destructive operations. **Fable prohibited.**
7. **Tests:** queued-input detection, one-copy submit, stale queue pointer,
   completion→next transition, clear/resume, duplicate prevention, missing-payload blocker,
   deliberate hold, crash-loop quarantine.
8. **Live acceptance on MESS without disrupting its current V6 work:** observe at least one
   NATURAL stage completion and prove the governor advances it or correctly blocks it, with
   no human ping. **Do not manufacture a risky failure.**
9. **Deploy only after backup + full suite.** Opus owns architecture, deploy and final
   review. Verdict `OWNER_OS_AUTONOMY_PHASE3 = PASS / PARTIAL / BLOCKED`, evidence only.

## Preconditions

- Phase 2 6h checkpoint file `reports/phase2_soak_6h_checkpoint.json` exists and is clean
  enough to continue. **Not yet met at the time of writing.**
- Phase 2 soak recorder and evaluator must remain untouched — no restart, no disturbance.

## Progress log

| when | item | status |
|---|---|---|
| 2026-08-05 | assignment persisted | done (`a9c8c5a`) |
| 2026-08-05 05:28 | phase 2 6h checkpoint | **PARTIAL** — clean on every safety criterion; failed only on the MESS stall, which is this phase's target |
| 2026-08-05 | governor + config + 19 tests | done (`b9ce855`) |
| 2026-08-05 | full suite | **1349 passed, 0 failed** |
| — | deploy + live acceptance on a natural MESS stage completion | pending |

## Gate decision

Phase 2's checkpoint was `clean: false`, so I did not upgrade it to PASS — it stands at
PARTIAL. I judged it "clean enough to continue" because no Phase 2 safety property was
violated (0 duplicates, 0 unapproved answers, 0 gaps, 0 quarantines, service active
throughout) and the single failing criterion — MESS parked 37 minutes on
`[Pasted text #3 +99 lines]` — is precisely what Phase 3 exists to govern.

## BLOCKER RESOLVED — the owner's agent authored the queue during V8

`/opt/mess/design/v1/REDESIGN_EXECUTION_QUEUE.md` now exists (created 07:19, rewritten
08:11) and **validates**. It arrived in a far better shape than the markdown I had planned
for: a `MACHINE-READABLE STATE` YAML block plus a verbatim post-`/clear` resume
instruction.

```
pointer: stage_02_invites      branch: fable-0.1.91-realdevice-ux
cwd: /opt/mess                 deploy_allowed: false
completed: 7 entries           stages: 10
```

The parser was rewritten against the real format (YAML first, markdown retained as a
fallback), and `config/project_queues.yaml` now points at this file — PROJECT_STATE is no
longer treated as the queue, per the owner's correction. Live check after deploy:
`queue valid: True | pointer: stage_02_invites | status: IN_PROGRESS`, and the governor
correctly returns `skip` while V8 is in flight. **V8 work was never touched.**

### Three defects found while building against the real file
1. **Placeholder would have been typed as text.** The first decision returned the pane's
   `[Pasted text #3 +99 lines]` placeholder as `step_text`; delivering it would have typed
   that literal string into MESS instead of submitting the owner's real 99-line paste. A
   queued line is now submitted with a bare Enter — `mode: enter`, no `step_text`.
2. **False `NEEDS_OWNER_PAYLOAD`.** The first parser matched the token anywhere, so stage 2
   was flagged as blocked because its body *instructs* "if absent, record
   NEEDS_OWNER_PAYLOAD". Anchored to line-start records; the resolved stage-1 marker is
   excluded too.
3. **Invalid YAML was masked.** When a machine-readable block existed but was mid-write,
   `parse_queue` fell through to the markdown parser and returned *its* error — making
   "the owner's agent is writing right now" indistinguishable from "this is a legacy file".
   The fallback now applies only when no YAML block exists at all; a mid-write queue is a
   WAIT.

I also removed a path I had invented for arbitrage2 (`reports/ACAP_STATE.md`, which does
not exist) rather than leave a fabricated source in config.

## Deployment

| | |
|---|---|
| HEAD | `18260ff` |
| Service | `ai-runtime.service` active, PID 1808767 |
| Suite | **1358 passed, 0 failed** |
| Backup | `/root/owner-os-backups/predeploy-phase3-20260805T062310Z` (incl. a snapshot of the queue file) |

## Live acceptance — NATURAL V8 completion observed (2026-08-05 08:46)

Nothing was manufactured and V8 was never touched. The monitor recorded the transition as
it happened:

```
08:26  pointer=stage_02_invites     status=IN_PROGRESS  mess=working  decision=skip
08:46  pointer=stage_03_media_voice status=CURRENT      mess=working  decision=skip
08:48  pointer=stage_03_media_voice status=CURRENT      mess=idle     decision=blocker:NEEDS_OWNER_PAYLOAD
```

### ✅ V8 completion updates the pointer
The MESS agent finished stage 2 and advanced the queue itself
(`stage_02_invites` → `stage_03_media_voice`); `completed` now carries 7 entries, the last
being `v7_group_flows / PASS / 25206a2`. The governor read the moved pointer immediately.

### ✅ Governor observes the completed stage and does not interfere
While the agent was working, every decision was `skip` — `stage_in_progress` /
`nothing_queued_and_stage_incomplete`. It only spoke once the pane went idle.

### ✅ Missing payload raises a precise NEEDS_OWNER_PAYLOAD blocker
Live, post-deploy, against the real queue:

```
decision: blocker  reason: NEEDS_OWNER_PAYLOAD  stage: stage_03_media_voice
blocker_fields:
  - image/media viewer: title, chrome, actions, zoom/close affordances, metrics
  - file surface: row copy, size/type presentation, download/open action labels
  - voice: recording, preview, send and failure state copy + metrics
```

Exact fields, quoted from the owner's own queue. No design invented, no spinning.

#### Defect this case caught — and it was caught by the live run, not the tests
The real queue records the blocker as **`payload: NEEDS_OWNER_PAYLOAD`** while `status`
stays `CURRENT`. My parser checked `status` only, so it read `needs_owner_payload: False`
and would have returned a silent `skip` on an idle agent — the exact stall this phase
exists to remove, reintroduced by my own code. Now `status`, `payload` and `blockers` are
all consulted, `missing_fields` is surfaced, and three regression tests pin it (including
an anti-overcorrection test that a fully-specified payload does NOT block).

### Still outstanding for a PASS
- **advance exactly once on grounded work** — not yet observed live. The MESS agent
  advances its own pointer per the queue's `advancement_rule`, so the governor's advance
  path is a fallback that has not been exercised naturally yet.
- **queued pasted input submitted once, live** — proven in tests; not yet reproduced
  naturally since the 37-minute stall.
- **`/clear` resume from the durable queue** — the verbatim instruction is extracted and
  available (`resume_instruction available: True`), but no `/clear` has occurred.
- **no duplicate prompt/agent/cwd collision** — one `mess-qa-automation` pane throughout.

## STANDING STATE — re-verified 2026-08-05 (post-deploy)

| check | value |
|---|---|
| queue valid | yes — `pointer: stage_03_media_voice`, `status: CURRENT` |
| needs_owner_payload | **true** (3 exact fields, unchanged) |
| branch / deploy_allowed | `fable-0.1.91-realdevice-ux` / `false` |
| MESS pane | `idle`, input line empty, **one pane** |
| governor decision | `blocker: NEEDS_OWNER_PAYLOAD` |
| duplicates | none — 1 pane each for mess / canary / arbitrage2 / payment |
| Phase 2 soak | alive, 597/1440 samples, 10.28h, last sample 10s old |

The media/voice design is NOT invented and MESS is not being interfered with. The blocker
stands as the correct terminal answer for stage 3 until the owner supplies the payload.

## ⚠ GAP — the governor is deployed but NOT WIRED IN

`core/continuation_governor.py` is imported by **nothing** in `core/` or `api/`:

```
grep -rn 'continuation_governor' core/ api/   → no hits outside the module itself
```

Every governor decision recorded in this report — including the live
`NEEDS_OWNER_PAYLOAD` blocker — was produced by invoking the module directly during
verification. The logic is proven against real live state, but no running loop calls it,
so it is **not autonomously governing anything yet**, and it does not persist blockers to
a durable ledger.

"Armed" is therefore currently **false**. Wiring it into the watchdog/autopilot tick is a
code change plus a service restart; MESS is mid backup-remediation and the owner asked for
no interference, so it is NOT being done unannounced. It is the next step and needs an
explicit go-ahead.

This does not change any evidence above — it changes what may be claimed from it.

## Deployment (current)

HEAD `ab39d65`, service PID 1907051, suite **1361 passed**, backups
`predeploy-phase3-20260805T062310Z` and `predeploy-phase3b-*` (each with a queue snapshot).

## Superseded — original blocker text

`/opt/mess/design/v1/REDESIGN_EXECUTION_QUEUE.md` is **not present**, and a search of
`/opt/mess` for `*EXECUTION_QUEUE*` / `*REDESIGN_EXECUTION*` finds nothing. The directory
holds payload manifests and design assets only.

The second declared source **does** exist: `/opt/mess/reports/PROJECT_STATE.md` (170 KB)
with an owner-ordered `▶▶ EXECUTE NEXT (2026-08-05)` section naming two concrete JSX
implementations and their authoritative specs.

**Exact missing field:** `file:/opt/mess/design/v1/REDESIGN_EXECUTION_QUEUE.md`.

Needed from the owner, one of:
1. the real path of the execution queue, or
2. confirmation that `PROJECT_STATE.md` §`EXECUTE NEXT` alone is the authoritative queue
   and pointer for MESS.

Until then queue-driven ADVANCEMENT for MESS is blocked by design — the governor reports
the blocker instead of guessing. Queued-input submission does not depend on it and works.

## What was built

`core/continuation_governor.py` + `config/project_queues.yaml`:

- **Detection** — `pending` (ghost-aware), `[Pasted text …]` markers, and the
  `Press up to edit queued messages` hint.
- **Submit exactly once, by ENTER.** A defect caught during construction: the first version
  returned the pane's placeholder as `step_text`, which would have TYPED
  `[Pasted text #3 +99 lines]` instead of submitting the owner's real content. A queued line
  is now submitted, never re-sent; the decision carries `mode: enter` and no `step_text`.
- **Advance only on written-down work** — the next item is quoted from the durable pointer
  section and stops at the next heading. Missing source or missing section ⇒ owner blocker
  naming the exact file/section.
- **Scope** — payment absent from the config and pinned absent by test. For arbitrage2 I
  declared NO queue source: the owner named one only for MESS, and inventing a path is the
  very failure this phase forbids. I initially guessed `ACAP_STATE.md`, found it did not
  exist, and removed it rather than leave a fabricated path in config.

### Deliberate policy note, stated plainly
`submit_owner_queued_paste: true` for MESS lets the governor press Enter on an opaque
owner paste it cannot read. That is a real widening: the content is unclassified. The
justification is that the text is the OWNER's own, already sitting in their input line, and
pressing Enter restores their intent rather than authoring anything — which is exactly the
stall the owner asked to eliminate. It is per-project, default off elsewhere, and audited.
