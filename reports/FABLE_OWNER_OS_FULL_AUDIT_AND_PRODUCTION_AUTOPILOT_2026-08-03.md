# FABLE — OWNER OS FULL AUDIT + PRODUCTION AUTOPILOT (2026-08-03)

Fable 5, continuation of the interrupted Fable A session (`a202151ee0e145cc7`).
Baseline checkpoint: `/root/owner-os-backups/pre-fable-20260803T191405Z`
(HEAD `60f9967`, baseline suite **1045 passed**). Branch `owner-os/control-plane-v2`,
local commits only — nothing pushed, nothing published.

All actuation in this session went through the canonical lease-gated Actuator and was
confined to `cp-canary:0.0` (the owner-authorized disposable canary). payment:0.0 and
arbitrage2-opus:0.0 were evaluated READ-ONLY only. No agent was created, no payment /
trading / promotion / credential / publication / destructive action, no tool-permission
dialog was answered anywhere.

---

## 1. Interrupted work completed — actuator PENDING-INPUT GUARD (§3c)

Fable A was killed mid-edit on this guard. Status found: guard **code complete** in
`core/control_plane/actuator.py` (defer on different pending text; submit-not-repaste on
identical text), but only the defer branch was tested.

Completed with adversarial tests that FAIL on pre-fix HEAD (proven by stash-run —
3 failed on `60f9967`, all pass post-fix):

| Test (tests/test_owner_os_adversarial.py) | Pins |
|---|---|
| `test_actuator_refuses_to_paste_onto_pending_input` (Fable A) | different queued text → defer, nothing pasted |
| `test_actuator_submits_same_pending_text_instead_of_repasting` (new) | identical typed line → Enter, never a second paste |
| `test_pending_guard_defers_without_burning_the_idempotency_key` (new) | a deferral leaves the step deliverable exactly once later |
| `test_waiting_input_is_a_poke_candidate_actuator_guards_the_line` (new) | waiting_input pokeable; the actuator line-guard decides |

**Live exercise (cp-canary):** `cp_action` idkey
`cp-canary:0.0|72acbf69-…|dd660d9ee2d0e393` (kind `pending_submit_recovery`,
verified, fence 4, 21:12:38Z, event 90) and a live guard deferral receipt
(event 91 `action_deferred_pending_input`, 21:13:39Z).

## 2. NEW production bugs found live this session (all fixed + adversarially tested)

These were found by driving the real system, not by code reading:

**(a) Recall-GHOST text read as pending input.** Claude Code renders the recall ghost of
the last submitted command (and suggestions) in DIM (SGR 2) — in a plain capture it is
byte-identical to typed text. Live: cp-canary's ghost `continue with the next safe canary
note` made `pending_input_text` report pending input → every autopilot poke deferred
(`pending_input_present`) and rotation would have been blocked indefinitely
(`pending_input_line`). Fail-safe direction, but a permanent availability hole.
Fix: `agent_control.prompt_text_from_styled` + styled (`-e`) capture in
`pending_input_text` (plain-tail fallback only if the styled capture fails);
`_pane_pending_input` now shares the same extractor. Tests use the EXACT captured bytes:
`test_dim_recall_ghost_is_not_pending_input`, `test_real_typed_text_is_pending_input`,
`test_menu_selection_is_not_pending_input`. Live validation: pending now `''`, next tick
delivered the real poke.

**(b) `deliver_and_verify` retry race → duplicate paste / foreign-text destruction.**
Live receipt `retried=true` at 21:12:38Z showed it: when the first Enter is merely slow
and the line is consumed between the last verify poll and the retry, the old retry
blindly `C-u` + re-pasted — duplicate copy of the step; and if DIFFERENT text had
appeared in that window, `C-u` would have destroyed never-classified input and pasted
over it. Fix: the retry re-checks the line immediately before acting — same text →
robust resubmit; empty → treat as landed and re-verify; foreign text → hands off,
surfaces the failure. Tests (scripted clock, fail on pre-fix by stash-run):
`test_slow_enter_consumed_between_polls_is_not_repasted`,
`test_foreign_text_at_retry_time_is_never_cleared_or_overwritten`.

**(c) Transport idempotency key ate the second `/clear` EVER.** Live: rotation #1
verified; rotation #2 (new conversation) failed — `cp_action …|2c8e85d331d1b0a9`
`verify_failed` + spurious owner gate — because `deliver_and_verify` used the constant
key `cw:<hash(text)>` and `agent_control._deliver`'s durable dedupe silently dropped the
identical `/clear`. Fix: transport key unique per attempt (logical dedupe belongs to the
layered ledgers `cw_step` / `cp_action`, which are conversation-scoped). Non-vacuous
test with a transport-faithful fake (`KeyedRotCtrl` mirrors `_deliver`'s seen-key drop):
`test_second_rotation_clear_is_not_dropped_by_transport_dedupe` — the old `RotCtrl`
ignored keys entirely, which is WHY the suite was blind to this (vacuity instance found
and fixed). Live retry after the fix: same idkey flipped `blocked → verified` and event
103 `blocker_resolved` was emitted (alarm + all-clear correlation live).

**(d) Central checkpoint unreadable inside the agent sandbox.** Live rotation #1: the
canary REFUSED to read `/root/ai-dev-runtime/checkpoints/…` (its CLAUDE.md scope rule),
resumed from in-scope task.md — correct fallback, checkpoint content unused. Fix:
rotation additionally writes an atomic, verified PROJECT-ROOT copy
(`<root>/CONTEXT_CHECKPOINT.md`) and the resume message references it AND carries the
exact NEXT COMMAND inline, so an unreadable file can never lose the handoff; the full
resume text is still re-classified `autonomous_safe` at actuate time. Live rotation #2:
"Checkpoint read. NEXT COMMAND executed." — note #6, zero duplicate work.

## 3. PASS/FAIL per audited dimension

| Dimension | Result | Evidence (tests + live receipts) |
|---|---|---|
| Architecture (single actuation path, deny-by-default, layered dedupe) | **PASS** | every pane command → lease+fence → policy recompute (`test_actuator_recomputes_policy_class_forbidden_text_blocked`) → idempotency → false-idle → pending-line guard → five-proof verified delivery. Dedupe now correctly stratified after fix (c). |
| State classification (live status region, stale scrollback) | **PASS** | `live_status_region` + real-pane regressions (`test_stale_shell_marker_in_scrollback_is_not_working`, `test_real_mess_pane_with_running_subagent_is_working`, minute-form spinner, compacting). Live: pre-fix service had cp-canary false-"working" 40+ min on a stale shell line; new code classified all 5 registry agents correctly this session. |
| Unfinished-task / background-subagent / false-idle detection | **PASS** | task-footer parsing; `waiting for N background agents` = working (LIVE: owneros-direct-fix + mess-qa-automation both `skip_progressing`, `background_subagent=True` — pre-fix both were false-idle poke candidates mid-audit); actuator false-idle guard (`test_false_idle_working_target_suppressed_after_restart`). |
| waiting_input vs waiting_owner semantics | **PASS** | waiting_owner NEVER poked (`test_waiting_owner_is_never_poked` — tool-dialog safety); waiting_input pokeable, the actuator pending-line guard decides submit vs defer; ghost fix (2a) makes the distinction truthful on live panes. |
| Safe next-step synthesis | **PASS** (static) | registry `next_step` per project; hard classifier pre-gate + actuator recompute; CI invariant `test_every_registry_next_step_is_autonomous_safe`; 6× `test_unsafe_next_step_is_not_poked`. Dynamic synthesis from the live task list remains a documented non-goal for this phase. |
| Leases / fencing / dedupe | **PASS** | LIVE exclusivity twice: autopilot delivery refused `stale_or_no_lease` while the audit lease was current (21:13Z); rotation refused `refused_clear_stale_or_no_lease` (ledger row 1). Fence: `test_fence_token_rejects_stale_actuation_after_restart`, `test_restart_midaction_stale_fence_rejected_no_duplicate`. Dedupe LIVE: 2nd tick → `already_verified`, poked=0. |
| Restart persistence | **PASS** | durable `cp_action`/`autopilot_run`/`cw_step`/`context_rotation`/`context_budget_state`; `test_restart_persistence_and_dedupe_no_reissue`; service redeployed this session — loops alive, `restart_safe=True`, `consistent=True` (§6). |
| Stuck-shell / dead-agent watchdogs | **PASS** | `watchdog_dead` (no duplicate created), `watchdog_stuck_shell` now REACHABLE in production (`test_stuck_shell_watchdog_reachable_without_explicit_conv_age_fn` — pre-fix `conv_age_secs` was always None live), `watchdog_false_completion` (completed claim + open tasks not accepted). |
| Duplicate prevention | **PASS** | no agent created anywhere this session; one rotation per (target, conversation) durable; poke idempotency live; fixes (b)+(c) close the two real duplicate/duplicate-adjacent delivery paths found. |
| Registry / end-state | **PASS** | 5 projects with end_state + safe next_step (`test_registry_has_five_critical_projects`); `end_state_met` on a zero-unfinished footer (`test_end_state_met_is_reported_when_footer_all_done`). |
| Delivery (verified, honest) | **PASS** | five-proof verify; retry hardened (2b); LIVE: poke delivery all five proofs true; notification honesty unchanged (receipts only on real 2xx — prior acceptance). |
| Same-chat-independent autonomy | **PASS** (server-side) | all loops server-side (supervisor, orchestrator, control-plane, continuation watchdog, autopilot [dormant, owner gate], context budget); no dependency on ChatGPT reachability. Same-chat wake itself remains at the documented EXTERNAL platform gates (unchanged, honest RED). |
| Context budget / checkpoint / rotation | **PASS** | §4 below — deterministic + TWO full live rotations with receipts. |
| Auto-poke (real kick → working) | **PASS** | §5 below — live receipts on cp-canary; honest working-evidence for owneros/mess; owner gates held for payment/arbitrage2. |

Test vacuity challenged: the acceptance tests that fed `tick()` fabricated `_tail` /
`claude_conversation` keys are superseded by real-contract adversarial tests (kept as
logic tests); the rotation controller fake that ignored idempotency keys (hid bug (c))
was replaced with a transport-faithful fake; every new regression was proven to FAIL on
pre-fix code by stash-running it against HEAD.

## 4. Context budget / checkpoint / rotation — production-complete

Deterministic (26 adversarial + 19 context-budget tests, incl.):
per-agent size+phase tracking durable in `context_budget_state`; atomic checkpoint
(tmp+fsync+rename) with all 10 sections (PROJECT/STATE/COMPLETED/HEAD/TESTS/JOBS/
PENDING/BLOCKERS/GATES/NEXT COMMAND); verify-readable gate (missing/empty HEAD or NEXT
COMMAND → rotation REFUSED, nothing sent — `test_failed_checkpoint_refuses_rotation`,
`test_checkpoint_write_failure_refuses_rotation`); never clears during
working/shell/subagent/tool-dialog/pending-input/active-marker
(`test_rotation_refused_at_unsafe_phase` ×5 + `test_background_subagent_blocks_rotation`);
one rotation per conversation durable; non-canary over-budget → owner-gated
`context_rotation_needed` event, ZERO actuation (`test_non_canary_rotation_is_owner_gated`).

**LIVE receipts on cp-canary:0.0** (`context_rotation` ledger, agent_control.db):

| id | result | conv mapping | receipt |
|---|---|---|---|
| 1 | `refused_clear_stale_or_no_lease` | — | lease exclusivity held live |
| 2 | **rotated** | `72acbf69-…` → `2af35da0-…` | checkpoint `checkpoints/cp-canary/checkpoint-20260803T212453Z.md` (verify ok, 10 sections); clear+resume verified; event 98 `context_rotated`; resumed, note #5, **no duplicate work** (found gap 2d) |
| 3 | `refused_clear_not_verified` | — | bug (c) surfaced honestly: blocked + owner gate, no silent success |
| 4 | **rotated** | `2af35da0-…` → `b2635b20-…` | checkpoint `…213013Z.md` + project copy `/root/cp-canary-v2/CONTEXT_CHECKPOINT.md`; clear+resume verified first try; event 103 `blocker_resolved` (prior gate auto-cleared by the verified action); canary READ the checkpoint, executed NEXT COMMAND, note #6, **no duplicate work, no duplicate agent** |

Rotation for every non-canary agent is proven deterministically only; live it produces
the owner-gated event (arbitrage2 ~21 MB and owneros ~26 MB conversations are over the
16 MB hard default, so the live loop will surface them — actuation stays impossible
outside `CONTROL_PLANE_CANARY_AGENTS`).

## 5. REAL auto-poke receipts

**cp-canary:0.0 (live, full production path `tick()` → evaluate → Actuator):**
21:23:49Z — decision `poke`, delivered, `cp_action`
`cp-canary:0.0|72acbf69-…|f09001150a4e74e8` (kind `autopilot_next_step`, **verified**,
fence 6, event 93) — ALL FIVE proofs true (submitted, pane_changed, prompt_consumed,
conversation_modified, state_transitioned). Kick → working transition confirmed by the
work product: canary appended dated note #4 (append-only, nothing external). Immediate
re-tick: `already_verified`, poked=0, zero sends — **dedupe live**. `autopilot_run`
rows 12–18 durable; unchanged consecutive decisions visibly deduped in the ledger.

**owneros-direct-fix:0.0 / mess-qa-automation:0.0 — live poke NOT possible at eval
time, with evidence (per mandate, not forced):** both are GENUINELY WORKING — panes show
`✻ Waiting for 1 background agent to finish` (owneros is running THIS very Fable audit
as its background agent; mess is awaiting its own Fable re-review). The autopilot
correctly recorded `skip_progressing` with `background_subagent=True` (ledger rows 12,
16 + repeats at 21:13/21:23Z) — the exact panes that pre-fix classified false-idle. The
system is armed to poke them the moment they are idle with unfinished work; their live
actuation enablement (CANARY allowlist) remains the owner's switch.

**payment:0.0 / arbitrage2-opus:0.0 (owner-gated, held):** both hit `poke_owner_gated`
live while idle with unfinished work (`autopilot_run` rows 15, 17) — the Actuator's
`not_canary` refusal, zero pane contact. Payment execution untouched; no trading action.

## 6. Full suite + redeploy

- Full suite: **1091 passed, 0 failed** (baseline 1045 + 46 new tests; one
  pre-existing environmental flake in `test_phase13` hardened — its global
  `pgrep "sleep 30"` collided with another agent's poll loop on this box).
- Redeployed `ai-runtime.service` at 21:40Z (required: the running process predated
  the working tree). Post-restart verification (21:42Z):
  * loops 5/5 alive, 0 stalled (continuation_watchdog, orchestrator,
    direct_agent_lifecycle, control_plane_engine, supervisor);
  * `commander autopilot disabled (owner gate)` — dormant as required;
  * `context budget started (interval 120s; soft 8000000 B, hard 16000000 B;
    rotation confined to CANARY_AGENTS)` — first live tick produced durable
    `context_budget_state` rows for ALL 5 registry agents;
  * `restart_safe=True`, consistency **green** (no violations);
  * `cp_action` row count unchanged across the restart — nothing re-issued
    (fence + idempotency held);
  * observability status honest-RED solely from 3 aging G4 owner-push
    dead-letters in the 1h window (unchanged, documented behavior).

## 7. Commits (local only)

- `11c4382` — feat(owner-os): full audit fixes — live-state classification,
  production autopilot contract, actuator guards, context budget/rotation
  (12 files, +1632/−39; includes the completed Fable A working tree and this
  session's four live-found fixes + all new tests).
- this report is committed on top of `11c4382` (docs commit, HEAD of
  `owner-os/control-plane-v2`). Nothing pushed.

## 8. Limitations

- `next_step` is static per project (deterministic, classifier-gated); dynamic synthesis
  from the live task list is out of scope this phase.
- Background-subagent detection is a tail regex plus state classification; a subagent
  invisible in the tail is covered only by the state/active-marker signals.
- Ghost detection depends on Claude Code's DIM styling of recall/suggestion text; a
  styling change would degrade to the previous (fail-safe, availability-only) behavior.
- The soft-threshold checkpoint writes only the central copy; the project-root copy is
  written at rotation time.
- Legacy `core/agent_context_budget.py` (orchestrator-integrated, detection-only live,
  rotation env-gated OFF) coexists with `core/context_budget.py` (actuator-routed,
  canary-confined). No double-rotation is possible (`AGENT_CONTEXT_ROTATE_ENABLED`
  unset + per-session opt-in absent); unification is future work.
- The live continuation watchdog auto-submits safe typed pending text on allowlisted
  idle panes (pre-existing design); observed live on arbitrage2 close after owner
  typing. No duplicate deliveries found in the transcript; flagged for owner awareness.

## 9. Owner gates NOT crossed (explicit)

1. Live actuation of payment:0.0 and arbitrage2-opus:0.0 — never; evaluate/read-only.
2. Live actuation enablement of owneros-direct-fix:0.0 / mess-qa-automation:0.0
   (adding to `CONTROL_PLANE_CANARY_AGENTS` in the systemd drop-in) — not persisted;
   they were not poked (genuinely working, evidence above).
3. `COMMANDER_AUTOPILOT_ENABLED` always-on loop — left dormant.
4. Live `/clear` rotation beyond cp-canary — never (deterministic proof + owner-gated
   events only).
5. git push / publication / any external side effect — none.
6. Telegram creds / same-chat relay (G4/G5) — untouched external gates.
