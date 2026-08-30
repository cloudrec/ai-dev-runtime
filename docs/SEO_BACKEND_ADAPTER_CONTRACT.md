# seo-backend thin-adapter contract (Owner OS 2.0)

Architecture decision (owner, 2026-08-15): **all universal Owner OS 2.0 core
logic lives in `/root/ai-dev-runtime`** — Supervisor/Doctor, Agent Fabric,
Task Contracts, runtime lifecycle/events, Venture Radar and scoring when they
land. seo-backend is a **thin adapter**: UI/API surface + Postgres task board,
delegating everything else over this HTTP contract. No seo-backend rebuild is
needed to evolve the core; the adapter changes only when this contract does.

## Transport

- Base: `http://172.17.0.1:8199/api/v1` (docker bridge — seo-backend already
  reaches it for `/jobs`).
- Auth: `Authorization: Bearer $RUNTIME_TOKEN` or the HMAC header pair —
  identical to the existing jobs client.
- Refusals: fabric/contract endpoints return **409 + exact reason** (fail-
  closed refusal is an answer, not an error); 404 unknown ids; 422 shape.

## Surface (pinned by `tests/test_adapter_contract.py`)

Runtime jobs (already consumed today): `POST/GET /jobs`, `GET /jobs/{id}`,
`POST /jobs/{id}/approve|cancel`.

Observability: `GET /runtime/status` (active/stalled/waiting_approval/
recent_failed with liveness evidence), `GET /control-plane/observability`.

Agent Fabric: `GET /fabric/agents`, `GET /fabric/agents/{ref}/status|result`,
`POST /fabric/agents/{ref}/send|stop`, `POST /fabric/start-or-resume`,
`GET|POST /fabric/contracts`, `GET /fabric/contracts/{id}` (with history),
`POST /fabric/contracts/{id}/transition`.

Venture Radar (task 193): `GET|POST /radar/candidates`,
`GET /radar/candidates/{id}`, `POST /radar/candidates/{id}/card|transition`,
`POST /radar/seed`. Candidate cards use the closed CARD_FIELDS vocabulary;
APPROVED/REJECTED/BUILDING transitions require `by="owner"` (forwarded owner
decisions only).

Business Analyzer (task 202): `GET|POST /analyzer/cards`,
`GET /analyzer/cards/{id}`, `POST /analyzer/cards/{id}/rescore|transition`,
`POST /analyzer/combine`. Seven fixed score axes, each requiring a written
rationale; build/spend/publish/outreach states are owner-only.

## Model router (task 209)

`POST /router/route`, `POST /router/outcome`, `GET /router/effectiveness`,
`GET /router/policy`. Routing decisions and outcomes are recorded here; the
adapter renders effectiveness and forwards outcomes, it never re-implements
the policy.

Refs: `tmux:<session:pane>` / `runtime:<job-uuid>`. Fabric states use the Task
Contract vocabulary (WORKING/BLOCKED/OWNER_DECISION/AGENT_DONE/VERIFYING/
VERIFICATION_FAILED/VERIFIED_DONE + CREATED/CANCELLED).

## Adapter responsibilities (and nothing more)

1. Mirror `owner_tasks` rows into runtime jobs / fabric contracts; keep its
   `runtime_retry`-style lineage columns pointing at runtime job ids.
2. Render `/fabric/agents` + `/runtime/status` in the owner UI.
3. Forward owner approvals (`/jobs/{id}/approve`, contract transitions with
   `by="owner"`). It never re-implements policy, retry, stall detection or
   verification — those live here.

## Non-goals until an owner-sanctioned seo-backend rebuild

The baked image stays as-is; this contract is forward-prepared. Nothing in the
core requires the adapter to exist.
