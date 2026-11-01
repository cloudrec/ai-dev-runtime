# Owner OS autonomy: Stage 14 audit readiness and live-E2E gate reduction, 2026-11-01

An automated instruction was received (Task #328). It is not owner approval.
- Author: Claude Opus 5.5. This is the implementer's own read, **not** an independent review.
- **The Fable review is PENDING**: this session cannot run it, because the instructions forbid creating agents.
- **PROD: NO-GO. Live unattended E2E: NOT PROVEN.**
- No code, grants, tokens, PROD, live DB or live browser were touched. The new items are reports, a re-run of one test, and one scratch experiment on a temporary DB.

## 1. Verified state
- Branch `owneros/autonomy-finish`, HEAD **`da6ab2f`**. The working tree was clean before this stage's reports were added.
- There are 35 commits since live `9557004`.
- `git diff --quiet 1571d36 HEAD -- core api tools hooks config` succeeds, so the **production code is `1571d36`**.

| evidence | excerpt | path |
|---|---|---|
| Full suite @1571d36 | `sha: 1571d361…` · `collected 4352 items` · `4348 passed, 4 skipped … 695.25s` · `exit=0` | `reports/stage12/full_suite_1571d36.txt` |
| Isolated E2E, re-run at clean HEAD | `sha: da6ab2f…` · `sends=[('tinycalc:add',1),('tinycalc:add',2),('tinycalc:mul',1),('tinycalc:cli',1)]` · `spoofs_refused=5 audit_rows=5` · `path=['dispatched','attempt_stalled','idle_unconfirmed','idle_unconfirmed','dispatched',…]` · `1 passed` | `reports/stage14/e2e_isolated_authenticated_da6ab2f.txt` |
| Stage 13 focused | `collected 169 items` · `169 passed` · `exit=0`. Caveat: run on the uncommitted test file. | `reports/stage13/focused.txt` |
| Stage 3 focused | `140 passed in 170.01s`. Caveat: **no SHA or command recorded**; later full suites cover it. | `reports/stage3/evidence_focused.txt` |
| Q7 contention (scratch) | 4 processes × 100 `_audit` appends on a temp DB → `exitcodes [0,0,0,0]`, `{'ok': True, 'rows': 400}`. Production connections use `sqlite3.connect(…, timeout=10)` (`core/control_plane/store.py:247`). | `reports/stage14/q7_audit_contention.{py,txt}` |

Correction to an earlier record: the Stage 13 E2E log (`reports/stage13/e2e_isolated_authenticated.txt`) was produced before its test file was committed. The Stage 14 re-run at `da6ab2f` replaces it as evidence.

### What the E2E actually exercised
**Real:**
- engine, contracts, git, private tmux, temp control-plane DB
- wake_bridge and wake_routes
- `wake_companion.tick` → `cdp_composer` (claim, latch, verify)
- `api/v1` with the real `_auth` (test-only token files)
- stall alert, idle window, untrusted labels, audit

**Simulated:**
- the LLM agent (fixture script)
- the ChatGPT page (fake CDP)
- the reviewer's judgment (deterministic policy)
- the clock (the engine clock is advanced 900 s per iteration)

**Not exercised at all:**
- the seo MCP server, which is how ChatGPT actually calls the API
- `agent_control.agent_send` into a real pane
- the context-budget controller loop
- the HMAC auth path
- a real Claude agent

## 2. Status of the seven questions (by this author's evidence)
| Q | Covered by tests | Unproven / gap |
|---|---|---|
| 1 Reopen | D-2 for both blocked and context_exhausted; replayed key refused; fresh decision applied (`test_d2_*`) | Reopen racing a concurrent submission for the same review (engine lease plus `WHERE status='pending'`, by code reading only) |
| 2 Bounded hold | Escalates once; owner resume sends once; owner-completed cancel; no resend without the owner (`test_d7_*`) | The `unsent` attempt arithmetic on resume after a busy-refused send; attestation scoping across a second stall of attempt N+1. **No test.** |
| 3 Idle window | 9 tests with a mocked evidence pair; mapping against the real `classify_state` | Behaviour on real Claude panes (spinner variants, status-line churn). **Live-only.** |
| 4 Rotation guard | D-3, D-4, terminal row (`test_d3_*`, `test_d4_*`) | The `agent_context_budget.evaluate` loop end to end against the new handle shape |
| 5 Untrusted text | Fence forging and labels (`test_agent_text_cannot_forge…`); the E2E asserts the fences | What the seo MCP renders to ChatGPT. **Not reviewed** (outside this repo). |
| 6 Identity | Shared-token spoofs → 403 in the API tests and the E2E; no default reviewer | The HMAC path (by code: `method != "client"` → 403, but untested); the seo MCP does not yet send a client token |
| 7 Audit and migration | Append-only triggers, tamper detection, the duplicate-column race (simulated), scratch contention | The migration on a live-sized DB that is in use. **Not done**, because the live DB is off limits. |

## 3. agency-agents cross-check: concrete remaining gaps only
Source: the upstream files fetched read-only on 2026-11-01.
- **Multi-Agent Systems Architect:**
  - *"Timeout behavior must be defined … SLA: define maximum wait time"* (l.346-347). The engine's hold escalates once (`FULLCYCLE_STALL_HOLD_TIMEOUT`), but **owner gates have no SLA or second-tier behaviour**. Gap.
  - *"Audit log: every tool call by every agent … Non-negotiable"* (l.307). Decisions are audited, but the agent's tool calls are not. Gap (out of the branch's scope).
- **Agents Orchestrator:** *"Maximum 3 attempts per task before escalation"* and *"Evidence required"* (l.43-44). Met: `FULLCYCLE_MAX_ATTEMPTS=3`, and verification comes from git and checks only. No gap.
- **MCP Builder:** *"Fail gracefully — return error content with `isError: true`"* (l.49) and *"input validation at the boundary"* (l.30). The API validates its input, but nobody has verified the seo MCP's mapping of 403/400 refusals to `isError`. Gap, owner of the gap: the seo repo.
- **Agentic Identity & Trust:**
  - *"credential lifecycle management: issuance, rotation, revocation, and expiry"* (l.24). Client tokens have **no expiry or rotation**. Gap.
  - *"Never trust mutable logs"* (l.50). The audit is append-only by trigger and hash-chained, but it sits in the same DB and has no external anchor. Partial gap.

## 4. Isolated LIVE E2E plan
Path under test: a real event → Owner OS wake → ChatGPT reviewer → authorized task → agent execution → Full Cycle decision.

**Setup (avoids PROD; nothing shared):**
1. A dedicated sandbox: separate `CONTROL_PLANE_DB` and `AGENT_CONTROL_DB` files, a separate branch API on its own port (for example 127.0.0.1:18080), a throwaway git repo as the project root, and its own `TMUX_TMPDIR`, so PROD's tmux server and watchers never see the pane.
2. A one-shot project spec with one trivial task (a function plus a test), `executor=agent_control`, and checks that run only the sandbox repo's tests.
3. A dedicated ChatGPT reviewer path. Either a separate Chrome profile on its own CDP port, or ChatGPT calling a sandbox MCP endpoint that points at the sandbox API with a sandbox client token.
4. Run unattended: the engine worker, the companion, and the ChatGPT reviewer. Record every event id, wake delivery, decision audit row and agent-log entry. The verdict is **pass only if no human touched any link**.

| need | avoidable without the owner? | how / why not |
|---|---|---|
| PROD control-plane writes, live DB backup, promotion | **Avoidable** | Sandbox DB files and the sandbox API port |
| Interference with the PROD tmux fleet and watchers | **Avoidable** | A separate `TMUX_TMPDIR`; the sandbox engine only |
| A grant change for the sandbox project | **Avoidable** | Spec-origin checks are trusted; no push or deploy (contract defaults) |
| Sharing the live browser (CDP :9222) | Avoidable only with a **code change** | `cdp_composer.CDP_PORT` is hard-coded 9222. Needs a small env knob (for example `CDP_PORT`) plus a separate Chrome profile. Not done: code changes were forbidden this stage. |
| **A logged-in ChatGPT session for the dedicated profile, or registering a sandbox MCP connector** | **Owner required** | It is the owner's ChatGPT account and credential |
| **A sandbox reviewer client token**, plus the sandbox MCP or reviewer sending it | **Owner required** (credential issuance) | The tech lead's directive forbids issuing tokens |
| **Starting one real Claude agent** in the sandbox pane (Claude usage) | **Owner required** | `core/agent_creation_policy.py` fail-closed creation policy, plus cost |
| Binding a wake route to a dedicated ChatGPT chat | Avoidable only if the sandbox has its own route table (sandbox DB) and conversation; the conversation itself needs the owner's account | |

**Minimal owner gate:** (a) approve one sandbox agent creation; (b) provide a ChatGPT reviewer path, either by logging into a dedicated profile or by registering a sandbox MCP connector; (c) authorize issuing one sandbox-scoped reviewer client token.

**Tech-lead decision needed before that:** whether to allow the `CDP_PORT` env knob, which is about a 2-line change in `tools/cdp_composer.py` plus a test, so that no live browser is shared.

## 5. Blockers
1. **The independent Fable review cannot be run from this session**, because creating agents is forbidden. It stays PENDING; the bundle is `reports/OWNER_OS_AUTONOMY_FABLE_REVIEW_BUNDLE_2026-11-01.md`.
2. **Live E2E:** owner gates (a)–(c) above, plus the tech-lead decision on the `CDP_PORT` knob.
3. **Promotion (later):** a live DB backup and the reviewer client token for the PROD connector, both owner-gated. The seo MCP must send the token and map refusals to `isError`.

## 6. Addendum: owner said to finish the safe remainder (same session, 2026-11-01)
The owner typed an instruction in the session to finish the work, notify ChatGPT and stop. This came from the owner directly, not through the API. It is **not** approval of PROD, tokens, agent creation or deploy.

**Commit `b9c8cec`:**
- `tools/cdp_composer.py`: a new `CDP_PORT` env knob. The default stays 9222. An invalid or privileged value yields 0, an unusable port, so it never falls back silently to the live browser. This removes the "shared live browser" need from the live-E2E plan in §4, though a separate Chrome profile is still required.
- `tests/test_stage14_gaps.py` (10 tests). All passed, and **no defect was found**:
  - **Q2:** an owner resume after an undelivered send keeps attempt 2 rather than 3, and an attestation never opens a later stall's fence (a new gate opens instead).
  - **Q6:** a valid HMAC signature gets 403 on ack and on decide.
  - **Q7:** the migration on a populated pre-branch DB (5000 tasks, 2000 reviews) with an open WAL reader succeeds in under 5 s, is idempotent, and gives old rows `unknown_polls=0`.
  - **CDP_PORT:** the knob's parsing.
- Full suite at `b9c8cec`: **4359 passed, 4 skipped, 0 failed**, exit 0 (4363 collected, 722 s). Log: `reports/stage14/full_suite_b9c8cec.txt`.
- The Fable bundle's primary range now extends to `b9c8cec` (adds `tools/cdp_composer.py` and `tests/test_stage14_gaps.py`). Rollback: `git revert b9c8cec` before the commits listed in the bundle.

**Still open (owner only):**
- the sandbox reviewer client token;
- approval to create one sandbox Claude agent;
- a logged-in ChatGPT in a dedicated browser profile, or a sandbox MCP connector.

The independent Fable review is PENDING. PROD stays NO-GO, and live E2E is still NOT PROVEN.
