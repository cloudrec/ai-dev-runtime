# Decision brief — G1 / D1 / D2 (2026-11-01)

Requested by an automated Owner OS instruction (tech lead), not owner approval. Based on
`reports/OWNER_OS_AUTONOMY_FINISH_STAGE4_2026-11-01.md` and `reports/OWNER_OS_AUTONOMY_STAGE3_4_HANDOFF_2026-11-01.md`.
**Nothing below is implemented.**

## 0. Git state (verified)
- Branch `owneros/autonomy-finish`, HEAD `073502f31ba243faeb51523738c519aea36d85dd`, clean before this file.
- Code head `5cfd7ab`. The commits after it change docs only.
- Production unchanged.

## 1. G1 — scope verification trusts the agent when git fails

**Defect.** In `core/fullcycle/engine.py` `_verify`:
- `_changed_files()` returns `None` whenever `git status --porcelain` exits non-zero. Causes include `index.lock`, a corrupt index, a permission error, or a root that is not a repo.
- `_verify` then sets `changed, source = result.get("changed_files", []), "agent_claim"`.
- So the scope and do-not-touch checks run on the agent's own claim. An agent that touched `README.md` but claims only `tinycalc/mul.py` passes `check_changes`.
- If the task's checks also pass, it goes to review as `passed=True`.

**Proof.** A throwaway test, run and deleted, not committed:
- Set-up: the regression fixture, with `_git("status")` forced to rc 128.
- Result: `passed=True, changed_source=agent_claim, violations=[]`.

**Same root cause, two other paths:**
- `_refusal` uses `dirty = self._changed_files()`; `None` is falsy, so the clean-tree precondition is silently skipped. A dirty tree is dispatched as clean.
- `_restore_violations` returns early when `changed_source != "git"`. That one is safe.

**Minimal isolated fix.** One file: `core/fullcycle/engine.py`. About 6 lines.
1. `_verify`: if `_changed_files()` is `None`, record `changed_source="unavailable"`, `changed_files=[]`. Add the reason `changed_files_unverifiable` and force `passed=False`. Still run the checks, so the feedback includes their output.
   - The failure goes down the existing path: `VERIFICATION_FAILED` → `_retry` (max 3) → owner gate `max_attempts_3:verification_failed`.
   - The agent's claim is kept in the evidence as `claimed_changed_files`, for the record only.
2. `_refusal` (attempt 1): if `_changed_files()` is `None`, return `git_status_unavailable`. That opens an owner gate at dispatch, so nothing unverifiable is sent. It is project-wide, not task-scoped, so `_parked` treats it as halting. Correct.

Option to reject: retry `git status` a few times before failing. That adds timing logic; the bounded task retry already covers transient locks.

**Regression tests** (`tests/test_fullcycle_e2e.py`):
- (a) git status failing during verify → `VERIFICATION_FAILED` with reason `changed_files_unverifiable`; never reaches review.
- (b) git status failing before the first dispatch → `OWNER_DECISION` `git_status_unavailable`; agent log empty.
- (c) a transient failure that clears → the next attempt verifies normally.
- (d) a non-git root keeps today's behaviour (until G1b is decided).
- Also re-run the full suite.

**Non-git roots are a supported mode today — this splits the fix.**
- `_commit_pending` (engine.py:812) checks `rev-parse --git-dir`. If that fails, it marks the task committed ("the owner commits; until then the dirty tree holds the next task").
- So a non-git project root is intended to run, and in that mode scope is *always* verified on the agent's claim. That is a policy choice, not a bug.
- **G1a (defect, deterministic):** `rev-parse --git-dir` succeeds but `status` fails → the fix above applies only in this case. No behaviour change for non-git roots.
- **G1b (policy):** non-git root → either (i) refuse at dispatch `project_root_not_git` (fail-closed, consistent with "never trust self-reported"), or (ii) keep it, but mark the review payload `changed_source: agent_claim` prominently so GPT knows scope is unverified. My recommendation is (i), but it removes a supported mode. **Tech lead to choose.**

**Recommendation: adopt G1a** (fail-closed, matches existing gates, no concurrency; about 8 lines with the `rev-parse` split). Decide G1b separately.

## 2. D1 — agent alive but silent (hang)

**Current code.**
- `_step_working` polls the adapter. `WORKING` has no deadline, so a task waits forever.
- `UNKNOWN` (the `agent_status` call raised) has no branch either, so it is silently treated like WORKING, also forever. This is a second, unreported hole of the same class.
- The live `TmuxAdapter` has `owns_agent = False`, so the engine cannot restart a live pane. A crash → `_block_for_owner("agent_crashed_not_owned")`. Only the test adapter owns its pane.
- The closed-loop SLO watchdog covers wake delivery, not fullcycle attempts.

**Options**

| | mechanism | safety risk |
|---|---|---|
| A | Deadline → one owner gate `attempt_stalled` (dedup by `gate_id`), no kill. Owner "resume": keep watching the same attempt (like `agent_permission_prompt`). "retry": redispatch attempt+1. "cancel" as today. | Lowest. Never acts on a live pane. Cost: an owner touch per hang. |
| B | Deadline → tech-lead review (`kind="stalled"`) with the pane tail; GPT answers "nudge", "wait" or "escalate". | Adds a review kind and a decision enum (API change to `REVIEW_DECISIONS` and the MCP tools). A "nudge" types into a live pane, which is direct-pane mutation: needs the allowlist and the busy check. |
| C | Deadline → kill + restart + redispatch, counted in `max_attempts`. | Highest. The adapter cannot restart a live pane (`owns_agent=False`). Killing mid-edit leaves a partial tree that the next attempt inherits; mid-migration or mid-deploy could be destructive. Rejected for live use. |

**Shared details for A and B:**
- `FULLCYCLE_ATTEMPT_TIMEOUT`, default 7200 s, measured from `fc_dispatch.ts` of the current attempt, which is already persisted, so it survives restarts.
- "Activity resets the clock" is NOT recommended: pane-tail heuristics are fragile, and a spinner looks like activity.
- `UNKNOWN`: count consecutive polls on the task (a new column, or a note). After N (e.g. 20) or after the same deadline → the same gate, reason `agent_status_unknown`.
- Exactly-once: the gate opens only when `gate_id` is empty. The `_open_gate` event is keyed `fullcycle:owner:{gate_id}`.
- A late result after the gate opened: `_owner_answers` "resume" goes back to WORKING, and the existing poll then finds DONE. If instead the owner chose "retry", the per-attempt result path makes the old attempt's late file ignored.
- Caveat: the old process may still write to the tree, so "retry" must be documented as "only after you have stopped the agent".

**Recommendation: A**, with `UNKNOWN` folded in. Small, no new API, no pane mutation. Tests:
- deadline → one gate and one owner event, with ticks idempotent;
- resume → same attempt, a late DONE is verified;
- retry → attempt+1, bounded by `max_attempts`;
- `UNKNOWN` streak → gate;
- the deadline survives an engine restart (uses the persisted ts).

## 3. D2 — agent context exhausted

**Current code.**
- The fullcycle engine has no context signal. The result contract is `status: done | blocked`.
- The separate `core/agent_context_budget.py` (used by agent_watch / direct agents) already has policy:
  - thresholds 45 / 55 / 65 %;
  - a safe-boundary gate ("NEVER /clear while a command … is active");
  - "write AND verify a fresh handoff BEFORE /clear";
  - cooldown + changed-hash so it cannot loop.
- It acts on live panes by itself, and nothing coordinates it with the fullcycle engine. A rotation mid-attempt today would look like WORKING forever (→ D1), or produce a result the engine still accepts. So D1 is a prerequisite.

**Options**

| | mechanism | safety risk |
|---|---|---|
| A | Agent-declared: the result may carry `status:"context_exhausted"` + `handoff` (bounded text). The engine treats it like a failed attempt with feedback = the handoff: redispatch attempt+1 to a fresh session, counted in `max_attempts`. The engine does not clear anything; a fresh session means the next envelope goes to a new pane/session that the owner or launcher provides. | Low. The handoff is self-reported, but only used as feedback; verification still runs on git + checks (after G1). Needs the session-reset step defined. With `owns_agent=False` the engine cannot reset, so the live default is an owner gate `context_exhausted_needs_new_session` with the handoff attached. |
| B | Engine-observed: fullcycle reads `agent_context_budget.detect_context_pct` from the pane tail and, at ≥65 % at a safe boundary (between attempts only), asks for a rotation before the next dispatch. | Medium. Parses the pane, overlaps a second controller on the same pane (a race with agent_watch rotation). |
| C | Leave it to agent_context_budget, and have fullcycle only tolerate it: the result arrives after the rotation, and the same attempt continues. | Depends on D1 and on the rotation being outside the attempt. Uncoordinated: two controllers. |

**Recommendation: A, gated by D1 + G1.**
- Smallest contract change: one more result status in `executor.py` + `_step_working`.
- No pane control from the engine. Bounded by `max_attempts`.
- On live (`owns_agent=False`) it lands in an owner gate carrying the handoff. The test adapter can exercise the full restart path.
- Coordination with `agent_context_budget` (it must not rotate a pane holding a fullcycle WORKING attempt) is a separate decision; I'd file it as D2b. **Needs tech lead**, because it touches live supervisor policy.

## 4. Test numbers for code SHA 5cfd7ab

| set | passed | failed | skipped | evidence |
|---|---|---|---|---|
| Full suite `python3 -m pytest -q -p no:cacheprovider tests/` | 4253 | 0 | 4 | exit 0, 576 s, `reports/stage4/full_suite_5cfd7ab.txt` |
| Fullcycle + autonomy-cycle + task-contract | 121 | 0 | 0 | `reports/stage4/evidence_focused_fullcycle_073502f.txt` (command, SHA, exit 0) |
| `tests/test_fullcycle_e2e.py` (subset of the above) | 18 | 0 | 0 | — |
| Stage 3 focused E2E set (8 files, at ccbcb25-era code) | 140 | 0 | 0 | `reports/stage3/evidence_focused.txt` (no command/SHA/exit inside the file) |

**All E2E are mocked:**
- the agent is a fixture script in a private tmux;
- ChatGPT is `StubReviewer`;
- the browser is a fake CDP page.

**Real (non-mock) gaps, unproven:**
1. Real ChatGPT reads the wake and calls the review MCP tools → next task, with no owner poke.
2. Real ChatGPT DOM/selectors under `cdp_composer`.
3. A real Claude Code pane through `TmuxAdapter` (`owns_agent=False`): permission dialog, crash, hang.
4. Real Telegram owner gate.
5. Real pane identity for `stage_done`.
6. Real context rotation interacting with fullcycle.

Live evidence covers only the stage_done → wake-decision leg (events 78155, 78167, 78237, 78336, 78347 → `wake` on owner-os).

## 5. Did Stage 4 already implement any of these?
- **G1: no.** Stage 4 did not touch `_changed_files` or the agent_claim fallback. `9a86a92` relies on `fc_dispatch` rows, not on git.
- **D1: no.** No deadline. Only the lease renewal (`5cfd7ab`), which is about engine ownership, not agent liveness.
- **D2: no.**
- Related and implemented:
  - `9a86a92`: owner-gate scoping. A G1 `git_status_unavailable` refusal would be project-wide, so it still halts everything, as intended.
  - `a4f87e0`: regression gate. Its check runs are independent of G1; its scope evidence is not.

## 6. Requested choices
1. **G1a:** adopt fix §1 (git repo + status failure → fail verify / refuse first dispatch)? Recommended: yes. **G1b:** non-git roots, (i) refuse or (ii) keep with flagged agent claim? Recommended: (i).
2. **D1:** option A with `UNKNOWN` folded in, timeout default 7200 s? Recommended: yes.
3. **D2:** option A, live default → owner gate with handoff? Plus a decision on D2b coordination.
