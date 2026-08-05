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

## ✅ GAP CLOSED — the governor is now wired into the live tick (`7b7849f`)

`commander_autopilot.tick` consults `_governor_pass` for governed projects **before** its
ordinary evaluation. Deployed HEAD `7b7849f`, service PID 2161534, suite **1366 passed**,
backup `predeploy-phase3wire-20260805T081128Z`.

Wired behaviour:
- **blocker** → durable `governor_blocker` row + `owner_gate` + a `needs_owner_payload`
  event, recorded **once** per (target, stage, missing-fields). A gate that reopens every
  60s is noise, not signal.
- **submit_queued** → presses Enter on the owner's own queued line under a lease, with the
  standard verification; outside `CANARY_AGENTS` it reports `governor_submit_owner_gated`
  and touches nothing.
- **advance_queue** → reported as available; delivery stays the agent's own advancement
  rule unless it stalls.
- **skip** → falls straight through to the existing autopilot logic.

### Defect the adversarial suite caught during wiring
My governor pass originally ran *before* the progress check. A pane can read `idle` while a
background subagent works, so the governor raised a blocker over live work in flight.
`_governor_pass` now returns immediately when `is_progressing(state, tail)` is true — the
autopilot's own detector is the authority. Pinned by a new test using the real
"✻ Waiting for 1 background agent to finish" render.

Live confirmation right now: MESS is `working` → `progressing: True` → wired pass returns
`None`, so the ordinary path runs and nothing is governed. Correct.

## LIVE EVIDENCE FROM THE WIRED GOVERNOR (2026-08-05)

The governor is now acting inside the production tick, not being invoked by me. Ledger
rows, verified after the fact:

| time (UTC) | target | decision | proof |
|---|---|---|---|
| 08:16:18 | `cp-canary:0.0` | **`governor_submitted`** | `verify: ok=True, queued_input=False, progressed=True` — an owner-queued line was submitted with Enter and real execution followed |
| 08:35:29 | `mess-qa-automation:0.0` | **`governor_blocker`** | `NEEDS_OWNER_PAYLOAD` at `stage_04_security_devices` |

**Durable blocker record, deduped as designed:**
`governor_blocker` holds ONE row — `first_seen 08:35:29`, `last_seen 08:44:37` — i.e. the
condition was re-observed across ticks and the record was refreshed rather than duplicated.
`owner_gate 9016e3eca57342d9 / kind=owner_payload_missing` was opened once.

**Second natural stage transition observed** (no human ping, no interference):
```
08:35  stage_04_security_devices  CURRENT  mess=idle     → blocker:NEEDS_OWNER_PAYLOAD
10:35  stage_04_security_devices  CURRENT  mess=idle     → blocker (still standing)
10:45  stage_04_security_devices  CURRENT  mess=working  → skip (resumed; hands off)
```
Stage 3 media/voice completed and the pointer advanced to stage 4 on its own; the missing
payload for stage 4 was recorded with its four exact field groups (Security modal/Center
titles and row copy, device list + revoke confirm copy, key-verification QR match/mismatch
copy, recovery/backup warning copy).

**One pane throughout** for both `mess-qa-automation` and `cp-canary` — no duplicate agent,
prompt or cwd collision at any point.

### Acceptance status
| criterion | status |
|---|---|
| V8/stage completion updates the pointer | ✅ observed twice, naturally |
| governor observes the completed stage | ✅ |
| missing payload → precise `NEEDS_OWNER_PAYLOAD` blocker | ✅ live, with exact fields, deduped, owner gate opened |
| queued paste submitted exactly once | ✅ live on cp-canary (`governor_submitted`, verified) |
| advance exactly once on grounded work | ⏳ not observed — the MESS agent self-advances per the queue's own `advancement_rule`, so the governor's advance path is a fallback that has not been needed |
| `/clear` resume from the durable queue | ⏳ no `/clear` has occurred; the verbatim instruction extracts cleanly |
| no duplicate prompt/agent/cwd | ✅ |

Two criteria remain unobserved, so the phase verdict stays unclaimed.

## THIRD TRANSITION — and a stall the governor was blind to (2026-08-05 11:23)

```
11:23  stage_05_live_calls  CURRENT  mess=idle     → skip:stage_in_progress   ← WRONG
11:25  stage_05_live_calls  CURRENT  mess=idle     → blocker:NEEDS_OWNER_PAYLOAD  (after fix)
11:43  stage_05_live_calls  CURRENT  mess=working  → skip (resumed)
```

Stage 4 completed and the pointer advanced to `stage_05_live_calls` on its own — the third
natural transition. But the pane then sat **idle**, not progressing, nothing queued, while
the governor returned `skip: stage_in_progress`. Nothing would have nudged it. That is the
stop-and-wait stall, alive again inside my own code.

**Cause.** Stage 5 names a real payload file (`design/v1/screens/CALLS_AND_STATES_V3.json`)
*and* records three `missing_fields`. My check looked only for the literal
`NEEDS_OWNER_PAYLOAD` token, so a payload path read as "fully specified" and the recorded
gaps were ignored.

**Fix (`28c2afb`).** Recorded `missing_fields` ARE the gap, whatever the payload field says.
Live decision immediately afterwards:

```
blocker NEEDS_OWNER_PAYLOAD  stage: stage_05_live_calls
  - participant picker: metrics and state copy
  - active call: control layout, metrics, reconnect/error copy
  - unavailable state copy beyond the existing V3 strings
```

An empty/whitespace `missing_fields` list is pinned NOT to raise a blocker.

### Second defect found in the same pass — a self-inflicted flake
`_governor_pass` read the input line straight from `agent_control`, i.e. from the **live
tmux pane**, so full-suite runs depended on whatever the real canary happened to be
showing (two autopilot tests failed in the suite, passed in isolation). It now reads
`pending` through the injected controller and only falls back to the live read when no
controller is given. Suite went 1366 → **1369 passed** with the flake gone.

Deployed HEAD `28c2afb`, PID 2480752, backup `predeploy-phase3c-20260805T094520Z`.

## FOURTH TRANSITION + an operational finding worth the owner's attention (12:16–12:18)

`stage_05_live_calls` → `stage_06_misc_real_surfaces`; the new stage records one broad
missing-fields entry (folders, scheduled messages, admin panel, update modal, SOS/check-in,
polls, location cards, contact profile — titles, row copy, action labels, states, metrics)
and the wired governor raised the blocker at 12:17 when the pane went idle.

**Blocker ledger is per-stage, as designed:** one row for `stage_04_security_devices`
(08:35→08:44) and one for `stage_06_misc_real_surfaces` (10:17 UTC). No duplicates.
Stage 5's blocker predates the `missing_fields` fix being deployed, so it exists only in
the monitor record, not the ledger.

### Exactly-once: verified, but only after checking
Eight `governor_submitted` events on `cp-canary:0.0` between 08:16 and 10:16 all carry the
SAME `expected_pending` text, three of them inside nine minutes. That looked like a
resubmit loop. It is not: the canary log shows a distinct note appended per submit, so each
event is its own work cycle where the autopilot's paste did not submit and the governor
pressed Enter once. Exactly-once holds **per queued line**.

### ⚠ But the canary is in a make-work busy loop — 619 notes today
`/root/cp-canary-v2/reports/CANARY_LOG.md` contains **619 entries dated 2026-08-05**
(latest: note #830). The canary's registry step is a repeating safe instruction, and since
the re-resumability fix (`4ed8d93`) every new idle cycle produces a fresh progress
fingerprint, so the autopilot re-pokes it on essentially every tick. Each poke burns model
tokens and appends another line.

That fix was correct for real projects — a session must be resumable on each new idle
cycle — but combined with a step that is *always* satisfiable it yields an unbounded loop
on the canary. Nothing unsafe is happening (the work is a dated log line, no external
effect), and no duplicate agents or panes exist. It is a cost and noise problem, not a
safety one.

Not changed unilaterally: capping it means either a per-target poke budget or a canary step
that can complete, and both are owner decisions about what the canary is for.

## GROUNDED VERIFICATION PASS (2026-08-05, no events manufactured)

Nothing was staged, pasted, cleared, crashed or nudged. Only observation and ledger reads.

### 1. The wiring is genuinely exercised by the real tick
Service PID **2480752**, started **11:45:22 CEST** (09:45Z) on HEAD `28c2afb`. Governor
rows written by the production tick **after** that restart:

```
10:07:22  cp-canary:0.0            governor_submitted
10:11:46  cp-canary:0.0            governor_submitted
10:16:16  cp-canary:0.0            governor_submitted
10:17:32  mess-qa-automation:0.0   governor_blocker
```

These are ledger rows produced by the running service, not by me invoking the module — the
distinction that invalidated the earlier "armed" claim. Wiring confirmed live across a
restart boundary.

### 2. Blocker persists and de-duplicates across tick cycles
`governor_blocker`, one row per stage, refreshed rather than duplicated:

| target | stage | first_seen | last_seen |
|---|---|---|---|
| mess-qa-automation:0.0 | stage_04_security_devices | 08:35:29 | 08:44:37 |
| mess-qa-automation:0.0 | **stage_06_misc_real_surfaces** | **10:17:30** | **10:31:56** |

Stage 6's blocker survived **~14 minutes of ticks** as a single row with a moving
`last_seen`. `owner_gate` count for `kind=owner_payload_missing` is **2** — one per blocked
stage, not one per tick.

Stage 5's blocker exists only in the monitor record: it fired before the `missing_fields`
fix reached production, so it never reached this ledger. Stated so the ledger is not
misread as complete.

### 3. Invariants
- **One live pane** each for `mess-qa-automation`, `cp-canary`, `arbitrage2-opus`,
  `payment` — all `dead=0`, no duplicate agent, prompt or cwd collision.
- **Payment excluded** from the governor config (`False`) and the recovery registry
  (`False`).
- **Phase 2 soak untouched and healthy:** recorder alive, **765/1440 samples**, 13.26h,
  last sample 62s old.

### ⚠ One residue worth naming: payment entries remain in the GATE registry
`config/approved_gates.yaml` still carries **2 `payment_standby` entries** (read-only
replication/liveness checks) written before payment was ruled out of scope. They are
currently unreachable — payment is not in `CANARY_AGENTS`, not in the watchdog's eligible
sessions, not governed — and `gate_answer_log` holds no payment rows, so nothing has ever
been answered for it. But they are latent approvals: if payment were ever added to the
eligible set, those approvals would become live without further review.

Not removed unilaterally, since editing the gate registry was not part of this pass.
Recommended: delete both entries so payment exclusion is structural rather than incidental.

## MILESTONE — queued input recovered on a REAL project session (2026-08-05 12:40:27)

Until now the exactly-once submit had only fired on the disposable canary. It has now
fired on `arbitrage2-opus:0.0`, a real project session, from a naturally occurring queued
line — nothing was staged:

```
governor reason: queued_text_unsubmitted   mode: enter
expected_pending: "Resume the approved paper-only audit"
verify: submitted=True prompt_consumed=True queued_input=False progressed=True ok=True
delivered: True        pane state after: working, input line empty
```

The line was submitted with a bare Enter (never re-sent), the input line was consumed, and
real execution followed. Note `conversation_modified: False` — acceptance rested on
`progressed` via the output/working signal, which is exactly the fast-step case the
`0455cc4` fix added; without it this legitimate recovery would have been recorded as a
failure and re-pasted.

### Blocker durability extended
`stage_06_misc_real_surfaces` is still ONE row: `first_seen 10:17:30 → last_seen 12:40:28`
— **~2h23m of continuous tick cycles**, refreshed not duplicated. `owner_gate` count
unchanged at 2 (one per blocked stage).

Governor decisions since 10:40Z, all service-written:
```
11:17:42  mess-qa-automation:0.0   governor_blocker
12:18:43  mess-qa-automation:0.0   governor_blocker
12:20:56  cp-canary:0.0            governor_submitted
12:32:38  cp-canary:0.0            governor_submitted
12:40:27  arbitrage2-opus:0.0      governor_submitted   ← first on a real project
```

Invariants hold: one pane each for mess / canary / arbitrage2 / payment; MESS still idle at
`stage_06` with an empty input line and no invented payload; Phase 2 soak alive and
untouched; service PID 2480752 unchanged.

## DEFECT — owner gates were mislabelled (found 13:00:57, fixed `8e298a0`)

An opaque owner paste appeared in `arbitrage2-opus:0.0`'s input line. That project sets
`submit_owner_queued_paste: false`, so the governor refused — correct. But the refusal was
recorded as `stage='-'`, `fields=[]`, and opened an owner gate labelled
**`owner_payload_missing` / "NEEDS_OWNER_PAYLOAD at -"**.

A refused paste is not a missing payload. `_record_governor_blocker` hard-coded that kind
for every blocker reason, so the owner's gate list read **3** `owner_payload_missing`
entries when only **2** are real (stage 4, stage 6) — the third buried its own actual cause.

Fixed: the gate now carries the governor's real reason
(`governor_owner_paste_not_auto_submittable`), and a fieldless blocker records what it
actually observed instead of an empty `-`. Test asserts a refused paste can never land as
`owner_payload_missing`. Suite **1370 passed**; deployed HEAD `8e298a0`, PID 3155542,
backup `predeploy-phase3d-20260805T131724Z`.

**History not rewritten:** the mislabelled gate row from 13:00:57 remains in the ledger. It
is stale, not recurring.

## OPEN — an owner paste is stuck on arbitrage2

`arbitrage2-opus:0.0` is holding an unsubmitted opaque paste. The governor will not submit
it because that project does not opt in to paste submission (MESS does; arbitrage2
deliberately does not). It moves when the owner submits it, or when the owner decides
arbitrage2 should carry `submit_owner_queued_paste: true`. Not changed unilaterally — the
flag is precisely the "may an agent press Enter on content it cannot read" decision.

## ACCEPTANCE SEQUENCE (owner-directed 2026-08-05) — IN PROGRESS

### 1. Blocker taxonomy — DONE
Gates now carry the governor's real reason. The mislabelled live gate
`819f13b8cc76495d` (opened as `owner_payload_missing` / "NEEDS_OWNER_PAYLOAD at -" for a
REFUSED PASTE on arbitrage2) was **closed via `answer_gate`, not deleted** — the row is
retained, audit event **id 2720** `owner_gate_answered` was appended, and the answer text
records why it was wrong. Open `owner_payload_missing` gates are back to the **2 genuine
ones** (`9016e3eca57342d9` stage 4, `cf206922bb2742ff` stage 6).

### 5. Project-role isolation — DONE (code + tests)
`config/project_queues.yaml` now carries `role`, `allowed_scopes`, `forbidden_scopes` per
project; `check_project_isolation()` refuses cross-project work with an explicit label, and
the check also guards the queued-paste path — even the owner's own queued line is refused
if it would drag a project out of role. Verified:

| target | instruction | result |
|---|---|---|
| mess | "run the payment payout reconciliation" | refused `payment` |
| mess | JobHunter vacancy microtask | refused `jobhunter` |
| arbitrage2 | "place order on the venue with the exchange key" | refused `live_trading` |
| arbitrage2 | rebuild the messenger redesign spec | refused `mess_ui` |
| mess / arbitrage2 | their own in-role step | allowed |
| payment | anything | `project_not_governed` |

### 3. Canary acceptance harness — BUILT, live run pending
`/root/cp-canary-v2/CANARY_EXECUTION_QUEUE.md`: 3 stages, file/report only, with stage C
deliberately unspecified so `NEEDS_OWNER_PAYLOAD` is proven against a genuinely absent
payload. Two grounded governor paths added:
- **artefact present → advance once** (the queue's own `advancement_rule`);
- **idle on an unstarted stage → deliver its verbatim instruction once**.

MESS is unaffected: its stages carry no `instruction` field and it still returns
`NEEDS_OWNER_PAYLOAD` for stage 6.

### Two defects found while building the harness
1. **`advance_queue` was reported, never delivered.** `_governor_pass` short-circuits the
   ordinary autopilot evaluation, so once it claims a target it must also act. It didn't:
   cp-canary would have lost its autopilot poke AND received nothing — a stall introduced
   by the governor itself. It now delivers the queue instruction through the lease-gated
   actuator, guarded by safety classification (`governor_step_unsafe`), allowlist
   confinement (`governor_advance_owner_gated`) and role isolation.
2. **Tick tests were reading production queue state.** Three autopilot/adversarial tests
   failed because the governor consults the shipped config and real queue files. The
   governor is now explicitly isolated in their fixtures. Third occurrence of production
   state leaking into tests in this phase — recorded as a pattern, not just a fix.

### 7. Post-fix soak recorder — BUILT
`tools/phase3_soak.py` + wrapper, detached and restart-persistent, tracking sampling gaps,
duplicate submissions, wrong-project actions, unknown prompt answers, recoveries,
`/clear` resumes and quarantine events. Smoke-tested. Starts after deploy.

### DESIGN CORRECTION — the governor must never type the queue's text (live 14:09:29)

First post-deploy canary tick produced **`governor_step_unsafe`**. The governor had tried
to deliver stage A's own instruction — `"append one dated line to reports/ACCEPTANCE_A.md
describing this stage"` — and the safety classifier refused it.

Measured behaviour of the gate:

| instruction | class |
|---|---|
| `continue with the next safe step` | autonomous_safe |
| `continue with the next safe canary note; append a dated line` | autonomous_safe |
| `append one dated line to reports/ACCEPTANCE_A.md …` | **owner_approval_required** |
| `resume the current stage from the durable queue` | **owner_approval_required** |

The tempting fix — widening the safe vocabulary so the harness worked — would have been
backwards. That gate exists precisely to stop the governor typing arbitrary text into a
live pane, and the queue's rich instruction is arbitrary text from the classifier's point
of view.

**Corrected design:** the governor delivers ONLY the project's own classifier-safe
continuation nudge. The stage content stays in the durable queue for the AGENT to read —
which is what the queue's `RESUME AFTER /clear` instruction already tells it to do. The
queue step is still recorded in the decision (`queue_step`) for audit; it is simply never
typed. This removes an entire class of risk: there is no path where governor-delivered
text is domain content.

Pinned by two tests — the queue's rich text must never appear in the delivered step, and
the delivered step must classify `autonomous_safe`. The earlier test that asserted the
opposite contract was corrected rather than left green on a wrong expectation.

### Deployment of the corrected governor
HEAD **`b760019`**, service PID **3385505**, started 16:28:09 CEST, all six loops up.
Suite **1390 passed, 0 failed** — and the earlier `test_planner_hanging_parent…` failure
did NOT recur, which supports reading it as load starvation during a 13-minute run rather
than a regression. Backup `predeploy-phase3final-20260805T142806Z`.

### Harness gap found immediately after deploy
The first post-deploy tick poked the canary, which dutifully did its OLD registry note —
because nothing told the canary agent that `CANARY_EXECUTION_QUEUE.md` exists. The
governor delivers only a safe nudge (by design, above), so the queue is useless unless the
agent reads it.

Fixed in `/root/cp-canary-v2/CLAUDE.md`: on "continue with the next safe step/canary note"
the agent must FIRST read `./CANARY_EXECUTION_QUEUE.md` and do the stage named by its
pointer, falling back to the old log line if the queue or its instruction is missing; and
re-read the queue after every `/clear`. That is harness wiring inside the disposable
canary, not project work.

### DEFECT — exactly-once violated on the `stage_not_started` path (live 14:28–14:32)

The corrected governor deployed and immediately re-nudged the SAME unstarted stage on every
tick:

```
14:28:29  governor_advanced   stage_a_write_note
14:29:39  governor_advanced   stage_a_write_note
14:30:57  governor_advanced   stage_a_write_note
14:32:xx  governor_advanced   stage_a_write_note      (4 total pre-guard)
```

One nudge per 60s tick, indefinitely. On a real project that is a pane prodded every minute
for a stage it has not begun — exactly-once broken.

**Fix:** per-stage delivery gate `governor_stage_delivery` keyed `(target, stage)`, with a
10-minute cooldown and a 3-attempt cap, then `governor_advance_suppressed`. Per-stage, so
genuine progress to a NEW stage is never blocked. Pinned by three tests: one nudge then
five suppressed ticks; a different stage still nudged; the cap terminating.

**Note the boundary this establishes.** This is the inverse of `4ed8d93`, which made a
session re-resumable on each new idle cycle. Both are right, and the distinguishing signal
is *whether the stage advanced*, not *whether the pane went idle again*. I got that
boundary wrong in both directions before landing on it, which is worth recording as the
actual lesson rather than filing two isolated fixes.

### Harness limitation discovered
An already-running Claude session does not re-read `CLAUDE.md`, so the canary could not see
the new queue instruction and kept doing its old log note (artefact `ACCEPTANCE_A.md` never
appeared). This is not a governor defect — and it is resolved by the `/clear` that item 3
of the acceptance requires anyway. `PROJECT_STATE.md` has been written to the canary in
preparation, as item 3 specifies.

### Still to do before any PASS
Full suite → backup → deploy → verify recurring ticks → post-deploy canary tick → the live
deterministic canary run (A → idle → B once → no resubmit → restart durability → real
`/clear` + resume). Item 4 (MESS/Arbitrage2) remains observational and non-interfering.

## Remaining unproven (unchanged)
- **advance exactly once on grounded work** — the MESS agent self-advances per the queue's
  own `advancement_rule`; the governor's advance path is a fallback that has not been
  needed. Four natural transitions have all been agent-driven.
- **`/clear` resume from the durable queue** — no `/clear` has occurred. The verbatim
  resume instruction extracts cleanly, but extraction is not proof of use.

No PASS is claimed. Status: **WORKING, monitored.**

## Superseded — the wiring gap as first reported

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
