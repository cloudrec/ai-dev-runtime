# Stage 15 sandbox defaults, 2026-11-04

An automated instruction was received through the Owner OS API. It is not owner approval, and these are DEV defaults, not owner decisions.
**Design only. Nothing created: no user, instance, token or service. Live E2E: NOT PROVEN. PROD: NO-GO.**

## Defaults
| item | default |
|---|---|
| Test MCP | separate compose project, own `.env` root 0600, own Postgres, `RUNTIME_URL` at the sandbox API, not `/opt/seo` |
| Sandbox API | `127.0.0.1:18080`, own `CONTROL_PLANE_DB`, `AGENT_CONTROL_DB`, `TMUX_TMPDIR`, clients file |
| Reviewed agent | own non-root uid, not in `docker`, no read of the runtime's or the MCP's files |
| Runtime | a different uid from the agent |
| Reviewer token | one throwaway client id, one listed MCP token, deleted after the run |

## Why not one shared non-root uid
The second audit proposed runtime and agents under one uid, plus accepting a clients file owned by the euid. That moves the problem. An agent with the runtime's uid can rewrite `runtime-clients.json` with its own hash and decide as the listed client, and can read the owner proof key. So the `st_uid` check stays as it is until the runtime and the agent have different uids. Root-owned files then still work, which needs no code change if the runtime stays root.

## Blockers in code, checked at `owneros/stage15-final-fixes`
1. **The main one: an agent runs under the runtime's own uid.** No `su`, `setpriv` or `setuid` in `core/agent_control.py`, `core/tmux_control.py` or `core/fullcycle/hosts.py`, and the tmux socket follows the euid (`core/tmux_control.py:83`). Dropping privileges for the agent is a product change, and `recovery_command` accepts only plain `claude` shapes (F4), so a wrapper there would be refused.
2. `/root/.claude/projects/` is hard-coded for session lookup: `core/agent_control.py:676,713`.
3. `core/ai_planner.py:19` falls back to `/root/.local/bin/claude`; `RUNTIME_CLAUDE_BIN` overrides it.
4. Root-only file checks: `api/v1.py:156`, `core/agent_control.py:1912`, `core/automation_grants.py:77`. Fine for a root runtime, a blocker for a non-root one.
5. No `fc-agent` user exists on the host.

## What this means for the first run
- Today nothing separates an agent from the token on one host, whichever instance holds it, while the agent runs as root.
- **Option A, recommended for the tool itself:** a product stage for blocker 1, runtime stays root, agent drops to its own uid. Closes H1 for agents. Sized and audited as its own task.
- **Option B, fastest:** first sandbox run as root with a throwaway token and sandbox-only DBs. The second audit accepts this for a sandbox only. It proves the loop, not the separation, and must not be read as closing H1.
Choice is the owner's.

## Offline harness
`tests/test_stage15_seo_client_roundtrip.py` is what can run without a token: real seo client, real API, real socket, seeded review. Local integration, not a live E2E.
