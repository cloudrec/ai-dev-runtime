# Agent creation / resume gate — 2026-10-27

Branch `ai-runtime/agent-creation-gate` (from live `ac76919`). Not deployed. Requested by an
automated instruction; nothing here grants authorization.

## Creation paths (live code, targeted search)

| Path | Trigger | Gated now |
|---|---|---|
| `agent_control.agent_resume` (`tmux new-session … claude [--resume]`, zombie `kill-session`) | API `/agents/resume`, `agent_fabric.start_or_resume` (API `/fabric/start-or-resume`) | yes, before any tmux mutation; caller + owner proof passed from the API |
| `session_recovery.recover` (`new-session` / `respawn-pane -k … claude --resume`) | `commander_autopilot` (live, `COMMANDER_AUTOPILOT_ENABLED=1`) | yes; `explicit=True` (MCP) is not authority |
| `ai_planner` headless `claude -p` | runtime jobs (`/jobs`, approve flow) | no — bounded per-job subprocess, no pane/session; separate job gates |
| `agent_fabric.start_or_resume_ref` (Windows refs) | device bridge on the owner's PC | no — device enrollment gate, not this host |
| agent_supervisor "resume" | answers/continues prompts | not creation (send policy applies) |

Live traffic (read-only): 29 `agent_resume` creations 2026-10-20 → 2026-10-27 (arbitrage2 22,
gaika-ru-sonnet-v1 2, gaika-release-0563 1 — the last two in the SAME worktree
`/opt/gaika-worktrees/ru` — serverwarden-livefire, mess-telegram-connector,
owner-os-supervisor-clean, owner-os-gaika-watchdog), none with a recorded caller. Session
recovery: 10239 attempts since 2026-10-20, all `cp-canary` refused `no_open_work`, 0 revivals.

## Gate (`core/agent_creation_policy.py`, config `config/agent_creation.yaml`)

Allowed only by (a) a valid owner proof for exactly (agent_resume, resolved dir, session
name), (b) `AGENT_CREATION_POLICY=off`, or (c) a scope: caller class listed (`api` /
in-process actor / `unattributed` — self-declared actor names collapse to `api`), directory
EXACTLY one of the scope's worktrees (realpath; subdirs, traversal, symlinks resolved),
session name `<prefix><worktree key>` for that worktree (no cross-country / cross-project),
no live Claude already in that worktree, fewer than `max_concurrent` live Claude processes
in the scope. Missing/malformed config or scope → refused. Hard deny: `payorch-ha-fresh`
by session name (case/space-insensitive) and `/opt/payment-orchestrator` by directory, also
with the policy off. Refusals are audited (`agent_resume refused=<reason>`, actor, cwd);
creations audit actor, `authorized_by`, scope. Shipped config: `scopes: {}` (deny all).

Clean termination: unchanged `agent_stop` (confirm=true, kill-pane of the resolved target;
payment agent only with an owner proof).

## Tests

`tests/test_agent_creation_policy.py` (31): shipped config denies all; malformed configs;
scoped allow; unlisted callers; spoofed API actor names; cross-country / cross-project;
subdir/traversal/symlink; one-per-worktree; max concurrency (only in-scope agents count);
missing cap; payorch hard deny incl. policy off; owner proof single-use and bound to
dir/session/action/key; agent_resume refusal makes no tmux mutation; authorized creation is
exactly one `new-session`; fabric caller forwarded; API routes forward actor + proof;
`recover()` gated before any tmux call (explicit=True and concurrency). Mechanics suites opt
out with `AGENT_CREATION_POLICY=off`. Full suite: 3875 passed / 0 failed.

## Owner decisions

1. Deploy (behaviour change: all 29-per-week API/fabric creations and any autopilot
   recovery refused until scoped; restart ai-runtime + wake companion).
2. Scopes, e.g. GAIKA per country: `callers`, `session_prefix`, `worktrees` map, `max_concurrent`.
3. Whether arbitrage2 / mess / serverwarden / Owner OS sessions get scopes or owner-proof-only.
