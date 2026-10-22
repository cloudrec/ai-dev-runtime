# Runtime fallback plan (deterministic, provider planner unavailable)

The AI provider planner did not return a usable plan, so this Runtime
job proceeded on a deterministic local fallback plan instead of failing.

- **Goal:** [RETRY] Agent event supervisor
- **Planner failure:** planner timed out
- **Timed out:** True

## Task instructions (verbatim)

Agent event supervisor — Build the missing Owner OS orchestration layer that turns existing agent events into reliable ChatGPT/assistant-driven continuation.

Context:
- Owner OS already detects and stores events such as work_stopped_incomplete, agent_waiting_input, agent_prompt_needs_response, agent_process_failed, completion/quiescent.
- The current failure is downstream: events are visible in notifications, but they do not reliably cause: wake assistant -> inspect exact agent/result -> verify work -> classify next action -> continue same agent or ask owner.
- Do NOT build another project-specific GAIKA watchdog. Build one general supervisor for all managed tmux/Claude agents.

Required behavior:
1) Event ingestion
   - consume relevant new agent events from the existing Owner OS event/notification pipeline;
   - durable cursor/idempotency so each event is processed once unless explicitly retried;
   - dedupe repeated quiescent/waiting notifications for the same unchanged agent state.
2) Per-agent state ledger
   - store target/session/project/conversation/worktree;
   - last event id/type/time;
   - observed state (WORKING, VERIFYING, DONE, CONTINUE, OWNER_NEEDED, FAILED, PAUSED);
   - last meaningful progress fingerprint/time;
   - last assistant decision/action;
   - next expected action;
   - cooldown/retry counters.
3) Assistant wake/delivery
   - for significant events, issue the existing wake-assistant mechanism with enough context to identify project+agent+event;
   - prove delivery, not merely enqueue;
   - retry safely on transient delivery failure without notification storms.
4) Decision policy
   - assistant verifies agent_status + recent output/result before acting;
   - safe/reversible/local continuation can continue same agent;
   - genuine owner gates (money/spend, secrets/credentials, production/release/publish, irreversible/destructive, CAPTCHA/anti-bot bypass, substantive product choice) become OWNER_NEEDED;
   - if an owner gate exists, preserve the question and allow unrelated safe work when explicitly permitted by project policy;
   - PAUSED projects/agents must never be auto-resumed.
5) Same-agent continuation
   - never create a duplicate tmux session/process/worktree when the same target/conversation exists;
   - continue/resume the same agent only after verifying it is idle/waiting/dead and the action is permitted.
6) Completion verification
   - an agent saying "done" is not enough;
   - supervisor/wake must verify requested artifact/tests/deployment evidence appropriate to the task;
   - if incomplete, classify CONTINUE and return a concrete next step;
   - if verified, DONE and notify owner only when useful.
7) Progress/stall detection
   - distinguish Claude idle from a background shell still progressing;
   - progress fingerprint should include state/output change and optionally git/artifact/test evidence;
   - don't spam "continue" while active work is progressing;
   - configurable cooldown/max autonomous continuations per hour.
8) Auditability
   - append-only/auditable log of event -> observation -> decision -> action -> delivery result;
   - status endpoint/tool output showing per-agent current state, last check, last progress, next action.
9) Safety/defaults
   - fail closed;
   - per-project overrides/denylist/read-only rules must remain enforceable (e.g. project-specific read-only agent rules);
   - no production/release permissions are broadened by this work.
10) Tests
   - deterministic tests for: working no-op; idle->safe continue; waiting owner safe; waiting owner gated; idle+background shell progressing; stalled shell; process dead resume same conversation; paused agent no-op; duplicate prevention; repeated event dedupe; cooldown; wake delivery retry; verification turning false "done" into CONTINUE; verified completion -> DONE.
   - preserve/repair existing wake-assistant proof/delivery tests rather than bypass them.
11) Deployment
   - integrate into the existing Owner OS / ai-runtime scheduler/event loop, persistent across restart;
   - expose a live status so we can prove the supervisor is actually running.
   - perform only the minimal safe internal service reload/restart needed to activate this Owner OS feature; do not touch unrelated project production services.

Acceptance evidence:
- tests green;
- commit SHA;
- live supervisor enabled;
- live status shows current monitored agents;
- one deterministic/live-safe proof that an existing synthetic/test agent event reaches wake processing and results in the expected no-op/continue decision;
- no duplicate agent created;
- docs for operation/disable/inspect.

Do not touch GAIKA application code.

## Repository metadata

- git repo: True
- branch at planning time: ai-runtime/220-windows-bridge
- head: 3b2fe5e
- remote: git@github-ai-dev-runtime:cloudrec/ai-dev-runtime.git
- tests/ dir present: True

## Conservative execution stages

1. inspect repository
2. create or preserve the correct task branch
3. implement the requested change
4. run focused tests
5. run the relevant full suite
6. commit
7. push
8. open or update a draft PR (never merge)
9. stop on any dangerous or irreversible action

## Test commands

- `python3 -m pytest -q`

## Planner accounting (preserved when available)

- output tokens: None
- input tokens: None
- cost_usd: None
- duration_ms: None

## Sanitized raw planner response (secrets redacted, truncated)

```
(empty)
```

> Fallback runs never merge, never force-push, and never delete. Any
> dangerous or irreversible action is left for a human.
