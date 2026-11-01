# Owner OS autonomy — Stage 5: G1 / D1 / D2 implementation (2026-11-01)

Direction: an automated instruction was received via the Owner OS API (ChatGPT tech-lead decisions on G1/D1/D2).
It is **not** owner sign-off. Background: `reports/OWNER_OS_AUTONOMY_DECISION_BRIEF_G1_D1_D2_2026-11-01.md`,
`reports/OWNER_OS_AUTONOMY_STAGE3_4_HANDOFF_2026-11-01.md`.

## 0. State
| | |
|---|---|
| Worktree / branch | `/opt/owneros-autonomy` / `owneros/autonomy-finish` (one agent, no subagents) |
| Base of this stage | `b7d0da1` (docs; code = `5cfd7ab`) |
| Commits | `eb3b30d` G1 · `9fb024a` D1 · `bf38344` D2+D2b · + this report |
| PROD | **Unchanged.** No push/merge/deploy; live tree, grants, global DB, live browser, other sessions untouched. |
| Approved for PROD | Nothing. Independent review required (§6). |

## 1. Decisions as implemented
| decision (tech lead) | implementation |
|---|---|
| **G1a** git status/HEAD errors fail closed | `_verify`: no git evidence → `changed_source=unavailable`, violation `changed_files_unverifiable`, never the agent's claim (kept as `claimed_changed_files`, record only). `_refusal`: first attempt with unreadable status → `git_status_unavailable`; every dispatch: `_git_refusal` (`rev-parse --is-inside-work-tree`, `--verify HEAD`) → `git_unavailable:*` / `git_head_unavailable:*`. `_commit_pending`: a failing `git add`/`diff` never marks a task committed (was: rc ignored). Staging = `add -A` + `reset -- .fullcycle` (an exclude pathspec naming a gitignored `.fullcycle` made `git add` exit 1). |
| **G1b** refuse non-git roots, Full Cycle only | `project_root_not_git` at dispatch (project-wide owner gate). Only `core/fullcycle/engine.py`; other project kinds untouched. |
| **D1** alert ChatGPT once at 7200 s or 20 UNKNOWN polls; never kill/restart/redeliver while prior attempt may be active; fence + verify before retry; max 3; tech lead, not owner | One `blocked` review carrying `stalled` per attempt (unique row per task/attempt/kind). Deadline from persisted `fc_dispatch.ts`; UNKNOWN streak in new additive column `fc_task.unknown_polls`. Task stays WORKING (late results still verified). "answer" → resend through `_stall_fence_open`: previous attempt's result checked first (late result → verified, resend dropped), then `adapter.quiescent()` must be **True** (positive idle/dead evidence; None/False wait). "cancel" held the same way (task stays in flight → no next task beside it). `_retry` bound = `max_attempts` (3) → owner gate only at the limit (existing escalation). |
| **D2** preserve compact handoff; routine handoff → ChatGPT review; safe stop + valid grant before clean session; owner gate only if privileged | Result `status: context_exhausted` + `handoff`, validated by `clean_handoff` (≤8000 chars; control chars removed / cut → named in `handoff_problem`; missing/empty/non-text reported, not dropped). Task → BLOCKED + `blocked` review (tech lead). "answer": safe stop (`quiescent` True) required; adapter that owns its agent → `restart` = clean session; **live agent → owner gate `clean_session_required`**, because the grant system has no session-lifecycle action (`ACTIONS = send/answer/create`) — adding one is a grant-policy change, not made. Handoff + answer = next attempt's feedback. Bounded by `max_attempts`. |
| **D2b-a** no rotation during an in-flight attempt | `engine.attempt_in_flight_for_agent(target)` (read-only; WORKING contract or pending send on that session; no tables → False; error → True). `agent_context_budget.evaluate()` returns `context_deferred_fullcycle_attempt` before any checkpoint-handoff write (it writes into the project root → would dirty the verified tree) or `/clear`. `fullcycle_holds` fails closed. |

## 2. Changed files (`git diff --stat b7d0da1..bf38344`)
`core/fullcycle/engine.py` +288 · `core/fullcycle/tmux_adapter.py` +45 · `core/fullcycle/executor.py` +6 ·
`core/agent_context_budget.py` +16 · tests: `test_fullcycle_e2e.py` +85 (G1, 6 tests), `test_fullcycle_stall.py` new (22),
`test_fullcycle_context.py` new (28), fixtures `fake_agent.py` (`exhausted` behaviour), `private_tmux.py` (`quiescent`).

## 3. Tests
| run | result |
|---|---|
| G1 tests before fix | 6 failed (reproduced); after: pass |
| D1 / D2 fence mutation (fence removed → tests must fail) | D1: 3 failed; D2 + D2b: 3 failed — restored after |
| fullcycle + autonomy-cycle + task-contract + context-budget + agent_watch suites at `bf38344` | **457 passed**, 0 failed |
| full isolated suite at `bf38344` (`python3 -m pytest -q -p no:cacheprovider tests/`) | **4309 passed, 4 skipped, 0 failed, exit 0** (774 s) = 4253 + 56 new (G1 6, D1 22, D2 28) — `reports/stage5/full_suite_bf38344.txt` |

Covered as requested: late completions (after alert; while resend fenced; stale answer after late result), duplicates
(same-key decision twice → one resend; other key → `review_already_decided`; repeated ticks → one alert/event), lost status
(UNKNOWN streak; readable status resets it; status lost during fence → still fenced), no concurrent agents (resend and
cancel fenced while `quiescent` False/None; mul not sent beside a cancelled-but-running attempt), malformed handoffs (missing,
empty, non-text, control chars, over-long, wrong attempt), engine restart (deadline + once survive).

**Mocked vs real:** agent = fixture script in private tmux; reviewer = hand-driven `submit_decision`; time = shifted clock;
`quiescent` overridden per test; live `AgentControlAdapter.quiescent` only unit-tested against a fake `agent_status`. No real
Claude pane, ChatGPT or browser was exercised. No real-wake claim.

## 4. Risks (for the reviewer)
1. **Live idle evidence is pane-text heuristics** (`agent_control.classify_state` → `waiting_input`). A misread could open the
   fence early; mitigated by agent_control's own busy refusal on send (`_BUSY_REFUSALS`).
2. A fenced resend waits indefinitely if the agent never reads idle (by design: never redeliver while possibly active); only one
   alert per attempt — no second escalation.
3. D2b blind window: between the pre-send handle write and the adapter's handle (with `target`) on the FIRST dispatch, the
   predicate cannot map the task to a session.
4. D2b matches by tmux session (`split(":")[0]`). Checked: the only `evaluate()` caller (`agent_orchestrator.py:673`) passes
   `agent_key = agent["target"]` (`agent_orchestrator.py:526`), the same tmux-target form as the adapter's handle `target`.
   Session-level matching is conservative (any pane of that session is held).
5. Live clean session is confirmed by the owner's "resume"; the engine does not verify the session is actually fresh.
6. A late result after a stall alert leaves the alert review pending in the inbox (an answer is then applied as stale — harmless, noisy).
7. **G1b on promotion:** any existing Full Cycle project with a non-git root will stop at dispatch. Check live project roots
   (read-only) before promoting.
8. **Schema:** first `connect()` after promotion runs `ALTER TABLE fc_task ADD COLUMN unknown_polls` on the live control-plane
   DB (additive). Backup that DB in the promotion plan.
9. Behaviour change on promotion: git errors now block Full Cycle work that previously passed on the agent's word.

### Explicitly UNRESOLVED (not fixed in this stage)
- **Non-git Full Cycle roots** — refused by design (G1b); any live project of that kind stops at dispatch on promotion. Live roots not inspected.
- **Stale stall review** — after a late result the stall alert stays `pending` in the inbox; answering it is applied as stale (no action), but it is noise for the tech lead.
- **Unverified fresh-session identity** — a live clean session is accepted on the owner's "resume"; the engine does not verify the session is new. The fixture path (restart) is the only verified one.
- **No live no-human-prompt E2E** — final autonomy is NOT claimed.

## 5. Rollback
Branch only. `git revert bf38344 9fb024a eb3b30d` (reverse order; bf38344 builds on 9fb024a's `_stall_review`/fence). Earlier
stages: see Stage 3/4 reports. The added column is harmless to old code (old SELECTs name their columns).

## 6. Independent review packet (author self-notes are not an independent review)
- `reports/stage5/diff_production_b7d0da1..bf38344.patch`, `reports/stage5/diff_tests_b7d0da1..bf38344.patch`
- Review first: `_stall_fence_open`, `_clean_session_ready`, the WORKING-stall branch in `_apply_decisions` (cancel held),
  `_blocked_review` slot reuse, `attempt_in_flight_for_agent` + `fullcycle_holds`, `_git_refusal` / `_verify` / `_commit_pending`.
- Questions: (a) is `waiting_input` sufficient idle evidence for a resend? (b) should a long-fenced resend escalate once more?
  (c) should a session-lifecycle grant action replace the live `clean_session_required` owner gate (owner/grant-policy decision)?

## 7. Successor handoff
1. `cd /opt/owneros-autonomy && git status && git log --oneline b7d0da1..HEAD`
2. Read this file; never run tests from `/root/ai-dev-runtime`.
3. Open: independent review (§6 + Stage 3/4 packets) → tech-lead answers to §6 questions → owner-gated promotion plan
   (installer, control-plane DB backup for the additive column, rollback, companion/runtime restart, check of live Full Cycle roots for G1b).
