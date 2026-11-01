# Owner OS autonomy: Stage 13 canonical handoff, 2026-11-01

An automated instruction was received through the Owner OS API (Task #328). It is not owner approval.
- Author: Claude Opus 5.5. This is **not** an independent review, and the Fable final audit is **pending**.
- **PROD: NO-GO. Live unattended E2E: NOT PROVEN.**

## State (verified with git)
- Worktree `/opt/owneros-autonomy`, branch `owneros/autonomy-finish`.
- HEAD is the Stage 13 docs/test commit, whose parent is `94cfbc0`.
- Production code is identical to **`1571d36`**. Stage 13 added one test file and these reports; it changed no production code.
- Code commits since live `9557004`:
  - Stage 3–10: through `dca2287`.
  - `71fa288`: review fixes, identity, audit.
  - `1571d36`: idle confirmation.
- Live `/root/ai-dev-runtime` (`9557004`), PROD, the live DB, secrets, grants and money were **not touched**.

## Evidence
| what | result | path |
|---|---|---|
| Full suite @ `1571d36` | **4348 passed, 4 skipped, 0 failed**, exit 0 (4352 collected, 695 s) | `reports/stage12/full_suite_1571d36.txt` |
| Full suite @ `71fa288` | 4339 / 4 / 0, exit 0 | `reports/stage12/full_suite_71fa288.txt` |
| **Stage 13 isolated E2E** | 1 passed | `reports/stage13/e2e_isolated_authenticated.txt` |
| Stage 13 focused (autonomy cycle, E2E, API, fixes, idle, stall, wake_bridge) | 169 passed, exit 0 | `reports/stage13/focused.txt` |
| Stage 3 focused | 140 passed. Caveat: no SHA or command recorded; later full suites cover it. | `reports/stage3/evidence_focused.txt` |

The full suite was not re-run in Stage 13: production code is unchanged since the green run at `1571d36`, and only a test file was added.

## Stage 13 E2E: what is real and what is simulated
Test: `tests/test_autonomy_e2e_authenticated.py`. It runs as one unattended driver loop with **no manual nudges**. Each iteration does: engine tick → wake companion tick → reviewer turn over HTTP → advance the engine clock.

The path it observed:
1. Dispatch of `add` attempt 1. The agent hangs.
2. `attempt_stalled` review, then its wake is delivered to the project chat and verified.
3. The reviewer answers over the **authenticated API**.
4. The resend is fenced until the idle window opens and confirms (`idle_unconfirmed` → `dispatched`), which gives **exactly one** resend.
5. Verify, approve, then `mul`, then the project `add_tasks`, then `cli`, then project done.

Assertions it checks:
- 5 decisions, all from `api:client/sandbox-gpt`.
- 5 shared-token spoofs, all refused with 403.
- Audit chain valid: 5 `accepted` rows.
- Agent text arrived fenced as UNTRUSTED.
- No live CDP access.

**REAL links:**
- engine
- contracts
- git
- private tmux
- control-plane DB (temp)
- wake_bridge decision
- wake_routes
- wake_companion.tick
- cdp_composer claim/latch/verify
- `api/v1` router with the real `_auth`, using a test-only token and a client-token file in tmp

**SIMULATED (so this is not live proof):**
- the LLM agent: a fixture script
- the ChatGPT page: a fake CDP session
- the GPT reviewer's judgment: a deterministic policy
- wall-clock time: an accelerated engine clock

## Minimal owner gate for a genuine live unattended E2E
Every one of these is needed, and none can be done safely without the owner:
1. **Agent creation approval** for one scratch Claude agent pane on a throwaway git repo (the creation guard).
2. **A dedicated ChatGPT conversation**, with a wake route bound to it for a sandbox project (route-binding decision).
3. **A reviewer credential:** a per-client runtime token for the ChatGPT MCP connector, placed in `RUNTIME_CLIENT_TOKENS_FILE`, with `FULLCYCLE_REVIEWERS=api:client/<id>`. The seo MCP (`/opt/seo`) must send it. This is credential issuance.
4. **Running branch code beside PROD:** the branch engine, API and companion need a **separate** control-plane DB and port, or a promotion. Promotion needs a backup of `control_plane.db`, because the additive migration adds `unknown_polls`, `fc_escalation`, `fc_decision_audit` and the triggers.
5. **Shared live browser:** the companion drives the live CDP browser (:9222) that PROD's wake companion also uses. Concurrent use needs owner sign-off.

## agency-agents cross-check (gaps that remain)
- **Multi-Agent Systems Architect, HITL:** "timeout behavior must be defined / SLA". A hold now escalates once, but an owner gate itself has **no SLA**.
- **MCP Builder:** `isError` mapping. Nobody has verified whether the seo MCP maps the API's 403/400 refusals (`review_superseded`, `decision_key_already_used`, the 403 for an unauthenticated principal) to `isError` with a usable message. That code is outside this branch.
- **Agentic Identity & Trust:**
  - "Never trust mutable logs": the audit is a hash chain in the same DB, so it is **tamper-evident only**. There is no external anchor.
  - Credential expiry and rotation: client tokens have none.
  - Met: fail-closed authorization, a default of deny, and evidence-based decisions.
- **Agents Orchestrator:** the QA gate, bounded retries and "inconclusive → fail" are met.

## Seven critical review questions (for the independent / Fable reviewer)
1. **Reopen (`_blocked_review`):** can a reopened review apply an old decision, lose the agent's question, or be decided by a spent key?
2. **Bounded hold (`_hold`, `_owner_answers` HOLD branch):** is there exactly one gate per review? Is the owner's attestation scoped to that review? Is the attempt number correct on resume?
3. **Idle window (`_idle_confirmed`):** can any path act on a single idle reading? Is the pane fingerprint sound? A volatile idle pane escalates; is that acceptable?
4. **Rotation guard (`attempt_in_flight_for_agent`):** can any live or pending attempt be missed? Can a stale row block every pane?
5. **Untrusted text:** does any agent-written string reach the tech lead or the next session unfenced? Can the agent forge a fence?
6. **Identity (`_reviewer_principal`):** can the shared or HMAC token, or `X-Runtime-Actor`, authorize ack or decide? Is having no default reviewer acceptable?
7. **Audit and migration:** is the chain correct under concurrent writers? Are the triggers effective? Is the migration safe on a live DB that is in use?

## Independent Fable review scope
- `git diff 9557004..HEAD -- core api tools hooks config`. The priority is `cbe4744..1571d36`: `core/fullcycle/{engine,executor,tmux_adapter}.py`, `core/wake_bridge.py`, `api/v1.py`.
- Tests: `tests/test_fullcycle_review_fixes.py`, `tests/test_fullcycle_idle_confirm.py`, `tests/test_autonomy_e2e_authenticated.py`, `tests/test_fullcycle_api.py`.
- Packet: `reports/OWNER_OS_AUTONOMY_STAGE12_REVIEW_PACKET_2026-11-01.md` (R1–R8) plus the 7 questions above.
- Run only in the worktree. **Never** run it from `/root/ai-dev-runtime`.

## Remaining risks
- Promotion makes every review decision return 403 until the reviewer token exists. This is intended fail-closed behaviour.
- "Idle" is still a classification: a tool that is silent, static and not a foreground shell could pass the 60 s window.
- The audit is tamper-evident, not tamper-proof.
- Only one owner escalation per hold, with no owner SLA.
- The seo MCP's `isError` mapping is unaudited.
- The live E2E is not proven.

## Rollback (branch only)
`git revert <stage13 commit> 94cfbc0 1571d36 71fa288`, newest first. PROD needs no rollback, because nothing was promoted.

## Next action
1. The tech lead runs the independent Fable review against the scope above.
2. Then, as owner gates: decide whether to authorize the live E2E (gate items 1–5), or to promote to a canary after the backup and the token are in place.
