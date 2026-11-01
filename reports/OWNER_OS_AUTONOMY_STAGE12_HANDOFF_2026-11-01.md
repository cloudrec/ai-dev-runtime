# Owner OS autonomy: Stage 12 handoff (independent-review fixes), 2026-11-01

An automated instruction was received through the Owner OS API: the tech-lead decision on the independent review. It is **not** owner approval.

- Implementer: Claude Opus 5.5. The fixes could not be routed to a Sonnet model from this session.
- The implementer also wrote the review, so this stage has **not been independently reviewed**. The Fable final audit is **PENDING**.
- **PROD: NO-GO. Live unattended E2E: NOT PROVEN.**

## Branch / commit
- Worktree: `/opt/owneros-autonomy`, branch `owneros/autonomy-finish`.
- Commits after `cbe4744` (whose code equals `dca2287`):
  - **`71fa288`**: review defects D-1..D-10, reviewer identity, immutable audit.
  - **`1571d36`**: false-idle hardening (confirmed quiet window).
- HEAD is the docs commit that adds this handoff, the review packet and the suite logs.
- Code changed: `core/fullcycle/{engine,executor,tmux_adapter}.py`, `core/wake_bridge.py`, `api/v1.py`. Tests and reports also changed.
- Production `/root/ai-dev-runtime` (live `9557004`) was **not touched**: no tests, edits, deploys, grants, credentials or pushes there.

## What changed (review IDs from `OWNER_OS_AUTONOMY_INDEPENDENT_REVIEW_2026-11-01.md`)
| ID | Fix |
|---|---|
| D-1 | The live adapter now treats `idle`, `dead` and `stale` as quiescent. `waiting_input` (unsubmitted text in the composer) is now **active**. |
| D-2 | A blocked or context_exhausted report that arrives after the stall answer **reopens** the same review as a new pending question, with a fresh event. The earlier decision key stays spent (`decision_key_already_used`). |
| D-7 / TL (1) | Hold timeout: a resend, cancel or clean-session resend held longer than `FULLCYCLE_STALL_HOLD_TIMEOUT` (default 3600 s, measured on the engine clock from the first hold) opens **one** owner gate per review, `stall_hold_timeout:<what>`. The dedup is the `fc_escalation` PK plus a conditional UPDATE. The owner's `resume` is the attestation that the attempt is over and carries out the tech lead's decision; `cancel` cancels. Without the owner, nothing is ever resent. |
| D-3 / D-9 | The pre-send handle now carries the session `target` and the previous attempt (`prev`). A refused send therefore still holds the pane and is still fenced on the old attempt. |
| D-4 | The rotation guard also holds panes whose attempt is waiting behind an `agent_permission_prompt` or `stall_hold_timeout` gate. A terminal task never holds a pane. |
| D-5 | A concurrent first migration tolerates `duplicate column`. |
| D-6 | `unknown_polls` resets on every confirmed send. |
| D-8 | A review that is re-announced moves `fc_review.event_id`. `wake_bridge._fullcycle_review_closed` then retires the older wake. |
| D-10 / TL (2) | The agent's summary, question, handoff and claimed files reach the tech lead and the next session **only** as fenced, labelled untrusted data: an `agent_report` with `trust: untrusted_data`, plus UNTRUSTED markers. The agent cannot forge or close a marker (`<<<`/`>>>` are defused). The control flow reads only protocol status fields. |
| TL (3) identity | `/fullcycle/reviews/*/ack` and `/decision` require an **authenticated per-client token principal** (`api:client/<id>`) that is listed in `FULLCYCLE_REVIEWERS`. A declared `X-Runtime-Actor` never authorizes. Allowlist entries that are not `api:client/` are ignored. **There is no default reviewer** (fail closed). |
| False idle (`1571d36`) | Before a resend, cancel or clean session after a stall or exhaustion, the agent must read `idle` **continuously, with an unchanged pane fingerprint**, for `FULLCYCLE_QUIET_CONFIRM_SECS` (default 60 s, engine clock). Any other reading, or a changed pane, restarts the window. The window is persisted per review (`fc_escalation.idle_since` / `idle_fp`), so it survives an engine restart. A crash or a delivered result needs no window. A pane that never confirms escalates to the owner through the hold timeout. The adapter gains `idle_evidence()`, which returns the verdict and the fingerprint from a single status read. |
| TL (3) audit | `fc_decision_audit` is append-only (UPDATE and DELETE triggers abort) and hash-chained (`verify_decision_audit()`). It records accepted and refused decisions, acks, reopen/supersede/escalation actions, and owner hold answers. |

## Tests
- New file `tests/test_fullcycle_idle_confirm.py` (9 tests) covers: a single idle reading never acts; a non-idle reading restarts the window; a changing screen is not idle; unknown never counts as idle; cancel also waits; the window survives an engine restart; a late result needs no window; the live adapter's single read; the default window is on. Mutation check: forcing an immediate window makes 4 of the 9 fail. The shared `fc` fixture keeps a 0 s window, because the fixture agent's idle is exact.
- New file `tests/test_fullcycle_review_fixes.py` (16 tests) covers:
  - D-1 against the real `classify_state` output
  - D-2 for blocked and context_exhausted, including the replayed-key idempotency case
  - the hold timeout: escalate exactly once, owner resume, owner-completed cancel, and no resend without the owner
  - stale wake retirement
  - fence-forging and injection attempts
  - audit immutability and tamper detection
  - anonymous-actor refusal and refused-decision auditing
  - D-3/D-9 across a busy-refused send
  - D-4
  - the D-5 race
  - D-6
  - stale terminal rows
- Mutation check: disabling the reopen or the escalation makes 6 of the new tests fail (6 failed, 9 passed).
- The API tests (`tests/test_fullcycle_api.py`) now authenticate the way `_auth` does. They prove that shared-token spoofing is refused, including spoofing a declared name that is on the allowlist, and that there is no default reviewer.
- Updated assertions for the labelled payloads: `test_fullcycle_stall.py`, `test_fullcycle_context.py`, `test_fullcycle_review_findings.py`.
- Focused run (fullcycle, wake, context-budget, closed-loop, hook, API): 580 passed and 1 failed, an assertion on the old payload shape. I fixed it and re-ran that file: 32 passed.

**Full-suite evidence** (each log's first line records the SHA; untracked files at run time were docs/report files only):

| commit | passed | skipped | failed | exit | log |
|---|---|---|---|---|---|
| `1571d36` (code HEAD) | **4348** | 4 | **0** | 0 | `reports/stage12/full_suite_1571d36.txt` (695 s) |
| `71fa288` | 4339 | 4 | 0 | 0 | `reports/stage12/full_suite_71fa288.txt` (798 s). Caveat: the `1571d36` edits were made in the worktree mid-run, after test collection. |

## Unresolved risks
1. **Owner gate for promotion: reviewer credential.** After promotion, every full-cycle decision is refused (403) until the owner does both of these:
   - provisions a per-client runtime token for the ChatGPT connector (`RUNTIME_CLIENT_TOKENS_FILE`);
   - sets `FULLCYCLE_REVIEWERS=api:client/<id>`.

   The seo MCP server (`/opt/seo`, not changed here) must also send that token. This is intended fail-closed behaviour, and it is a credential step only the owner can take.
2. **"idle" is the classifier's fallthrough.** Any state that does not match an active marker reads as idle. The guards against misreading are: active markers, a foreground shell, a transcript written within the last few seconds, and `agent_send`'s own busy refusal. A tool that writes nothing for a long time and shows an unrecognised spinner could still read as idle. Suggested hardening: require two idle readings at least about 60 s apart before a resend.
3. **The audit chain is tamper-evident, not tamper-proof.** It lives in the same DB, so root could drop the triggers and rebuild a consistent chain. Next step: anchor the head hash outside the DB, for example in the event log or the owner's channel.
4. **One owner escalation per review.** After the owner's attestation there is no further SLA. If the owner never answers, the task stays BLOCKED, while the rest of the project stays held, as before.
5. **Live DB migration on promotion.** It adds the `fc_task.unknown_polls` column, the `fc_escalation` and `fc_decision_audit` tables and two triggers. Back up `control_plane.db` first.
6. **The engine-level `submit_decision(by=…)` is not authenticated.** Authentication is enforced only at the API boundary. Any in-process caller on the host can still call it, though it now refuses an empty actor and audits the call. This trust boundary is the host itself.
7. Live unattended E2E is still NOT PROVEN, and the independent and Fable reviews of this stage are pending.

## Next concrete step
1. Run an independent review (Fable final audit) of `cbe4744..HEAD` using `reports/OWNER_OS_AUTONOMY_STAGE12_REVIEW_PACKET_2026-11-01.md` (questions R1–R8).
2. After that, these owner-gated steps: the reviewer client token, a DB backup, and a canary promotion following `OWNER_OS_AUTONOMY_STAGE7_REVIEW_PACKET_2026-11-01.md` §6.

**Rollback (branch only):** `git revert 1571d36 71fa288`.
