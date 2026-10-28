# STOP → wake → ChatGPT → next task without owner pokes — 2026-10-28

Requested by an automated instruction (ChatGPT via Owner OS); nothing here is owner
approval. Local branch only: no restart, deploy, config, secret, grant or push.

Branch `ai-runtime/stop-wake-closed-loop` (worktree `/root/ai-dev-runtime-stop-wake`),
base `cc38238` (= live `/root/ai-dev-runtime` HEAD; wake companion restarted 02:57 CET on it).

## What was observed (live DBs, read-only, last 24 h)

GAIKA is driven by ChatGPT through active grant `gba3c9487e88c` (`api:bearer/mcp-token-3`,
owner_cli, expires 2026-11-03). Stop → next ChatGPT `agent_send` latency for
`gaika-country-ru`: mostly 14–800 s, but the stops at **02:25:50, 03:26:28, 04:45:25 UTC**
waited **3597 s, 4725 s and 35+ min (ongoing)**. `owner_intervention` was recorded at 02:25
and 03:21, so the owner had to poke the chat by hand.

`agent_turn_stopped` being routine is **not** the defect. The Stop hook fires every turn
(421/day), so waking on it would be a storm. The native supervisor tried to continue on
those turns 304 times and was refused every time
(`not_delivered:automation_target_not_allowlisted`), which is correct under
`AGENT_AUTOMATION_POLICY=enforce`. After that, the quiescence rule (`agent_watch`, 300 s)
emits `work_stopped_incomplete`. That event is actionable, has trigger class `completion`,
and wakes the agent's project route. That part works: there were 41 such wakes in the day.

The losses happen downstream of that wake:

1. **A send that may have reached the chat but was never verified is dead for 3 h.** When
   the page takes the phrase, `cdp_composer` latches `wake_submitted`. That latch correctly
   forbids a second send of the event. If the user turn is then never observed
   (`user_turn_not_observed_after_send`), the companion does not acknowledge the event and
   the closed-loop watchdog does not watch it. Nothing follows up until
   `record_abandoned_wakes` retires it after `MAX_WAKE_AGE_SECS`. There were 26 such
   actionable wakes in 7 days. Example: event 73945 (gaika-ru `work_stopped_incomplete`,
   04:52:57, unverified send at 04:54:31) is the 04:45 stall.
2. **The watchdog suppressed every GAIKA watch.** `closed_loop_wake._resolution_reason`
   resolved a watch as `pane_awaiting_owner` whenever any `owner_gate` was open for the
   agent. `discovery` opens a `classify_scope` gate ("unknown-scope agent at …") for every
   agent outside the static scope map, and nobody answers those gates (45 are open). Every
   watch for such an agent was therefore resolved within about 1 s and never re-woken.
   Over 7 days that hit 19 of 34 watches, including every gaika-country-ru watch.
   `owner_status.DIAGNOSTIC_GATE_KINDS` already classifies these gate kinds as diagnostic,
   not as owner decisions.

## Change (2 files + tests)

- `core/closed_loop_wake.py`: `pane_awaiting_owner` now counts only **decision** gates
  (`kind NOT IN owner_status.DIAGNOSTIC_GATE_KINDS`). `register_delivery(verified=False)`
  is added, with a new column `wake_loop_watch.delivery_verified` (migrated, default 1).
- `tools/wake_companion.py`: when an actionable wake fails and `wb.was_submitted(event)`
  is true, the companion now registers the watch with `verified=False` and still does not
  acknowledge. `_latched` fails closed, so any doubt keeps the old behaviour.

Safety properties, unchanged or tested:

- **Idempotence.** The original event is never sent twice, because of the latch.
  `register_delivery` is `INSERT OR IGNORE`.
- **Bounded follow-up.** A re-wake is a new event (`wake_loop_no_progress`, dedup key
  `wakeloop:<id>:no_progress`), followed by at most one escalation. After that the watch
  is terminal.
- **No self-feeding.** The watchdog never watches its own re-wake events.
- **No storm.** An older watch resolves `progress_observed` as soon as any newer event
  exists for the agent, so at most one watch per agent is effectively open. Within an SLO
  window, repeated scans emit nothing.
- **No new authority.** No pane text is forwarded. The phrase still carries only the
  event id, a closed trigger class, the route key and a sanitized agent reference. The
  change touches no grant, send policy or creation policy, and gives agents no ability to
  lead themselves: only ChatGPT acts, under its existing grant.
- **Real owner decisions still suppress re-wakes**, e.g. `owner_approval_required`.

Replay over the last 7 days of live data: none of the 19 `pane_awaiting_owner` resolutions
would still be suppressed. The 26 unverified sends would each have been watched, and each
would have produced at most one re-wake and one escalation.

Known cost: re-wakes and escalations are `owner_action_required` / high severity, as they
were before. A stall that stays stalled now also queues an owner Telegram push, which
already happens for verified wakes. Eight of the 26 unverified wakes were about
`payorch-ha-fresh`, which stays hard-denied for sends. Its wakes already reach ChatGPT
today, and this change adds no actuation path to it.

## Tests

`tests/test_stop_wake_closed_loop.py` (11) runs the real `cto.emit` → `wake_bridge`
(decision, route, pending) → worktree `tools/wake_companion.tick` →
`closed_loop_wake.slo_scan` chain. Only the browser is faked. It covers:

- **Two-turn chain.** A stop wake is taken but unverified, is watched, and gets exactly one
  re-wake in gaika's own chat with `trigger=loop_watchdog`. The re-wake is verified, each
  event is submitted once, and agent progress then resolves the watch with no escalation.
- **Still stalled.** Exactly one re-wake and one escalation, then quiet.
- **Gate kinds.** A decision gate still suppresses; all four diagnostic gate kinds do not.
- **Unchanged paths.** A refused send (no latch) is not watched and is retried as before. A
  non-actionable unverified wake is not watched.
- **Helpers.** `register_delivery` idempotence; `_latched` fails closed.

Three tests in `tests/test_closed_loop_wake.py` used `classify_scope` as their example
"owner gate". They now use `owner_approval_required`, so they keep testing what they claim.

Sabotage checks:

- Removing the gate-kind filter fails 6 tests.
- Removing the companion's unverified watch fails 2 tests.

Full suite: **3996 passed / 0 failed / 4 skipped** (base `cc38238`: 3985 passed / 0 failed / 4 skipped; +11 new). `tests/test_closed_loop_wake.py` + new file: 106/106.

## Not changed (findings for follow-up)

- The native supervisor makes 300+ continuation attempts a day that the send policy
  refuses (event path 74, idle sweep 230). The refusals are harmless but repeat every
  300 s for each target. A pre-check against `agent_control.automation_decision`, with a
  terminal skip, would remove them. That touches the live supervisor loop, so it is left
  for a separate change.
- Of the companion's verification failures, `user_turn_not_observed_after_send` happened
  27 times in a day. It may be a selector timing issue after the 2026-10 ChatGPT UI change
  (`cc38238`). This change bounds the damage but does not fix the root cause.
- 45 `classify_scope` gates have been open since September. Answering or retiring them is
  an owner or ops decision.

## Live proof needs an owner-gated deploy

The chain is reproduced in tests on real modules. Proving it live needs the wake companion
running this code, which means a restart of `owner-os-wake-companion.service`. That is
owner-gated. The runtime API (`ai-runtime.service`) does not need a restart: the slo_scan
and the companion both run inside the companion process.

Suggested owner sentence:
"I approve fast-forwarding /root/ai-dev-runtime to ai-runtime/stop-wake-closed-loop <commit> and restarting only owner-os-wake-companion.service, with rollback to cc38238 on failure."

- **Rollback:** `git -C /root/ai-dev-runtime reset --keep cc38238 && systemctl restart owner-os-wake-companion.service`.
  The added column is additive and harmless if left in place.
- **Acceptance after deploy:**
  - the next actionable `user_turn_not_observed_after_send` has a `wake_loop_watch` row
    with `delivery_verified=0`;
  - after 900 s without progress there is one `wake_loop_no_progress` delivered to the
    route chat;
  - no `pane_awaiting_owner` resolutions for agents that have only `classify_scope` gates.

## Follow-up (same branch): the two findings above, fixed locally

Requested by an automated instruction. Not approval. Local commits only; nothing deployed.

### Native supervisor send-policy pre-check — `1c4a102`

Before calling `send_fn`, `native_supervisor.scan` now asks `agent_control.automation_decision`.
It passes the same session, target, actor (`native_supervisor`), action (`agent_send`) and
pane cwd that `_deliver` uses.

- **Refuse-only.** The pre-check can only refuse. If it answers "", or raises, the real
  send runs, and `_deliver` remains the single enforcing point.
- **Audit.** A refusal is recorded with action `policy_refused`, the same reason
  `not_delivered:<refusal>`, ok=0 and `{"precheck": true}`.
  - It never counts as a continuation, so it cannot trip `MAX_CONSECUTIVE`.
  - The event is consumed exactly as a refused send consumed it.
- **Backoff.**
  - The idle sweep re-asks a refused target only after
    `NATIVE_SUPERVISOR_POLICY_REFUSED_BACKOFF_SECS` (3600).
  - The event path is never backed off, so an allowlist edit or a grant takes effect on
    the next turn.
- **Wiring.** By default the pre-check sits only in front of the real
  `agent_control.agent_send`. A caller that injects its own `send_fn` passes its own
  `policy_fn`.
- **Expected effect** at today's rates:
  - about 230 idle-sweep sends a day become about 2 recorded refusals per target per day;
  - the about 74 event-path sends become recorded refusals without the pane lookup and
    audit line of a pretend send.
- **Tests:** `tests/test_native_supervisor_policy_precheck.py`, 9 tests.
  - Covers the refusal before any send, the unchanged audit reason, event consumption,
    allowlisted targets still sending, and a policy change applying on the next turn.
  - Covers the payment cwd hard deny, errors never authorizing, the grant lookup
    happening as the in-process actor, idle-sweep backoff, and the cap not being counted.
  - Sabotage: removing the event-path or the idle-path pre-check fails 2 tests each.

### `user_turn_not_observed_after_send` — `5aad66a`

**Diagnosis.** Since the companion restart (01:57 UTC), 15 of 48 attempts ended this way.

- They occurred only on the payment-orchestrator chat (`…6a97e63a`, 8 failures and 4
  successes) and the GAIKA chat (`…6ae0f469`, 7 failures and 3 successes).
- The owner-os chat had 15 successes and 0 failures.
- 9 of 21 attempts made right after the single browser tab was navigated to another chat
  failed this way, against 6 of 27 attempts in the same chat.
- The GAIKA chat is new (38 attempts in total), so chat length or virtualization alone
  does not explain it.
- The live DOM could not be inspected read-only: the browser has one tab, sitting on
  `chatgpt.com/`, and reaching a chat would mean navigating the owner's browser. The exact
  markup cause is therefore **not proven**.

**Local defect fixed.** When the user-turn proof missed, the verifier ignored the
assistant side. The assistant side is independent evidence: the back-pressure check
already establishes that nothing was generating before the send. Now any of the following
proves the message landed, and the attempt continues to the existing
`_await_assistant`/`_settled` checks:

- the stop control appears (it was known to be absent before the send);
- a new assistant turn appears;
- the newest assistant id changes.

An unreadable value, or an unknown pre-send state, is not evidence. The post-click window
is `WAKE_USER_TURN_SECS`, default 20 (previously a fixed 10). The longer wait applies only
on the failure path. The latch and the no-resend behaviour are unchanged.

**Tests:** `tests/test_composer_assistant_evidence.py`, 8 tests. Sabotage: removing the
evidence branch fails 3; restoring the 10 s window fails 1. The older composer, delivery,
companion and closed-loop suites hard-code the live path, so they were also run against
this checkout: 273/273.

**Residual.** If the user turn and the assistant both genuinely fail to appear, the
outcome stays `user_turn_not_observed_after_send`. Commit `dfe9174` covers that case with
the watchdog's single re-wake. Live confirmation needs the companion running this code.

### Test counts (head of this follow-up)

Full suite: **4013 passed / 0 failed / 4 skipped**. Base `cc38238` was 3985; this adds
11 + 9 + 8 new tests.

### Owner-gated step (unchanged scope; new head)

"I approve fast-forwarding /root/ai-dev-runtime to ai-runtime/stop-wake-closed-loop <head> and restarting only owner-os-wake-companion.service, with rollback to cc38238 on failure."

All three changes run in the companion process (`native_supervisor.scan`, `cdp_composer`,
`closed_loop_wake`). `ai-runtime.service` does not need a restart.

## Independent audit and deploy gate (2026-10-28, later)

Trigger: an automated instruction was received via the Owner OS API (a relay that
claims an owner YES in ChatGPT). Per its own text this relay is **not** CLI owner proof.
Nothing in this section records owner approval.

| | |
|---|---|
| Base (live) | `cc38238`, live `/root/ai-dev-runtime` (branch `ai-runtime/220-windows-bridge`), 0 tracked changes |
| Audited | `cc38238..0d98fd8` (dfe9174, 1c4a102, 5aad66a, 0d98fd8) |
| Review fixes | `94194e6` |
| Frozen code SHA | `94194e6`, fast-forward from cc38238 verified (`merge-base --is-ancestor`) |
| Reviewer | one fresh-context Claude Opus subagent, read-only, no prior context, separate from the author |
| Verdict | round 1: **GO** (0 blockers, 3 minor, 2 nits). Round 2 (on 94194e6): **GO** |
| Tests | focused suites 127/127. Full suite **4017 passed / 0 failed / 4 skipped** (cc38238 baseline: 3985) |

Findings and how each was resolved:

- MINOR, composer: assistant count/id evidence could be a false positive after a
  post-navigation re-mount. Fixed: that evidence now counts only when `gen_before is False`.
  - Residual: a re-mount while nothing was generating can still produce a false
    positive. This is bounded, because a verified delivery also registers an SLO watch.
- MINOR, native supervisor precheck: its inputs could differ from `_deliver`'s resolved
  pane. Fixed: it refuses nothing unless the live pane gives both session and cwd.
- MINOR, wake_companion: about 27 a day of unverified wakes now get watches and can
  escalate (at most once per event, never resent). Not changed. Monitor escalation
  volume for 24 h after deploy.
- NIT, closed_loop_wake: a failed import of `DIAGNOSTIC_GATE_KINDS` used to drop the
  whole gate check. Fixed: it falls back to the old rule, where every open gate counts
  (sentinel checked against NULL kinds).
- NIT, composer: a failed attempt can now take up to 10 s longer (USER_TURN_SECS=20).
  Accepted.
- Each fix has a regression test, and each test fails with its fix reverted.

Reviewer's deploy-safety notes:
- `slo_scan`, `register_delivery`, `native_supervisor.scan` and `submit_phrase` run only
  in the companion. `ai-runtime.service` imports these modules read-only and none are on
  its skew list, so restarting only the companion is consistent.
- The migration is a single additive `ALTER TABLE ADD COLUMN delivery_verified DEFAULT 1`.
  No `SELECT *` or positional read of the table exists.
- Rolling back to cc38238 is safe: old code ignores the column and the `policy_refused`
  rows.

**Deploy NOT performed.** Blocker: no host-verified owner proof.
- The only standing owner instruction is to deploy or restart only with explicitly
  authenticated approval, and in this session only an owner turn typed directly at the
  CLI counts. The relay disclaims being that proof.
- No fast-forward, restart or live evidence was produced. The companion was observed
  already restarted by someone else at 06:44 CET (PID 393675) on cc38238.

Exact step once host owner proof is given:
1. `git -C /root/ai-dev-runtime merge --ff-only 94194e6` (after re-checking 0 tracked changes)
2. `systemctl restart owner-os-wake-companion.service`. Leave `ai-runtime.service` alone.
3. Verify live: an actionable stop event leads to a `wake_delivery` delivered, then ACK,
   then a new ChatGPT command reaching the agent. Watch escalation volume for 24 h.

Rollback: `git -C /root/ai-dev-runtime reset --keep cc38238`, then restart only the
companion.

## Deploy and live closed-loop evidence (2026-10-28)

**Approval.** An owner turn was typed directly in the supervisor CLI session at about
08:51Z, with no automated header. No `deliveries` row matches its text, and there was no
delivery to the pane after 08:28Z. It approved a fast-forward to `94194e6` and a restart
of only `owner-os-wake-companion.service`, with rollback to cc38238.

**Deploy (08:52:02Z).**
- `git merge --ff-only 94194e6` on live `/root/ai-dev-runtime`, with 0 tracked changes
  before and after.
- Companion restarted: PID 393675 → 2106593, NRestarts 0. `ai-runtime.service` was left
  alone (PID 1084446 unchanged).
- The `wake_loop_watch` table was backed up first to
  `backups/stop-wake-deploy-2026-10-28/wake_loop_watch.sql`.
- The migration added `delivery_verified` live. The first heartbeat advanced at
  08:52:52Z, and there are 0 errors or tracebacks in the companion journal.

**Live chain** (all timestamps UTC, from control_plane.db / agent_control.db):

| step | evidence |
|---|---|
| event | 74114 `agent_watch/work_stopped_incomplete` on `owner-os-supervisor-clean:0.0`, 08:49:16 |
| attempts | 08:54:10 `composer_did_not_clear_after_send` (not sent, not latched, retried; a pre-existing mode, 67 in the prior 24 h) |
| delivery | `wake_delivery` 44670, 09:01:09.927, delivered=1, `submitted_and_assistant_started_generating`, route owner-os, chat …de5162d4ac17 |
| ack | `wake_audit` acknowledged at 09:01:09.935 (8 ms after the verified delivery) |
| watch | `wake_loop_watch` 74114 delivery_verified=1, resolved `runtime_reports_agent_working`, no re-wake |
| GPT → agent | `deliveries` `wake74114-close-loop-proof-20261028`, 09:01:42, actor `api:bearer/mcp-token-3`, target `owner-os-supervisor-clean:0.0`, delivered/submitted/prompt_consumed=true |
| executed | the supervisor pane acted on that command in the next turn: it ran these checks and wrote this section |

A second event, 74112 (vtb prompt-needs-response), was delivered at 08:59:54.753 and
acknowledged at .765, and has a watch row with verified=1. Its VTB owner-gated content
was not acted on.

**Regression checks since the deploy:**
- Duplicate wake: exactly 1 delivered row and 1 `wake_submitted` latch per event (74112,
  74114). Failed attempts did not latch.
- Duplicate tab: 1 CDP page (the owner-os chat).
- False success: both delivered rows rest on the assistant starting to generate.
  - No delivery came only from the new evidence fallback.
- Stale ack: every ack since the deploy follows its verified delivery within 12 ms, with
  one exception, 73984.
  - 73984 (a gaika-pl stop latched at 06:02 on the old code) was retired by
    `record_abandoned_wakes` after 10 823 s as `submitted_delivery_unproven`. That is the
    existing, expected path.
- Watchdog: 0 `closed_loop_wake` re-wake or escalation events.
- Native pre-check: 3 idle `policy_refused` rows (one per target), then backed off.
  - No refused continuation attempts.
  - 7 event-path skips.

### Post-deploy verification at 09:05Z (an automated instruction was received for event 74126)

- **At-most-once (all live data):** there are 614 `closed_loop_wake` `wakeloop:*` events,
  with 614 distinct dedup keys. Across 8 254 `wake_loop_watch` rows, none has rewoken>1 or
  escalated>1. Since the deploy there are 0 re-wake and 0 escalation events.
  - Every post-deploy watch is still inside its 900 s SLO; 74112 falls due at 09:14:54Z.
    The escalation rate after the deploy cannot be measured yet.
  - In the 7 days before the deploy, the only `closed_loop_wake` events were 175
    `owner_intervention`.
- **Idle backoff:** in the hour before the deploy there were 31 idle rows, all refused
  `continue` (bitwhite 11, gaika-pl 9, serverwarden 11).
  - Since the deploy there are 3 rows, one `policy_refused` per target at 08:54/08:56Z,
    and nothing after that: the backoff holds.
  - The event path made 0 continue attempts against 3 refused in the prior hour. All 11
    event rows are skips (external wait, not a turn boundary, self-reference,
    value-bearing send blocked).
- **Self-project exclusion:** native_supervisor skipped `owner-os-supervisor-clean:0.0`
  as `supervisor_self_reference` (events 74117, 74123), so it never sent to the pane.
  ChatGPT handled the self-project by wake instead:
  - 74114 was delivered at 09:01:09Z and answered with a GPT command at 09:01:42Z.
  - 74126 (`agent_waiting_input`, 09:04:14Z) was delivered at 09:04:48Z, verified, and
    has a watch open; this turn is its handling.
- **Defect search:** none found in the changed code. Pre-existing and unchanged:
  `composer_did_not_clear_after_send` / `composer_ambiguous_or_absent` send failures,
  and some native rows keyed by cwd rather than pane (e.g. `/root/ai-dev-runtime`).

### After the 09:15Z cutoff (an automated instruction was received)

- **Escalations after 09:15Z:** exactly one closed-loop event, `wake_loop_no_progress` 74133
  (dedup `wakeloop:74112:no_progress`, 09:17:25Z), re-waking ChatGPT about vtb event 74112
  as designed. There was no escalation and no other non-routine event in 09:15-09:17Z.
  The self-project watches 74114/74126/74129/74132 all resolved on progress.
- **Expected input already satisfied?** No. The vtb prompt asks owner-gated questions
  (the 10 000 RUB order, disabling demo products, release). No open decision gate or
  recorded owner decision covers them, so the re-wake, and at most one later
  `wake_loop_stalled`, is the correct path. No one acted on the vtb questions.
- **Composer clear/absent failures (pre-existing):**
  - 06:16-08:58Z the owner-os route alternated `composer_did_not_clear_after_send` and
    `composer_ambiguous_or_absent:0`, mostly with no navigation. Every attempt since
    08:59Z has succeeded (6/6).
  - After a `did_not_clear` (whose handler runs `innerHTML=''` on the ProseMirror
    composer), the next attempt on the same chat was `absent:0` in 26/65 cases, against
    32/149 after any other result.
  - That is a correlation. It is not proof that the wipe breaks the editor, so the wipe
    is NOT changed.
  - Concrete defect fixed in `d629f07` (local, not deployed): tab recovery and its retry
    outcome were never recorded. Each recovery now writes a journal line, so the next
    occurrence can be diagnosed. Full suite 4020 passed / 0 failed / 4 skipped.
- **Native rows keyed by cwd:** this is by design for decisions taken before the pane is
  resolved, and `_cap_exemption` reads them back by cwd. It would leak only if panes
  shared a cwd, and the live check shows 8 agent panes with 8 distinct cwds. Not a
  defect today; not changed.
- **To take effect,** `d629f07` needs the companion restarted on it. That is
  owner-gated and was not done.

### Event 74139 `wake_loop_stalled` on vtb:0.0, a defect fixed in `360992c` (an automated instruction was received)

- **Chain:**
  - 74112 `agent_prompt_needs_response`, payload `state=waiting_owner`,
    `class_reason=inventory_waiting_owner`, owner_action_required=1, 08:42Z.
  - It was delivered and acknowledged 08:59:54Z.
  - 74133 `wake_loop_no_progress` followed at 09:17Z, then 74139 `wake_loop_stalled`
    (critical) at 09:32Z. Once each, so dedup held.
- **Why:**
  - `_resolution_reason` had no rule for "the agent itself declares it waits on the
    owner".
  - Before dfe9174 the only open vtb gate, `classify_scope` (a diagnostic gate),
    resolved the watch as `pane_awaiting_owner` by accident. dfe9174 correctly stopped
    counting diagnostic gates, which exposed the gap.
  - The owner had already been told by 74112 and is re-reminded hourly by agent_watch
    (`REMINDER_SECS=3600`). The re-wake asked ChatGPT for decisions it may not make, and
    the stall alarm duplicated the owner's own queue.
- **Fix (fail-closed, positive evidence only):** the watch resolves silently as
  `pane_declared_waiting_owner` only when all of these hold:
  - the original prompt event has owner_action_required=1 and `class_reason=inventory_waiting_owner`;
  - agent_watch still classifies the pane `owner_prompt` with the SAME digest.
- **Still escalating:** decision menus (`decision_prompt_at_bottom`), events without the
  recorded reason, a new digest (a new episode), and any read error keep escalating as
  before.
- **Invariant:** the watchdog never re-wakes or escalates an unchanged, already-notified
  owner-wait episode. A real stall or a new question still escalates once.
- **Tests:** 7 new; removing the check fails 1. A read-only dry run on the live DB:
  `_declared_owner_wait(74112)=True`. Full suite 4027 passed / 0 failed / 4 skipped.
- **Not touched:** the VTB decisions, routes, and messages to vtb. vtb_old still resolves
  via the `unmapped_route` fallback to owner-os; binding a route needs an owner-supplied
  conversation URL.
- **Deploy:** local only. Live stays at 94194e6. Taking `360992c` live (together with
  d629f07) needs owner approval for a fast-forward plus a companion-only restart.

## Owner task "full autonomous loop" — deploy cd7ee22 and live proof (2026-10-28, 09:54-10:16Z)

**Approval.** An owner message was typed directly in the supervisor session at about
09:54Z. No `deliveries` row matches it; the last API delivery was at 09:35:17Z. It
authorised, after an audit and a green full suite, a fast-forward up to 323d900 and a
restart of only the companion, with rollback to 94194e6.

**Audit.** A fresh-context Opus reviewer (read-only) audited 94194e6..323d900.
- Verdict: GO, with one MAJOR finding. `360992c` trusts `class_reason=inventory_waiting_owner`,
  which `_STATE_WAIT_OWNER_RE` also matches on FINISHED panes ("do you want me to…").
  Examples: payorch 72722, gaika-ru 73627, bitwhite 72620. On a looped GAIKA agent it would
  disable the 15-min re-wake and delay the loop by up to 1 h.
- Because the owner's goal is the autonomous loop, **360992c was NOT deployed** and is
  reverted on the branch (3660c31).
- Deployed instead: **cd7ee22**. It is an ancestor of 323d900, so a strict subset of the
  approval. It adds the composer recovery logging `d629f07` plus report changes.
- Full suite at 323d900: green. At cd7ee22 (= d629f07): 4020/0/4.

**Deploy (10:01Z).**
- Live 94194e6 → cd7ee22 with `merge --ff-only`; 0 tracked changes before and after.
- 37 untracked files hashed before the fast-forward
  (`backups/stop-wake-deploy-2026-10-28/untracked_sha256_before_cd7ee22.txt`); all
  unchanged afterwards.
- Companion restarted (PID 2106593 → 2761085, NRestarts 0). `ai-runtime.service` untouched
  (PID 1084446).
- Rollback: `git reset --keep 94194e6` plus a companion restart.

**Root cause of the owner-os wake outages (06:16-08:58Z, 09:45-10:03Z), seen live:**
- A structural probe at 09:56Z showed ChatGPT mid-generation (Stop visible). Wakes then
  ended `did_not_clear`, and the following `absent:0` replaced the tab.
- The fresh tab rendered ChatGPT's own error, "Could not load this ChatGPT conversation /
  Try again": no turns and no composer. Further attempts failed as `absent:0` until the
  conversation loaded again.
- With d629f07 live, the first recovery logged at 10:03:17Z:
  `composer_ambiguous_or_absent:0 -> tab_replaced retry=submitted_and_assistant_started_generating`.
  74151 was delivered.

**Live chains (UTC):**

| chain | event (trigger) | wake delivered + ACK | ChatGPT next command | executed |
|---|---|---|---|---|
| self-project | 74114 work_stopped_incomplete 08:49 | 09:01:09 / 09:01:09 | 09:01:42 `wake74114-close-loop-proof-20261028` | yes (this pane ran it) |
| self-project | 74126 agent_waiting_input 09:04:14 | 09:04:48 / 09:04:48 | 09:05:21 `wake-74126-safe-postdeploy-verify-20261028` | yes |
| self-project | 74132 work_stopped_incomplete 09:12:38 | 09:13:51 / 09:13:51 | 09:14:29 `self-project-safe-continue-74132-escalations-20261028` | yes |
| GAIKA via self | 74141 work_stopped_incomplete 09:34:11 | 09:35:36 / 09:35:36 | 09:36:58 `gaika-pl-20261028-1137-verify-7ee6947e-v1` → gaika-country-pl | yes: pl turns 09:37:24, 09:38:25, then stop 74150 09:42:59 |
| GAIKA | 74150 work_stopped_incomplete 09:42:59 | 09:44:13 / 09:44:13 | none: ChatGPT checked and judged further PL work owner-gated | — |
| GAIKA re-wake | 74164 wake_loop_no_progress 10:03:19 | 10:06:04 / 10:06:04 | none, same reason (ChatGPT reply in the pl chat, 1m14s of work) | — |

All triggers are agent events (Stop hook → agent_watch quiescence / waiting_transitions)
or the watchdog. None is an hourly timer or an owner message. Every ChatGPT command
landed 32-81 s after its wake, sent as `api:bearer/mcp-token-3`, with
delivered/submitted/prompt_consumed=true.

**Where the loop stops today is an owner gate, not a defect.** ChatGPT's own verdict on
74164 (pl chat): gaika-country-pl is idle and healthy, 7ee6947e has 2405 tests passed,
and "further PL development requires a test release and a decision on the Frisco data".

**Further local fix (not deployed): `358281d`.** `detect_owner_intervention` counted
ChatGPT's own API sends as the owner acting. Example: 74142 for gaika-pl, where the task
was actually sent at 09:34:48Z by `api:bearer/mcp-token-3`. 12 of 175 such rows in 7
days. It now checks for a delivered `api:` send first, failing to the old attribution on
any error. 4 new tests; full suite 4024/0/4. It is outside the approved 323d900 scope and
needs its own audit and owner approval to deploy.

**Isolated end-to-end test.** I used the self-project pane `owner-os-supervisor-clean:0.0`.
It is not a product project, and it is the only allowlisted automation target. A new
dedicated test agent would need the creation gate and a new delegation grant, both owner
gates, so none was created.

### Deploy of 358281d (10:35Z, owner approval typed in the CLI)

- **Audit:** a fresh-context Opus reviewer (read-only) gave GO.
  - It verified JSON `delivered` reads back as 1, that the target and epoch formats match,
    and that the only caller is agent_watch in the companion.
  - Live dry run: 5 runtime-equivalent API-driven resumes in 24 h.
- **Minor findings, accepted:**
  - The window is "any `api:` send since the notification", so a later owner action
    could be under-counted (the safe direction for this metric).
  - The read has a 5 s ro timeout inside agent_watch's write transaction (about 1 ms in
    practice).
- **Deploy:**
  - Live cd7ee22 → 358281d with `merge --ff-only`; 0 tracked changes; 37 untracked files
    verified unchanged by hash.
  - Companion restarted (PID 3072856, NRestarts 0). `ai-runtime.service` untouched
    (PID 1084446).
  - Heartbeat advancing, 0 errors.
- **Rollback:** `git reset --keep cd7ee22` plus a companion restart.

### Live chain on 358281d (10:42Z, an automated instruction was received)

- **Chain:**
  - Event 74204 `agent_waiting_input` on owner-os-supervisor-clean:0.0, 10:37:03Z.
  - Wake delivered and ACKed 10:41:29Z, through a logged recovery:
    `assistant_generating_wedged -> tab_replaced retry=submitted_and_assistant_started_generating`.
  - ChatGPT command `event-74204-live-chain-358281d` at 10:42:10Z, sent as
    `api:bearer/mcp-token-3`, with delivered=1 and prompt_consumed=1.
  - The pane resumed: agent_watch shows `working`, and this turn executed the command.
  - `_automation_sent_since(74204)` = True, so this resume will not be logged as an
    owner intervention. There are 0 owner_intervention rows since the restart.
- **Companion health since the restart:**
  - 6 of 6 delivery attempts succeeded (74183, 74178, 74197, 74198, 74199, 74204).
  - Heartbeat 28 s old, NRestarts 0, 0 errors.
  - Watchdog: 74198 deregistered as `pane_escalated_by_stall_doctor`.
  - 74200 (bitwhite) was held by the per-agent cooldown and claimed at 10:42:22Z; this
    is normal coalescing.

## Per-conversation tabs and the false-wedge fix: local, not deployed (automated instructions received)

**Evidence (live, read-only, since the 94194e6 deploy):**
- 7 ChatGPT commands arrived 32-81 s after a delivered wake, so they are wake-driven.
  3 arrived 5-21 min later (10:25 and 10:27 to gaika-pl, 10:48 to this pane), so they
  are not wake-driven: an owner/ChatGPT session.
- Before the composer fix (the night of 10-27/28) almost every wake failed, so commands
  then came from direct sessions. Minutes of the hour are uniform, so there is no hourly
  timer.
- **Every failure stretch on the owner-os chat began when the single shared tab was
  navigated back to it while ChatGPT was still answering** (09:45, 10:20, 10:44Z).
  - A fresh load mid-answer renders "Could not load this ChatGPT conversation", which
    reads as `composer_ambiguous_or_absent:0` and leads to another tab replacement.
- 10:47:47Z: `assistant_generating_wedged -> tab_replaced` on a live turn. 22 s later,
  ChatGPT sent its 10:48:09Z command from that chat.
  - In the 2026-10 UI a thinking or tool-calling turn has no streaming class and no
    message ids, so the 4 s sample calls it wedged.

**Fix (tools/cdp_composer.py), commits e505602, dd43316, 355aad3, 3e5f79e:**
- `find_chatgpt_page(conversation_url)` reclaims a root page first. Otherwise the caller
  opens a tab of its own while there are fewer than `CDP_CONVERSATION_TABS` (6, capped at
  ≤ CDP_MAX_PAGES-2).
  - At the cap, only a tab provably NOT generating is reused.
  - With every tab busy, the attempt returns `all_conversation_tabs_busy` before
    anything is typed or latched.
- A wedge now needs both:
  - the 4 s sample to agree;
  - the stop control continuously up with the page length unchanged for
    `CDP_WEDGE_MIN_SECS` (900), or for 3x that since first sighting regardless of length.
- The observation is per conversation and per tab, and is forgotten after a 1800 s
  sighting gap (more than the 900 s cooldown plus the 300 s backoff).
- Unchanged: the phrase is typed only into the tab whose URL is the bound conversation,
  and the latch and dedup are untouched.

**Tests:**
- `tests/test_composer_tab_isolation.py`: 22 tests. Sabotage of isolation, the wedge
  gate, the busy refusal and the gap each fails tests.
- The legacy composer suite against the worktree module: 115/115.
- Full suite at 3e5f79e: **4046 passed / 0 failed / 4 skipped**.

**Independent critical audit** (fresh Opus, read-only, 4 rounds): final **GO** for
358281d..3e5f79e. Fixed along the way: stale-observation ageing, a missing ceiling, the
gap shorter than the retry cadence (a liveness MAJOR), and an unguarded env parse.
Open, non-blocking:
- a thinking turn longer than 15 min can still be reloaded;
- dead tabs at the cap give `all_conversation_tabs_busy` until the 3 h expiry;
- probing at the cap costs up to about 23 s per tab.

**Deploy (owner-gated, not done):**
- Fast-forward live 358281d → 3e5f79e and restart only owner-os-wake-companion.service.
  `ai-runtime.service` does not use the changed code.
- After deploy, ChatGPT tabs grow from 1 to one per active route (up to 6). Watch
  `browser_tab_health` headroom and the `all_conversation_tabs_busy` lines.
- Rollback: `git reset --keep 358281d` plus a companion restart.

### Deploy of 3e5f79e (12:05Z, owner turn typed in the CLI)

- **Approval:** the owner turn "установи 3e5f79e" was submitted at the terminal.
  - The last API delivery to the pane was 12:02:14Z, so this was not an API delivery.
  - No stall-doctor or governor submission was made.
- **Deploy:** live 358281d → 3e5f79e with `merge --ff-only`.
  - 0 tracked changes; 37 untracked files verified unchanged by hash.
  - Companion restarted (PID 3909219, NRestarts 0). `ai-runtime.service` untouched
    (PID 1084446).
- **Tabs:** 1 → 2, owner-os (…d4ac17) and gaika (…8355c8). Neither was navigated away.
- **Owner-os chat busy at 12:09-12:15Z:** the result was `assistant_still_generating`
  three times, with no tab replacement. Before the fix this path was false `wedged`,
  then a replacement, then "Could not load".
- **GAIKA chain:**
  - 74272 was delivered and ACKed at 12:06:54Z.
  - ChatGPT sent `gaika-pl-20261028T1210Z-offline-gap-v1` at 12:09:02Z.
  - gaika-pl worked; its next stop, 74287, was delivered and ACKed at 12:12:29Z, and the
    watch resolved as `pane_alive_and_working`.
- **Rollback:** `git reset --keep 358281d` plus a companion restart.

### Post-install baseline on 3e5f79e, 12:20Z (an automated instruction was received)

- **74278 (owner-os):**
  - 12:08 `did_not_clear` (fresh owner-os tab), then 12:13 `assistant_still_generating`.
  - 12:19:19Z delivered and ACKed once ChatGPT's turn ended.
  - ChatGPT command `selfwake-74278-postinstall-verify-20261028` followed at 12:20:28Z.
- **Non-self chain (GAIKA):**
  - 74272 was delivered and ACKed at 12:06:54Z.
  - Command `gaika-pl-20261028T1210Z-offline-gap-v1` at 12:09:02Z.
  - gaika-pl worked; stops 74287 and 74289 were delivered and ACKed at 12:12:29Z and
    12:17:59Z.
- **Since the restart:**
  - 9 attempts: 5 delivered, 3 `assistant_still_generating` (back-pressure, no tab
    replaced), 1 `did_not_clear`.
  - 1 recovery: 74272 `absent:0` → tab replaced → delivered.
  - 0 `all_conversation_tabs_busy`, 0 errors.
- **Tabs:** 2 and stable (owner-os, gaika).
- **Companion:** PID 3909219, NRestarts 0, heartbeat 4 s.
- **Still pending:** 74279 (vtb re-wake) and 74293 wait on ChatGPT's current owner-os
  turn. Only time-based observation remains.

## 2b49d15 + 8e9db95: code ready, NOT installed (automated instructions were received)

Both commits were made on automated instructions. Neither is owner approval, and
neither is installed. A visible unsent draft in the owner pane is not authority either.

### 2b49d15: one escalation per unchanged owner prompt (event 74523)

- **Cause:**
  - `vtb:0.0` has had one four-question owner prompt on screen, with digest
    `a3f420d6b8b74117`, unchanged for 9 days.
  - agent_watch re-notifies it hourly (`REMINDER_SECS`). Every reminder opened a new
    watch, which re-woke at +15 min and escalated at +30 min.
  - Result: 3 ChatGPT turns per hour in the owner-os chat, and 8 escalations in a week.
- **Why the existing gate check missed it:** vtb's only open gate is `classify_scope`,
  which is diagnostic by design. The rejected 360992c rule (`inventory_waiting_owner`)
  was not reused.
- **Fix:** a watch resolves silently as `prompt_already_escalated_unchanged` only if
  all of the following hold:
  - the event is a prompt wake with a digest;
  - agent_watch still shows `owner_prompt`/`blocker` with that same digest;
  - an earlier prompt watch for the same target and digest has already escalated after
    `digest_since`.
- **What it does not change:**
  - The first watch of every episode still escalates.
  - Stop, crash and quiescent watches are untouched.
  - Any change on the pane starts a new, fully alerting episode.
  - Read errors fail closed.
  - The reminder itself is still delivered.
- **Replay on a copy of the live DB:** the rule matches all 5 vtb watches of the day.
  Of the watches open at that time, only 74518 (vtb) matched.

### 8e9db95: a capped ChatGPT chat is never a plain delivery

- **Observation:**
  - The owner-os and payment-orchestrator chats intermittently show "You've reached the
    maximum length for this conversation … Start new chat" in an `aside[role=alert]`.
  - The composer stays editable.
  - Both chats kept answering: 33 and 27 delivered wakes in 6 h.
  - **Consequence: the notice is observed, not used as a refusal.** Refusing on it would
    silence working routes.
- **Fix:**
  - `conversation_is_full()` reads only `[role=alert]` and returns a boolean.
  - Success keeps `ok=True`, but its reason gains `+conversation_full_notice` and the
    result carries `conversation_full=True`.
  - Post-send failures (`user_turn_*`, `assistant_started_*`) become
    `conversation_full:<reason>`.
  - Pre-send reasons are never rewritten, because `submit_phrase` recovery and
    `chat_registry` match them exactly.
  - An unreadable or raising probe changes nothing.
- **Control:** 4 of the 9 new tests fail on 3e5f79e.

### Pre-install verification

- **Branch** `ai-runtime/stop-wake-closed-loop`, HEAD 8e9db95. On top of the live 3e5f79e:
  - 2 code commits: 2b49d15, 8e9db95;
  - 3 report-only commits;
  - no tracked changes left in the worktree.
- **Files changed vs 3e5f79e:**
  - code: `core/closed_loop_wake.py`, `tools/cdp_composer.py`;
  - tests: `tests/test_closed_loop_wake.py`, `tests/test_cdp_composer.py` (a structural
    probe answer in the fake page only), new `tests/test_composer_conversation_full.py`;
  - this report.
- **Live checkout:** `/root/ai-dev-runtime` is on branch `ai-runtime/220-windows-bridge`
  at 3e5f79e, with no tracked changes. 3e5f79e is an ancestor of 8e9db95, so this is a
  fast-forward.
- **Tests:**
  - Earlier full suite on 3e5f79e: 4044 passed, 0 failed.
  - Full suite on 8e9db95: 4064 passed, 4 skipped, 0 failed (11:47).
  - Legacy `test_cdp_composer` against the worktree module (temp copy), plus tab
    isolation, conversation-full and closed-loop tests: 254 passed.
  - Related subset (wake, watch, stall, owner, loop, supervisor, cdp, composer,
    companion): 1734 passed, 0 failed.

### What the single install would change

- **Not loaded yet:** both changed modules are imported only by the companion process
  (PID 3909219, started 13:04:24 CET, NRestarts 0). It still runs 3e5f79e code.
- **Skew detection:** both files are on `_WORKER_WATCHED_FILES["wake_companion"]`. A
  fast-forward without a restart would raise deploy skew, so the two steps go together.
- **ai-runtime.service:** none of its orchestrator files change, so it needs no restart.
  `diagnostics.py` imports `cdp_composer`/`closed_loop_wake` lazily and read-only.
- **Install** (only on an owner turn typed in the CLI):
  1. `git -C /root/ai-dev-runtime merge --ff-only 8e9db95`
  2. `systemctl restart owner-os-wake-companion.service`
  - Back up the untracked-file manifest first, the same way as for earlier installs.
  - No migration, no config, route or secret change.
- **Rollback:**
  1. `git -C /root/ai-dev-runtime reset --keep 3e5f79e`
  2. `systemctl restart owner-os-wake-companion.service`
- **Proof required after activation:**
  - The next hourly vtb reminder must produce no `wake_loop_no_progress`; the watch must
    resolve as `prompt_already_escalated_unchanged`.
  - A delivery to a capped chat must show `+conversation_full_notice` in
    `wake_delivery`.
  - Non-vtb watches must still re-wake and escalate.
  - **Not complete until this is observed live.**

### classify_scope backlog: owner decision, not engineering work

- 45 open gates. Closing one needs either:
  - a verified owner decision via `/control-plane/owner/decision`, or
  - a scope entry in the orchestrator config, where `mode: auto` grants autonomy.
- Both are owner authority. No gate was touched.
- **38 belong to agents recorded `dead`.** One owner decision ("dead, close") would
  retire them.
- **7 are live, `observe_only`:**
  - `bitwhite-backend-v3`, `/opt/bitwhite`
  - `gaika-country-pl`, `/opt/gaika-worktrees/pl`
  - `gaika-country-ru`, `/opt/gaika-worktrees/ru`
  - `serverwarden-livefire`, `/opt/hostsecure`
  - `mess-telegram-connector`, `/opt/mess`
  - `vtb`, `/opt/vtb`
  - `owner-os-supervisor-clean`, `/root/ai-dev-runtime`
  - Each needs a project mapping and a mode decision from the owner.

### Self-project chain, event 74581 (live 3e5f79e; an automated instruction was received)

- **Timeline:**
  - 18:15:19Z: agent_watch reported `work_stopped_incomplete` for
    `owner-os-supervisor-clean:0.0` (quiescent, `at_rest_unchanged_for_348s`).
  - 18:16:41Z: the wake was delivered to the owner-os chat
    (`submitted_and_assistant_started_generating`).
  - 18:17:47Z: ChatGPT sent `agent_send` via `api:bearer/mcp-token-3`, 66 s after the
    delivery.
  - The watch resolved as `runtime_reports_agent_working`.
  - No owner action was involved.
- **Native supervisor:** it is correctly excluded. `SELF_PROJECT` is `ai-dev-runtime`
  and is in `AUTO_REGISTER_DENY_PROJECTS`. `native_supervised_target` has no row for
  this pane or cwd. ChatGPT continuation is the only path.
- **Stall-doctor `escalate … LOST_CONTINUATION` lines for this pane:** these are the
  unsent human draft in the pane's input line. That draft is never submitted by
  automation. Notifications are deduplicated to 1 per pane per day (owner decision of
  2026-09-11), and the watch closes as `pane_escalated_by_stall_doctor`. This is by
  design and not a defect.
