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

## Adversarial review (follow-up, same branch)

Requested by an automated instruction; no agents started, no live changes.

| Attack / gap | Finding | Fix + test |
|---|---|---|
| Direct tmux / Popen bypass | only `agent_control.agent_resume` and `session_recovery.recover` create panes; `claude` spawned only by `ai_planner` (`-p` jobs) and `native_sessions` (`agents --json`) | invariant test pins this and that both sites create under the lock |
| TOCTOU: simultaneous launches | **real**: no lock between check and `new-session`; a new Claude is invisible to the inventory for a moment | `creation_guard()` (host-wide `flock`) around check→create; `reserve()` counts the worktree live for 180 s; 8 parallel launches → exactly 1; 9 across 3 countries → exactly 2 (cap). Removing the lock or reservations makes these tests fail |
| Recovery race | backoff sleep (≤900 s) between the check and tmux | fast pre-check kept; locked re-check with a fresh inventory right before tmux; reservation released when the revived process is gone |
| Symlink rename / retarget | **real**: a country path re-pointed at another country made the match order-dependent | two worktrees resolving to one dir → `creation_scope_ambiguous`; a configured worktree that is a symlink → `creation_scope_worktree_is_symlink`; renamed-away worktree → out of scope |
| Caller spoofing | every API caller is `api` whatever `X-Runtime-Actor` says | local `tools/agent_create.py` is the only `owneros_controller`; API spoof of that name refused |
| Policy-off / owner-proof duplicates | reservations ignored on those paths | reservation check on every allow path, before a proof is consumed |
| Deny boundaries | `/opt/payment-orchestrator-evil` not under the deny dir; subdirs are | boundary tests; payorch name denied for the controller too |
| Host overload | no global cap | optional `max_live_agents` |

Not fixed (documented): any LOCAL process (including a running agent) can execute
`tools/agent_create.py`; the scope still bounds what it can create (names, worktrees, caps).
A per-caller secret would be needed to tell the controller from other local processes —
the owner-proof path exists for that if wanted.

## GAIKA readiness (fixture only)

`tests/fixtures/agent_creation_gaika_3_countries.yaml`: ru / kz / uz worktrees, callers
`[owneros_controller]`, `session_prefix: gaika-`, `max_concurrent: 2`, `max_live_agents: 12`.
Host today: 16 cores, 62 GB (46 GB free), 8 live Claude processes at ~330 MB each → memory is
not the limit; subscription rate limits and supervision are. Recommendation: start at
`max_concurrent: 2` per scope and `max_live_agents: 12`; raise only after the first two
countries run cleanly.

## Compatibility with the live send policy

Live: `AGENT_AUTOMATION_POLICY=enforce`, `AGENT_AUTOMATION_TARGETS=owner-os-supervisor-clean@api`.
Created country agents are NOT reachable by any automatic sender until the owner adds scoped
send entries, e.g. `gaika-ru@cto_bridge` (keyless CTO advice) — tested. The controller
creates locally (`tools/agent_create.py`, caller `owneros_controller`); ChatGPT/MCP cannot
create or message country agents under these settings.

## Deployment checklist (owner-gated)

1. Approve deploying this branch (merge into the live line, fast-forward from `ac76919`).
2. Config `config/agent_creation.yaml` stays `scopes: {}` at first (deny all automatic
   creation; owner-proof only) — or the owner supplies the GAIKA scope with real worktrees.
3. Back up HEAD, `config/agent_creation.yaml`; restart `ai-runtime.service` and
   `owner-os-wake-companion.service`.
4. Verify: `/health` 200; `agent_resume` via API → `creation_not_in_any_scope` (audit
   `refused=`); `session_recovery` refusals `creation_policy:*` for unscoped targets; no
   creations in `agent_control.jsonl` since restart.
5. Rollback: `git reset --keep ac76919`, restart both services (or `AGENT_CREATION_POLICY=off`
   in both drop-ins + `.env` as the escape hatch). New state files
   `agent_control.db.creation.{lock,json}` are inert to older code.

Owner gates: deploy; scope contents (countries, worktrees, cap); which caller may create
(`owneros_controller` recommended); send entries for created agents; optional owner key.

## Offline validation report (2026-10-27, after the adversarial fixes)

Requested by an automated instruction. Code under test: `7e477d8` + the cross-process race
test committed with this report (no production code change). Nothing deployed or started.

| Check | Result |
|---|---|
| Full suite, TZ=Europe/Berlin (system) | 3898 passed, 0 failed (6 m 34 s) |
| Full suite, TZ=Europe/Kyiv | 3898 passed, 0 failed (6 m 32 s) |
| Thread races (`-k "simultaneous or reservation"`), 25 repeated runs | 25/25 green |
| NEW cross-process race: 6 Python processes, real `flock`, Claude never visible, session names never colliding | 1 created, 5 `worktree_reserved_by_recent_creation`; 5/5 runs green |
| Sabotage, threads: lock removed / reservations removed | 1 / 3 race tests fail (both mechanisms load-bearing) |
| Sabotage, processes: lock AND reservations removed | 6 of 6 processes create a session (the duplicate the gate prevents) |
| Live directory | no `agent_control.db.creation.*` files written by tests |

Known limitations:
1. Any local process (including a running agent) can run `tools/agent_create.py`; the scope
   bounds what it can create, but it is not distinguishable from the controller without a
   per-caller secret (owner proofs are the existing alternative).
2. Reservations last 180 s; a Claude process that takes longer to appear in the inventory
   could permit a second creation after that window (one-per-worktree via session name and
   `find_live_agent_for_dir` still apply once it is visible).
3. The lock is host-local (`flock`); creation from another host is out of scope (none exists).
4. `ai_planner` `claude -p` job subprocesses and Windows-ref starts are not gated here.
5. The fixture is test-only; real GAIKA worktrees/scopes do not exist yet.
