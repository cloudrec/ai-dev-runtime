# Runtime fallback plan (deterministic, provider planner unavailable)

The AI provider planner did not return a usable plan, so this Runtime
job proceeded on a deterministic local fallback plan instead of failing.

- **Goal:** [RETRY] GAIKA agent watchdog
- **Planner failure:** model did not return JSON
- **Timed out:** False

## Task instructions (verbatim)

GAIKA agent watchdog — Implement a production-safe per-agent watchdog/autopilot loop for existing Claude/tmux agents, starting with target gaika-ru-sonnet-v1:0.0.

Goal: stop requiring the owner/ChatGPT to manually notice "idle" and push the agent after every completed chunk. Use existing Owner OS agent primitives (status/read/send/answer/resume or the internal equivalents) and existing scheduler/runtime patterns. Do NOT create duplicate Claude sessions or worktrees.

Required behavior:
1. Poll/inspect target frequently enough for active development (roughly 1-2 minutes; use existing scheduler/event loop conventions, do not busy-loop).
2. state=working: no intervention.
3. state=idle:
   - inspect recent pane output;
   - if a safe next step is explicit, send a concrete continuation;
   - if a long background shell is running, verify progress rather than trusting the shell count; while it progresses, direct the agent to useful read-only analysis/scratchpad work if tracked files must not be edited.
4. state=waiting_owner/waiting_input:
   - read the question;
   - auto-answer ONLY safe, reversible, local maintenance/development questions covered by the standing envelope below;
   - if a real gate remains, leave it for owner and continue any independent safe work instead of stopping the whole project.
5. dead agent: resume the SAME conversation/worktree/session only if no matching live agent exists.
6. After a completed chunk/report, verify the agent actually begins the next safe step; do not leave a bare "continue with the next safe step" sitting idle.
7. Dedupe/idempotency/cooldown so the watchdog cannot spam repeated messages or oscillate.
8. Fail closed: if classification is uncertain, do not perform risky action; surface it for owner.
9. Persist audit events explaining what watchdog observed, what it sent/answered, and why.
10. Add tests for working/idle/waiting_owner/background-shell/dead/dedup scenarios, plus restart durability.

GAIKA standing envelope:
AUTO-SAFE without owner approval:
- safe local code/tests/docs/report work;
- read-only analysis;
- stale-lock reap only when dead PID/process is independently confirmed and project has a штатный reap path;
- specific retailer re-crawls already listed by the project when they do not involve CAPTCHA bypass, credential use, paid proxy/VPN, or production release;
- local daemon/process health inspection;
- preparing fixes in scratch/tmp while mutation runner owns tracked files.
ALWAYS OWNER-GATED:
- PROD/release;
- money/spend/purchases;
- secrets/credentials;
- CAPTCHA/anti-bot bypass or stealth/residential rotation;
- enabling/using paid proxy/VPN unless explicitly authorized;
- irreversible/destructive operations;
- material product/policy decisions where code/docs do not already establish the rule.

GAIKA priority queue when multiple safe tasks exist:
A) product readiness / exact EXEC_CODE_READY coverage;
B) missing country/retailer execution mechanisms (target: >=2 retailers per supported country);
C) collector reliability / due refreshes;
D) live-test blockers and VPN-needed evidence;
E) planner/regression edge cases.
Do not let low-value planner polish displace country/mechanism readiness.

Acceptance:
- watchdog can keep gaika-ru-sonnet-v1 progressing across at least one idle->continue transition in a deterministic test/simulated pane;
- no duplicate agent/session creation;
- risky owner gate is not auto-approved;
- safe waiting_owner example is auto-resolved;
- repeated identical observation does not spam;
- implementation documented with enable/disable/target config and logs;
- all relevant tests green;
- commit changes and report exact files/tests/commit and how to enable it for gaika-ru-sonnet-v1:0.0.

Do not modify GAIKA application code in this task. This is Owner OS/runtime infrastructure only.

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

- output tokens: 515
- input tokens: 2
- cost_usd: 0.051513
- duration_ms: 5074

## Sanitized raw planner response (secrets redacted, truncated)

```
I'll inspect the existing agent/runtime primitives before designing the watchdog so it reuses established patterns instead of duplicating them.
```

> Fallback runs never merge, never force-push, and never delete. Any
> dangerous or irreversible action is left for a human.
