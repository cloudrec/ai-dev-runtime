# Agent Fabric v1 (task OWNER-192)

One abstraction over the two kinds of workers Owner OS drives:

| kind | authoritative store | ref shape |
|---|---|---|
| live tmux/Claude agent | `core.agent_control` + control-plane `agent` table | `tmux:<session:pane>` |
| runtime worker (job) | `core.job_store` (`runtime_jobs.db`) | `runtime:<job-uuid>` |

Design rule: the fabric is a **unifying view + lifecycle gateway, never a
second store**. Duplicate protection holds by construction — the fabric cannot
drift from a registry it does not own. Every mutating verb delegates to the
already-hardened primitive (duplicate proof, leases, idempotency keys, dialog
fail-closed rules stay intact). The wake/stuchalka delivery paths are
untouched: fabric emits nothing the sources don't already emit.

## Verbs (`core/agent_fabric.py`)

- `list_agents()` — unified inventory with per-entry: ref, kind, project,
  server, cwd, tmux target, session/conversation id, state, `fabric_state`
  (Task Contract vocabulary), current task, last activity, health,
  capabilities. One source failing never blinds the other (`errors[]`).
- `status(ref)` / `result(ref)` — live status / durable evidence.
- `start_or_resume(project_dir)` — fail-closed no-duplicate: refuses (with the
  live agent's ref) when a live Claude already owns the cwd; otherwise
  delegates to `agent_control.agent_resume`.
- `send(ref, text)` — tmux only; runtime workers take no interactive input by
  design (their instructions are the job row) — refusal, not emulation.
- `stop(ref, confirm=True)` — destructive, demands explicit confirm; runtime
  stop = job cancel, idempotent on terminal jobs.
- `handoff(ref, to_project_dir)` — durable audited intent (CTO event) + a
  start-or-resume at the destination. No pane surgery: the source keeps
  running until stopped explicitly.

## Task Contract (`core/task_contract.py`)

Machine-readable contract: GOAL / SCOPE / DO_NOT_TOUCH / ACCEPTANCE_CRITERIA /
TESTS_REQUIRED / LIVE_CHECK_REQUIRED / PUSH_ALLOWED / DEPLOY_ALLOWED /
OWNER_DECISIONS / EXPECTED_REPORT. Unknown fields are refused loudly; powers
default to off.

State machine (fail-closed; history append-only in
`fabric_contract_transition`):

```
CREATED -> WORKING | BLOCKED | OWNER_DECISION | CANCELLED
WORKING -> BLOCKED | OWNER_DECISION | AGENT_DONE | CANCELLED
BLOCKED -> WORKING | OWNER_DECISION | CANCELLED
OWNER_DECISION -> WORKING | BLOCKED | CANCELLED
AGENT_DONE -> VERIFYING | CANCELLED          # a claim, not a result
VERIFYING -> VERIFIED_DONE | VERIFICATION_FAILED
VERIFICATION_FAILED -> WORKING | BLOCKED | OWNER_DECISION | CANCELLED
```

`VERIFIED_DONE` and `VERIFICATION_FAILED` are unrecordable without evidence;
`VERIFIED_DONE` additionally demands `tests` evidence when the contract says
`tests_required` and `live_check` evidence when `live_check_required`.

## HTTP surface (`/api/v1/fabric/*`, bearer/HMAC auth as all v1)

- `GET  /fabric/agents[?include_terminal=true]`
- `GET  /fabric/agents/{ref}/status` · `GET .../result`
- `POST /fabric/agents/{ref}/send` · `POST .../stop`
- `POST /fabric/start-or-resume`
- `GET/POST /fabric/contracts`, `GET /fabric/contracts/{id}` (with history),
  `POST /fabric/contracts/{id}/transition`

Refusals surface as HTTP 409 with the exact reason.

## Tests

`tests/test_agent_fabric.py`, `tests/test_task_contract.py` — inventory
unification, source-failure independence, duplicate-start refusal, runtime
send refusal, confirm-gated stop, evidence-gated verification, terminal
states, history.

## v1 limits (deliberate)

- `server` is always `local`; multi-host fabric needs a transport first.
- `handoff` moves the task, not the pane.
- MCP exposure rides the existing HTTP surface; no separate MCP server added.
