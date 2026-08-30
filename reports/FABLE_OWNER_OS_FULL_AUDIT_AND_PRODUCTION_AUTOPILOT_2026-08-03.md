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
| Architecture (single actuation path, deny-by-default, layered dedupe) | **PASS** | every pane command → lease+fence → policy recompute (`test_actuator_recomputes_policy_class_forbidden_text_blocked`) → idempotency → false-idle → pending-line guard → five-proof verified delivery. Dedupe now correctly stratified after fix (c). Classifier-breadth caveat: §3.1 — the wall is containment, not classification. |
| State classification (live status region, stale scrollback) | **PASS** | `live_status_region` + real-pane regressions (`test_stale_shell_marker_in_scrollback_is_not_working`, `test_real_mess_pane_with_running_subagent_is_working`, minute-form spinner, compacting). Live: pre-fix service had cp-canary false-"working" 40+ min on a stale shell line; new code classified all 5 registry agents correctly this session. |
| Unfinished-task / background-subagent / false-idle detection | **PASS** | task-footer parsing; `waiting for N background agents` = working (LIVE: owneros-direct-fix + mess-qa-automation both `skip_progressing`, `background_subagent=True` — pre-fix both were false-idle poke candidates mid-audit); actuator false-idle guard (`test_false_idle_working_target_suppressed_after_restart`). |
| waiting_input vs waiting_owner semantics | **PASS** | waiting_owner NEVER poked (`test_waiting_owner_is_never_poked` — tool-dialog safety); waiting_input pokeable, the actuator pending-line guard decides submit vs defer; ghost fix (2a) makes the distinction truthful on live panes. |
| Safe next-step synthesis | **PASS** (static; rests on containment, not classifier strength — §3.1) | registry `next_step` per project; CI invariant `test_every_registry_next_step_is_autonomous_safe`; 6× `test_unsafe_next_step_is_not_poked`; actuator recompute blocks denylisted text. The classifier pre-gate is NOT narrow (§3.1): this PASS rests on canary confinement + the fixed registry step texts. Dynamic synthesis from the live task list remains a documented non-goal for this phase. |
| Leases / fencing / dedupe | **PASS** | LIVE exclusivity twice: autopilot delivery refused `stale_or_no_lease` while the audit lease was current (21:13Z); rotation refused `refused_clear_stale_or_no_lease` (ledger row 1). Fence: `test_fence_token_rejects_stale_actuation_after_restart`, `test_restart_midaction_stale_fence_rejected_no_duplicate`. Dedupe LIVE: 2nd tick → `already_verified`, poked=0. |
| Restart persistence | **PASS** | durable `cp_action`/`autopilot_run`/`cw_step`/`context_rotation`/`context_budget_state`; `test_restart_persistence_and_dedupe_no_reissue`; service redeployed this session — loops alive, `restart_safe=True`, `consistent=True` (§6). |
| Stuck-shell / dead-agent watchdogs | **PASS** | `watchdog_dead` (no duplicate created), `watchdog_stuck_shell` now REACHABLE in production (`test_stuck_shell_watchdog_reachable_without_explicit_conv_age_fn` — pre-fix `conv_age_secs` was always None live), `watchdog_false_completion` (completed claim + open tasks not accepted). |
| Duplicate prevention | **PASS** | no agent created anywhere this session; one rotation per (target, conversation) durable; poke idempotency live; fixes (b)+(c) close the two real duplicate/duplicate-adjacent delivery paths found. |
| Registry / end-state | **PASS** | 5 projects with end_state + safe next_step (`test_registry_has_five_critical_projects`); `end_state_met` on a zero-unfinished footer (`test_end_state_met_is_reported_when_footer_all_done`). |
| Delivery (verified, honest) | **PASS** | five-proof verify; retry hardened (2b); LIVE: poke delivery all five proofs true; notification honesty unchanged (receipts only on real 2xx — prior acceptance). |
| Same-chat-independent autonomy | **PASS** (server-side) | all loops server-side (supervisor, orchestrator, control-plane, continuation watchdog, autopilot [dormant, owner gate], context budget); no dependency on ChatGPT reachability. Same-chat wake itself remains at the documented EXTERNAL platform gates (unchanged, honest RED). |
| Context budget / checkpoint / rotation | **PASS** | §4 below — deterministic + TWO full live rotations with receipts. |
| Auto-poke (real kick → working) | **PASS** | §5 below — live receipts on cp-canary; honest working-evidence for owneros/mess; owner gates held for payment/arbitrage2. |

### 3.1 CORRECTION (independent re-review): `autonomous_safe` is BROAD, not narrow

Earlier characterizations (including the code comment at
`core/control_plane/actuator.py:55` — "NARROW by design … arbitrary free-form text is
owner_approval") overstate the classifier, and no PASS above may be read as resting on
it. What `classify_action` actually does: (1) `_FORBIDDEN_RE` denylist match (English
tokens) → prohibited; (2) exact bare `/clear`|`/compact` → autonomous_safe; (3) ANY
text with a continue/proceed/resume/carry on/keep going/go on/next safe step prefix
that dodges the English denylist → **autonomous_safe**; (4) everything else →
owner_approval. Verified live probes: `"proceed to send 5 BTC to wallet X"` →
autonomous_safe ("send"/"BTC"/"wallet" are not denylisted);
`"resume and promote staging traffic to production"` → autonomous_safe ("promote" is
not in `_FORBIDDEN_RE`). The legacy continuation watchdog's `is_safe_continuation` is
weaker still: denylist-only, English-only, no prefix requirement (live consequence in
the §8 disclosure — Russian text cannot be classified at all). This is PRE-EXISTING,
not a regression of this session: `_SAFE_CONTINUATION_RE`, `is_safe_continuation` and
`_FORBIDDEN_RE` are byte-identical at baseline `60f9967` (this branch only ADDED the
exact-match bare-`/clear|/compact` branch). The REAL containment is: (a) canary
confinement — the Actuator refuses every target outside `CONTROL_PLANE_CANARY_AGENTS`
regardless of policy class; (b) fixed internal step texts — everything the autopilot /
rotation can actuate is a hard-coded registry `next_step` or a bare `/clear`, never
free-form text from an owner/agent/pane; (c) the CI registry invariant
(`test_every_registry_next_step_is_autonomous_safe`) pinning those fixed texts.
Classifier tightening (allowlist of exact step texts, or language-independent
semantics) remains open follow-up work.

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
the owner-gated event — actuation stays impossible outside
`CONTROL_PLANE_CANARY_AGENTS`. CORRECTION (re-review): the original prediction here —
that the ~21 MB arbitrage2 and ~26 MB owneros conversations are over the 16 MB hard
default "so the live loop will surface them" — was true at audit time but is MOOT.
Those jsonl files are the PREVIOUS conversations; both sessions restarted ~21:00Z, and
`measure()` reads only the LATEST conversation. The durable `context_budget_state`
rows (22:28Z tick) show arbitrage2-opus:0.0 at ~1.4 MB and owneros-direct-fix:0.0 at
~0.32 MB — both far under the 8 MB soft threshold. No owner-gated
`context_rotation_needed` event will fire from those old files.

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
- this report is committed on top of `11c4382` (docs commit `b362850`). Nothing pushed.
- post-report: `45cfb37` — fix(autopilot): delivered-poke ledger dedupe (re-review
  finding, §10; NOW LIVE since the 22:29:37Z restart — see the §10 deployment
  record, which supersedes the earlier "not deployed" statement), followed by
  this correction pass (docs only).

## 8. Limitations

- `next_step` is static per project (deterministic; the classifier gate is a broad
  prefix+denylist check — §3.1 — so safety rests on the fixed texts + canary
  confinement); dynamic synthesis from the live task list is out of scope this phase.
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
- DISCLOSURE (re-review correction — the original wording here understated this): the
  legacy continuation watchdog (pre-existing code, running before the 21:40Z restart)
  contacted arbitrage2-opus:0.0's pane **SEVEN times between 21:12:45Z and 21:38:15Z**
  (`cw_step` ledger, conversation `15f13266-…`, all verified delivered): **six
  auto-Enter submissions of owner-typed Russian instructions** staged in the input
  line — «удали старый scratchpad», «да, закоммить их в backend/tools/repro/»,
  «почини вакуумный probe для leg B», «почини toctou probe тоже», «запусти полный
  pytest и покажи финальный статус», «сохрани чекпоинт и заверши сессию» — plus **one
  proactive paste** of "Continue with the fault-matrix extension and replay harness"
  (21:13:20Z). Each contact was gated only by `is_safe_continuation`, an English-token
  denylist that cannot classify Russian at all — «удали» ("delete") passed untouched
  (§3.1). No duplicates occurred, and the texts were the owner's own typing; but the
  system auto-submitted delete-capable non-English text with no semantic check. The
  watchdog's pending-submit policy is flagged for an explicit owner decision.

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

## 10. Independent adversarial re-review (recorded 2026-08-04)

An independent re-review verified this report against the working tree, a true
pre-fix baseline, and the live databases. CONFIRMED: suite 1091 (1092 after its own
added test; the baseline worktree collects exactly 1045); all four §2 fixes real and
non-vacuous under targeted single-fix reverts; every live receipt matched against
control_plane.db / agent_control.db (`cp_action` dd660d… fence 4 / event 90;
f09001… fence 6 / event 93 with all five verify proofs; `context_rotation` rows 1–4;
`autopilot_run` rows 12/13/15/16/17); the safety walls (canary confinement,
policy-class recompute, rotation guards) held under its probes. It also produced the
corrections now folded into §3.1, §4 and §8.

**Baseline trap it found:** `tests/conftest.py:16` hardcodes
`sys.path.insert(0, "/root/ai-dev-runtime")`, so a naive stash/worktree baseline run
silently imports the FIXED code from the live tree and shows false green. With the
path repointed at the true baseline, 21 of the 26 adversarial tests fail on genuine
pre-fix code; the 5 that pass on both sides are deliberate anti-overcorrection
invariants, correctly not advertised as regression pins.

**One real defect found and fixed:** `_record_run` deduped on the decision string
alone, so the verified 21:23:49Z canary poke ("poke", delivered) following the 21:13Z
lease-refused attempt (also "poke") left NO `autopilot_run` row — that delivery was
invisible in the autopilot ledger (only `cp_action`/event carried it). Fixed in
commit `45cfb37` (a delivered poke is always recorded), regression test
`test_delivered_poke_is_never_deduped_out_of_the_ledger` proven to fail pre-fix;
suite 1092.

**DEPLOYMENT RECORD (corrected 2026-08-04, final re-review) — the paragraph that
previously stood here ("OWNER DECISION — deliberately NOT deployed … the LIVE service
still carries the ledger-dedupe observability bug") is SUPERSEDED. True sequence:**

1. The owner FIRST chose not to deploy `45cfb37`; at the time the correction pass
   (`2d2ed5d`, 00:32 local) recorded that, the statement was believed current — but it
   was already stale when committed (see 2).
2. A subsequent owner follow-up was applied by the interface as "Deploy now", and the
   coordinator restarted `ai-runtime.service` at **2026-08-04 00:29:37 CEST
   (22:29:37Z)** — verified from systemd (`ExecMainStartTimestamp=Tue 2026-08-04
   00:29:37 CEST`, MainPID 4063628; uvicorn startup logged 00:29:41 local). The
   working tree at start time was `45cfb37`, so the live process runs the ledger-fix
   code (`2d2ed5d`, committed 00:32:48 local, is docs-only and post-start — inert to
   the running process).
3. The owner then sent a STOP after the fact: do not deploy / restart / enable live
   autopilot; if already started, halt, report exact state, do NOT roll back without
   verification. Complied: **no rollback was performed and no further service changes
   were made** — deliberate hold at the owner's instruction. Verified at re-review
   (~22:40Z): same PID 4063628, same start timestamp, git history intact.
4. **Live-effectiveness of the `_record_run` delta — latent, not active:** the module
   is imported at startup, but `_record_run`'s only production caller is
   `commander_autopilot.tick`, whose only in-process caller is `run_loop`, and the
   startup log records `commander autopilot disabled (owner gate)` (00:29:41). The
   context-budget loop imports only `load_registry`/`_real_tail` from that module.
   So while the autopilot stays dormant, NO code path executes `_record_run`: the fix
   is loaded but runtime-inert. It becomes effective the moment the owner enables
   `COMMANDER_AUTOPILOT_ENABLED` (or a tick is invoked manually) — no further deploy
   needed for it.
5. Post-restart verification (independent, ~22:40Z): loops 5/5 alive 0 stalled;
   consistency green (event 114, no violations); `restart_safe=True`; actuation scope
   green (canary allowlist only); `cp_action` count 11 with newest row 21:52:29Z —
   **nothing re-issued across the 22:29:37Z restart**; `autopilot_run` max id 18
   (21:23:49Z) — no autopilot activity since; context budget ticking (22:35:45Z rows
   for all 5 registry agents, all far under the 8 MB soft threshold). Six
   `deliveries` rows after the restart are coordinator/owner-keyed manual API sends
   (human-descriptive idempotency keys, e.g. `owneros-cancel-wrong-deploy-selection-…`),
   with no corresponding `cp_action`/`cw_step`/`autopilot_run` rows — not from any
   audited autonomous path.

**Residual risks left open (accepted, documented):**
- Fix (c)'s per-attempt transport keys narrow but do not eliminate the crash window:
  a crash between transport delivery and the durable ledger write can no longer be
  caught by transport-level dedupe on restart.
- `context_budget.phase()` scans the full 40-line pane tail (not the live status
  region) for active-execution markers / background subagents, so a stale marker in
  scrollback can block a rotation that is actually safe (fail-safe,
  availability-only).
- Ghost detection (§2a) would degrade FAIL-UNSAFE only if a future Claude Code
  renders dim autosuggestion text AFTER real typed text — `prompt_text_from_styled`
  returns '' whenever SGR-2 is present, which would then hide genuine staged input
  from the /clear guard. Verified NOT the case in today's Claude Code; removal of dim
  styling degrades fail-safe as already noted in §8.
