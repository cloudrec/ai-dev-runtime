# Fable adversarial-review bundle: Owner OS autonomy (Task #328), 2026-11-01

This bundle is for a clean, independent reviewer. It was prepared by the implementer (Claude Opus 5.5), so treat every claim in it as untrusted. An automated instruction was received; nothing here is owner approval. **The independent Fable review is PENDING. PROD: NO-GO. Live E2E: NOT PROVEN.**

## Target
- Worktree `/opt/owneros-autonomy`, branch `owneros/autonomy-finish`, HEAD `da6ab2f3c7d1`.
- Production code is identical to **`1571d361185b`**. Commits after it only add tests and docs.
- Diff ranges:
  - **Primary** `cbe4744c6fbd..1571d361185b -- core api`: 5 files, +478/−95. These are the Stage 12 fixes: `core/fullcycle/{engine,executor,tmux_adapter}.py`, `core/wake_bridge.py`, `api/v1.py`.
  - **Full branch** `955700417adb..1571d361185b -- core api tools hooks config`: 19 files, +1187/−103. `9557004` is the live tree.

## Four critical test files
1. `tests/test_fullcycle_review_fixes.py` (16 tests): D-1..D-9, injection, audit immutability.
2. `tests/test_fullcycle_idle_confirm.py` (9 tests): the false-idle window.
3. `tests/test_fullcycle_api.py` (6 tests): authenticated principal, spoofing, no default reviewer.
4. `tests/test_autonomy_e2e_authenticated.py` (1 test): the unattended sandbox loop, using the real `_auth`.

## Commands (run only inside the worktree; `tests/conftest.py` isolates the DBs and walls off live CDP)
```
cd /opt/owneros-autonomy && git status --short && git rev-parse HEAD
python3 -m pytest -q -p no:cacheprovider tests/test_fullcycle_review_fixes.py \
  tests/test_fullcycle_idle_confirm.py tests/test_fullcycle_api.py \
  tests/test_autonomy_e2e_authenticated.py tests/test_fullcycle_stall.py tests/test_wake_bridge.py
git diff cbe4744..1571d36 -- core api
```
Full suite (about 12 min): `python3 -m pytest -q -p no:cacheprovider tests/`. The recorded result at `1571d36` is **4348 passed, 4 skipped, 0 failed** (`reports/stage12/full_suite_1571d36.txt`).

**Never run anything from `/root/ai-dev-runtime`.** It is the live tree, and its suite opens real browser tabs.

## Questions to attack (each needs a verdict: CONFIRMED-SAFE, DEFECT with file:line and a repro, or NEEDS-DECISION)
1. **Reopen** (`engine._blocked_review`): can a reopened review apply an old decision, lose the agent's question, or be decided by a spent key?
2. **Bounded hold** (`_hold`, the HOLD branch of `_owner_answers`): is there exactly one gate per review? Can the owner's attestation be reused for another review or attempt? Is the attempt number right on resume when a prepared send was never delivered (`unsent`)?
3. **Idle window** (`_idle_confirmed`, `AgentControlAdapter.idle_evidence`): can any path act on a single idle reading? Is a sha256 of the last 1500 characters of the pane a sound "unchanged" test?
4. **Rotation guard** (`attempt_in_flight_for_agent`): can a pending or live attempt be missed? Can a stale row block every pane?
5. **Untrusted text** (`untrusted`, `agent_report`, `_agent_blocked_payload`, `_review_payload`): does any agent string reach the tech lead or the next session unfenced? Can the agent forge a fence?
6. **Identity** (`api/v1._reviewer_principal`): can the shared bearer token, HMAC, or `X-Runtime-Actor` authorize ack or decide?
7. **Audit and migration** (`_audit`, `verify_decision_audit`, `_TRIGGERS`, `_MIGRATIONS`): is the chain correct under concurrency? Is the migration safe on a live DB that is in use?

## Known caveats (stated so they are not mistaken for findings)
- The E2E simulates the LLM agent, the ChatGPT page, the reviewer's judgment and wall-clock time. It is **not** live proof.
- **Q7 concurrency:** only a scratch contention run exists (4 processes × 100 appends, chain OK: `reports/stage14/q7_audit_contention.txt`). There is no committed test.
- **Migration:** never run against a copy of the live DB, because that DB is off limits.
- The audit is tamper-evident only. Root can rebuild the chain.
- `submit_decision(by=…)` is authenticated only at the API boundary.
- `cdp_composer.CDP_PORT = 9222` is hard-coded.
- The Stage 3 focused log records no SHA (`reports/stage3/evidence_focused.txt`).
- After promotion, every review decision returns 403 until the owner issues a reviewer client token. This is intended.

## Rollback (branch only; nothing was promoted)
`git revert da6ab2f 47a568f 94cfbc0 1571d36 71fa288`. Older stages are listed in the Stage 3–10 handoffs.

## Deliverable
Save the review as `reports/OWNER_OS_AUTONOMY_FABLE_REVIEW_<date>.md`, recording the exact HEAD, the verdict for each of Q1–Q7, and the commands run. Do not edit code, PROD, grants or tokens.
