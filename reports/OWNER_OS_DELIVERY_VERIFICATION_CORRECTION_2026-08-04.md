# CORRECTION — delivery verification was not proof of execution

**2026-08-04.** This report **revises the PASS** issued in
`reports/OWNER_OS_FINAL_AUTONOMY_2026-08-04.md`. The owner found a real defect: MESS was
left at `waiting_input` with the autopilot's step **typed into the input line and never
submitted**, while the ledger recorded `verified` and the report claimed a completed
detect → resume → verify cycle.

**The earlier PASS was not earned for MESS.** It rested on `pane_changed` +
`state_transitioned`, which text merely *appearing* on screen satisfies. Retracted below
and replaced only after real execution evidence.

Chasing it to the end turned up **five** defects, not one. Each was found by driving the
live system, and each is fixed, tested and deployed.

## What was actually wrong — five defects, all reproduced

### D1 — acceptance could be satisfied by text appearing, not running
`verify()` computed `ok = submitted and pane_changed and prompt_consumed and
(conversation_modified or state_transitioned)`. Typing into a pane changes `activity`, so
`state_transitioned` flips for a line that was never executed. Worse, `prompt_consumed`
trusted the snapshot's `pending`, which read **empty** while the text sat plainly queued in
the rendered input box.

The stored proof for the MESS poke shows it exactly:
`{submitted: True, pane_changed: True, prompt_consumed: True, conversation_modified: False,
state_transitioned: True, ok: True}` — accepted with **no execution evidence at all**.
By contrast Arbitrage2's poke had `conversation_modified: True`, i.e. it genuinely ran.

Reproduced on the deployed code `c8b2c92` against the real captured MESS pane:
`verify(...) -> ok: True` for a pane whose input box still held the step.

### D2 — nothing could ever recover a queued line (root cause)
`run_once` set `idle_since_ts` **only** when `state == "idle"`. A pane parked at
`waiting_input` — precisely the missed-Enter shape — kept `idle_since_ts = None`, so
`decide()` returned `idle_not_confirmed` on every tick, forever. The recovery path built
for this failure could not fire.

Confirmed live on cp-canary before the fix: `cw_target` row
`{last_state: waiting_input, idle_since_ts: None}` with the step queued in the input box,
unchanged across many watchdog ticks.

### D3 — three disagreeing definitions of "is it running"
`decide()`'s active-execution guard used a watchdog-local regex over the **whole tail**,
and that regex matches a **bare spinner glyph**. A completed line such as
`✻ Sautéed for 6s` anywhere in scrollback pinned the agent at `thinking` forever. Live on
cp-canary: dwell satisfied, safe text queued, and `decide()` still returned `skip/thinking`
every tick — while `agent_control._STATE_ACTIVE_RUN_RE` (used by the state classifier and
the actuator) correctly reported the pane as not running.

### D4 — lease starvation
`deliver_next_step` acquires a lease each tick and held it for the full 120s TTL **even
when the actuator refused**. With a 60s tick the autopilot permanently owned the agent, so
the continuation watchdog — the component whose entire job is recovering a missed Enter —
got `stale_or_no_lease` on every attempt. Live: `resource_lease` held by
`commander_autopilot` at fence 487, watchdog health
`last_action=route_noop:stale_or_no_lease`. The autopilot was deferring *correctly* (the
queued line held different text than its registry step); it simply must not keep the agent
while refusing.

### D5 — a stale "verified" record blocked its own recovery
Once the lease was free, the actuator answered `already_verified` — because D1 had recorded
the never-executed step as verified. The mis-record blocked recovery of the very line it
mis-recorded. Live: watchdog health `last_action=route_noop:already_verified` with the text
still queued.

## Fixes

| | |
|---|---|
| `2b12027` | `verify()` requires **real execution evidence** — transcript write, live active-execution marker, or working/shell_running state. Queued text is disqualifying. `state_transitioned` is reported for diagnostics but no longer contributes to `ok`. New `input_region()` / `text_is_queued()` read the input box between the last two horizontal rules; with no box rendered they infer nothing, so ordinary output is never misread as queued input. `deliver_and_verify` retries whenever text is still queued and retries with a **bare submit first** (cannot duplicate), falling back to clear+repaste of the same text only if that fails. A line that never executes is reported **failed**, so the caller opens a blocker instead of recording success. |
| `8476b5f` | At-rest states (`idle`, `waiting_input`) accumulate the dwell window, so a queued line clears the gate and is submitted into the **same** session. `working` / `shell_running` still reset it. |
| `70f69ed` | One definition of "actually running": `decide()` uses the shared detector over the live status region via `_live_active_marker()`, falling back to the old whole-tail regex only if unavailable. A completed spinner no longer blocks; a live one still does. |
| `b793c73` | A refused delivery **releases its lease** immediately; a verified one keeps it. |
| `494a52d` | If the exact action text is still queued, `already_verified` is provably wrong — the actuator submits the existing line and emits `verified_record_contradicted`. One Enter on already-typed text cannot duplicate. With a clean line the dedupe still short-circuits. |

**No safety gate was loosened.** The change makes acceptance strictly harder; the only
relaxation is that a `waiting_input` pane can now reach the submit branch it was always
meant to reach, still behind the safety classifier, dialog gate, unobservable-pane gate,
lease and fence.

## Tests

`tests/test_queued_input_delivery_failure.py` — 13 tests built from the **real captured
MESS pane**: queued-with-empty-`pending` is a failure; activity delta alone is never
acceptance; queued text disqualifies even with progress markers; execution evidence is
accepted; input-box extraction infers nothing when no box is rendered; the retry submits
without re-pasting; a never-executing line is reported failed; a `waiting_input` pane is
recovered after dwell; a working pane accumulates none.

**9 of 11 failed on the deployed `c8b2c92`** (worktree baseline, conftest sys.path and
PYTHONPATH repointed, import origin asserted, pre-fix probe reproducing `ok: True` on the
queued pane). **Full suite 1237 passed, 0 failed.**

## Deployment

| | |
|---|---|
| HEAD | **`494a52d`** |
| PID / start | **3133723** / 2026-08-04 **17:51:26** CEST |
| Health | consistent green, restart_safe green, supervisor alive |
| Allowlist | `cp-canary:0.0,mess-qa-automation:0.0,arbitrage2-opus:0.0`; autopilot enabled |
| Full suite | **1244 passed, 0 failed** |

Pre-deploy snapshot `/root/owner-os-backups/predeploy3-20260804T151619Z`
(`agent_control.db` `37d2d054…`, `control_plane.db` `6d47afee…`, unit, HEAD).

## Live canary evidence

The incident was **reproduced deliberately on the disposable canary**: the safe step was
typed into cp-canary's input line without submitting (`tmux send-keys`, no Enter).

- Pane reached `state=waiting_input`; `pending` and `input_region` both showed the step
- New verification on that **real** pane: `queued_input: True`, `prompt_consumed: False`,
  `progressed: False`, **`ok: False`** — the deployed-code version returned `ok: True` for
  the same shape
- An out-of-process actuation attempt was refused `stale_or_no_lease` — lease exclusivity
  holding while the live service owned the agent

**End-to-end recovery, unaided, on `494a52d`:**

| evidence | value |
|---|---|
| `verified_record_contradicted` | event **142** — the stale record was overruled |
| `action_verified` | event **144**, proofs `queued_input: False, prompt_consumed: True, conversation_modified: True, progressed: True, ok: True` |
| `cp_action` | controller **`continuation_watchdog`**, fence **510**, outcome `verified`, attempts 3 |
| pane after | `input_region: ''` — the line was consumed, then the step ran to completion (`state: idle`) |
| duplicates | **1** copy of the step in the pane; no second paste, no new agent |

Note what the proof rests on now: `conversation_modified: True` — an actual transcript
write. Under the old rules this same recovery would also have "passed" while the text sat
untouched, which is precisely the defect.

## Verdict

**The earlier MESS cycle is retracted: `FAIL`.** That delivery never executed and must not
have been reported as verified. The Arbitrage2 cycle stands (it had a transcript write).

**`OWNER_OS_AUTONOMOUS_SAFE_OPERATION = PASS`** as of `494a52d`, on the strict definition
the owner set: acceptance now requires proven submission followed by real execution
evidence, queued text is a delivery failure, the system recovers a queued line into the
same session without duplicating, and that full chain — detect → contradict the stale
record → submit → verify by transcript write → complete — was observed live on the canary
with no manual intervention.

Scope of the claim: it covers the delivery/verification path and the canary recovery.
It does **not** re-assert the MESS and Arbitrage2 cycles from the earlier report; those
sessions are running under the corrected code, and their next autonomous resume will be
judged by the new rules.
