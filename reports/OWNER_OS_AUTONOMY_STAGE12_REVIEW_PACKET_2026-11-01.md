# Owner OS autonomy: Stage 12 independent-review packet, 2026-11-01

This packet is self-contained, for a reviewer starting from a clean context. It was written by the implementer (Claude Opus 5.5), so treat every claim in it as untrusted and verify against the source. An automated instruction from the tech lead was received; nothing here is owner approval. **PROD: NO-GO. Live unattended E2E: NOT PROVEN.** The Fable final audit is pending.

## Scope
- Worktree: `/opt/owneros-autonomy`, branch `owneros/autonomy-finish`.
- Range under review: **`cbe4744..HEAD`**. `cbe4744` holds the code previously reviewed as `dca2287`.
- Code diff: `git diff cbe4744..HEAD -- core api` covers `core/fullcycle/{engine,executor,tmux_adapter}.py`, `core/wake_bridge.py` and `api/v1.py`.
- Tests: `tests/test_fullcycle_review_fixes.py` and `tests/test_fullcycle_idle_confirm.py` are new. Updated: `tests/test_fullcycle_{api,stall,context,review_findings,e2e}.py`.
- Background: `reports/OWNER_OS_AUTONOMY_INDEPENDENT_REVIEW_2026-11-01.md` (defects D-1..D-11) and `reports/OWNER_OS_AUTONOMY_STAGE12_HANDOFF_2026-11-01.md`.
- **OFF LIMITS:** `/root/ai-dev-runtime` (live), any DB other than test/tmp DBs, tokens, grants, deploys and pushes. Do not run the suite from the live tree.

## Questions (answer each with CONFIRMED-SAFE, DEFECT (file:line plus a repro), or NEEDS-DECISION)

**R1 — Reopen.** `engine._blocked_review` reopens a decided or superseded stall review as pending.
- Can a reopen apply an old decision?
- Can it lose the agent's question?
- Can it double-wake the tech lead?
- Can a replayed or old key decide the new question? (`submit_decision`: the `decision_key_already_used` path.)
- Is it correct to delete `fc_escalation` on a reopen?

**R2 — Bounded hold.** Covers `engine._hold`, `_idle_confirmed`, `_stall_fence_open`, `_clean_session_ready`, the cancel path in `_step_working`, and the hold-gate branch in `_owner_answers`.
- Does exactly one owner gate open per review?
- Can the owner's attestation be reused for a different review or attempt?
- Can the hold ever resend without either the owner or confirmed idle?
- Is the attempt number correct on the owner's resume? (Look at the `unsent` handling.)

**R3 — Idle confirmation.** Before acting on "idle", the agent must read idle continuously with an unchanged pane fingerprint for `FULLCYCLE_QUIET_CONFIRM_SECS` (default 60). The window state lives in `fc_escalation.idle_since` / `idle_fp`.
- Bypass cases: a CRASHED pane, or a result that arrives late.
- Is any path left that acts on a single idle reading?
- Is the fingerprint (a sha256 of the last 1500 characters of `recent_activity`) a sound "unchanged screen" test?
- What happens if the idle pane has volatile text? The expected answer: it never confirms, so it escalates to the owner after `STALL_HOLD_TIMEOUT`.

**R4 — Pre-send handle and rotation guard.** Covers `_try_dispatch` (handle `target` / `prev`) and `attempt_in_flight_for_agent`.
- Does any pending or live attempt fail to hold its pane?
- Can a stale terminal row block rotation for every agent?

**R5 — Untrusted agent text.** Covers `untrusted()`, `agent_report()`, `_agent_blocked_payload`, `_review_payload`, and the clean-session feedback.
- Does any agent-written string still reach the tech lead or the next session unfenced?
- Can the agent close or forge a fence?
- Does any control decision read agent text?

**R6 — Identity.** Covers `api/v1._reviewer_principal` and `fullcycle_reviewers`.
- Can the shared bearer or HMAC token, or `X-Runtime-Actor`, ever authorize ack or decide?
- Is it acceptable that there is no default reviewer? (Promotion then needs the owner to provision a client token.)

**R7 — Audit.** Covers `_audit` and `verify_decision_audit`, plus the triggers in `_TRIGGERS`.
- Append-only enforcement.
- Chain correctness under concurrent writers (`BEGIN IMMEDIATE` on a shared connection).
- Are the right events recorded?
- Known limit: the chain is tamper-evident only, and root can rebuild it.

**R8 — Migration.** Additive changes: the `fc_task.unknown_polls` column, the `fc_escalation` and `fc_decision_audit` tables, and the triggers. Check that the concurrent `duplicate column` case is tolerated, and that an old DB without these keeps working.

## How to test (only in the worktree)
```
cd /opt/owneros-autonomy
python3 -m pytest -q -p no:cacheprovider tests/test_fullcycle_review_fixes.py \
  tests/test_fullcycle_idle_confirm.py tests/test_fullcycle_api.py tests/test_fullcycle_stall.py \
  tests/test_fullcycle_context.py tests/test_fullcycle_e2e.py tests/test_wake_bridge.py
```
`tests/conftest.py` isolates the DBs and walls off live CDP. For full-suite evidence, see `reports/stage12/`.

## Known residual risks (not defects found by the implementer)
1. Promotion needs an owner credential step: a per-client token for the ChatGPT connector, and the seo MCP must send it.
2. The audit is tamper-evident, not tamper-proof. Anchoring its head hash externally is the next step.
3. There is one owner escalation per review, with no SLA after it.
4. `submit_decision(by=…)` is authenticated only at the API. In-process callers on the host are trusted.
5. The idle confirmation is only as good as the classifier and the pane fingerprint. A tool that is silent, static and not a foreground shell could still read as idle after 60 s.
