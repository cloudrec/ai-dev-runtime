# Full-cycle autonomous development — first vertical slice (2026-10-30)

Branch `ai-runtime/fullcycle-orchestrator` (worktree `/root/ai-dev-runtime-fullcycle`),
based on live `d8bcb62`. Not deployed, not pushed; the live tree, its 37 untracked files,
live panes, routes, grants and databases were not touched.

Requested by an automated instruction relayed from the ChatGPT tech lead over the Owner
OS API (not owner-verified). Direction confirmed by the owner in their own words the same
day: prove fully autonomous spec-to-result development with ChatGPT + agent first, then
Windows/macOS, then sales.

## What exists now

| Piece | File | Reuses |
|---|---|---|
| Versioned spec + acceptance criteria + checks + plan | `core/fullcycle/spec.py` | control-plane DB |
| Executor contract (envelope file in, result file out) | `core/fullcycle/executor.py` | — |
| Execution hosts: local, SSH; Windows = refusing TODO | `core/fullcycle/hosts.py` | — |
| Permission gates | `core/fullcycle/gates.py` | `agent_creation_policy.hard_denied`, `permission_resolver` |
| Live executor: existing agent via `agent_control` | `core/fullcycle/tmux_adapter.py` | `agent_control.agent_send/agent_status` |
| Engine: state machine, review inbox, retries, recovery | `core/fullcycle/engine.py` | `task_contract`, `control_plane.api` (events, leases, owner gates) |
| Review-inbox HTTP API for ChatGPT | `api/v1.py` `/api/v1/fullcycle/*` | v1 bearer auth |
| Fixture project, stand-in agent, private-socket tmux adapter | `tests/fixtures/fullcycle_demo/` | — |
| Runnable demo | `tools/fullcycle_demo.py --scenario happy\|crash\|wrong\|reject` | — |

Loop: spec → task (contract CREATED) → gates → envelope + one-line prompt to the agent →
result file → git diff ⊂ scope + task checks → event `task_completed` (wake bridge) →
GPT decision via inbox (approve / reject / answer / add_tasks) → VERIFIED_DONE → local
commit → next task → … → spec checks → `project_completed` event.

## Acceptance

| Criterion | Result | Evidence |
|---|---|---|
| Stable spec + acceptance-criteria store | PASS | append-only versions with sha; tasks pin their version; `test_fullcycle_spec_gates.py` |
| Technology-agnostic executor contract | PASS | envelope/result files; adapter verbs submit/poll/collect/restart |
| Persisted task/event state machine | PASS | `task_contract` history per task; `fc_*` tables; events in control plane |
| Event-driven GPT CTO handoff | PASS (mocked browser leg) | review event goes through the real `wake_bridge.should_wake/record/pending_wake/record_delivery/acknowledge`; nothing typed into ChatGPT |
| Safe permission gates | PASS | denied roots incl. containing roots, payment dirs, untested executors, push/deploy, reviewer-supplied checks, dirty tree; agent permission dialogs always go to the owner |
| Completed work returns for review, then next task | PASS | e2e full loop incl. GPT adding a missing task |
| Idempotent retries, duplicate wake | PASS | decision keys; review unique per attempt; dispatch latch + intent; dup-wake test |
| Crash recovery | PASS | engine killed after send, before commit, before recording verification, between BLOCKED and review; each resumes without a second prompt or lost work |
| Negative: agent crash / permission denied / prompt / wrong work / scope / reject / blocked / max attempts | PASS | `test_fullcycle_e2e.py`, `test_fullcycle_review_findings.py` |
| Real ChatGPT end to end on this loop | NOT RUN — blocked | see Blockers |
| Real Claude agent on this loop | NOT RUN — blocked | see Blockers |
| Remote/SSH executor | PARTIAL | SSH host contract proven through a local ssh stand-in; no real remote host; starting agent panes remotely is a TODO behind the creation guard |
| Windows / macOS | NOT STARTED (by priority) | `WindowsBridgeHost` refuses; macOS = same POSIX contract, never run on macOS |
| Qwen / Codex | EXPLICIT TODO | profiles exist, refused at dispatch until an end-to-end run is recorded |

Tests: 70 in `tests/test_fullcycle_*.py`, repeated 3× without a flake; the agent-policy
guard tests (`test_agent_automation_policy.py`, `test_agent_creation_adversarial.py`)
pass. Full suite at `65a05e3`: 4166 passed, 4 skipped (base `d8bcb62`: 4096 passed, 4 skipped).

## Independent review

An adversarial review agent (read-only) found 16 issues; it verified the top ones by
running code. All critical/high and the cheap medium ones are fixed in `a0eed20` with a
regression test each. The full suite then caught that the owned-pane tmux adapter typed
into and created panes outside `agent_control` and the creation guard — i.e. it could
have started an agent without the owner's creation approval. It was moved to test
infrastructure (`65a05e3`) instead of weakening the guards.

Still open from the review (low): partial-send residue is cleared with C-u only for the
test adapter; gitignored files are invisible to the scope check; a check of a spec-author
task runs as written (the spec author is trusted).

## Blockers for a real end-to-end canary

1. **ChatGPT cannot reach the inbox yet.** The `/api/v1/fullcycle/*` routes exist only
   on this branch. They need a deploy and an `ai-runtime` restart (owner approval), and
   the ChatGPT MCP connector needs tools for them (connector side, not in this repo).
2. **No route for a fullcycle project.** A review event wakes the chat bound to the
   project's route. Binding needs an owner-supplied conversation URL.
3. **No engine process.** Nothing ticks the engine in production yet; it needs a
   long-running worker (a systemd unit or a thread in the wake companion) — a live
   change (owner approval).
4. **The agent.** The only live executor is an EXISTING agent through `agent_control`;
   its target must be allowlisted or granted for this actor. Creating a dedicated agent
   for the canary goes through the creation guard (owner passphrase).

Smallest real canary once approved: deploy the branch, bind a fresh chat for project
`tinycalc` (fixture copied to a scratch dir), allowlist one existing scratch agent, run
the engine worker, let ChatGPT review through the API. Every piece except those four
approvals is ready.
