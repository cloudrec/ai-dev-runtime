# Automation policy — predeployment compatibility, rollout and rollback (2026-10-27)

Prepared under an automated instruction; read-only against live. No config, DB, service,
agent, owner proof or secret was created or changed. Every scope/live step below is an owner
decision.

## 1. Live traffic inventory (agent_control audit logs, 2026-10-19 22:37 → 2026-10-27 02:08 UTC)

569 delivered/acted events (`tools/automation_policy_dryrun.py`, read-only):

| Sender family | Caller class | Events | Targets |
|---|---|---|---|
| native supervisor (`NATIVE_SUPERVISOR_TARGETS=*`) | `native_supervisor` | 393 | giftcards-bot-review-opus 156 (now closed), bitwhite-backend-v3 130, gaika-ru-sonnet-v1 101, gaika-release-0563 4, aes-canary 2 |
| Owner OS API → this controller pane | `api` | 63 | owner-os-supervisor-clean (send + answer) |
| API/MCP → other agents | `api` | 51 | gaika-ru-sonnet-v1 28, owner-os-gaika-watchdog 18, gaika-release-0563, mess-telegram-connector, aes-canary, serverwarden-livefire |
| native goals (`/goal`) | `native_goals` | 3 | aes-canary |
| agent_stop (owner closes / pauses via API) | unattributed | 8 | aes-canary, bootstrap, giftcards, gaika-ru-sonnet-v1, owner-os-gaika-watchdog, gaika-release-0563 |
| agent_resume (agent creation) | unattributed | 51 | arbitrage2-opus 44, gaika-*, mess-telegram-connector, serverwarden-livefire, owner-os-supervisor-clean |

- **payorch-ha-fresh: 0 sends, 0 answers, 0 stops** in the window → the hard deny and the
  stop guard change nothing observed.
- **Human-typed owner input** goes straight into a pane, never through agent_control →
  absent from the ledger and unaffected by any setting here.
- Ledger retention is ~21 h (`deliveries` 51 rows); the audit log is the longer source.

## 2. Impact (dry-run, exact candidate policy)

| `AGENT_AUTOMATION_TARGETS` | Blocked | Kept |
|---|---|---|
| empty (fail-closed default) | 510/569: all native supervisor, all Owner OS→controller, all API/MCP sends | stops (confirm-gated), resumes |
| `owner-os-supervisor-clean@api` | 447/569: native supervisor + API/MCP to agents | Owner OS → controller (63) |
| `owner-os-supervisor-clean@api,bitwhite-backend-v3@native_supervisor` | 317/569 | + supervisor nudges to bitwhite (130) |

A bare entry (`owner-os-supervisor-clean`) would also let the native supervisor type into
the controller pane — use `@caller` entries only. Combined with the native CTO deploy line,
CTO typing needs `<controller>@cto_bridge,<worker>@cto_bridge`; without them requests stay
`pending_refused` (verified in `tests/test_native_cto_tmux_e2e.py`).

## 3. Remaining gaps (not changed)

- `agent_resume` / `tmux_launch` (agent creation, 51 events) is outside this policy.
- Senders without an actor (watchdog, orchestrator, context budget, phase advance, fabric)
  can only be scoped as `@unattributed`; giving them actors is a follow-up.
- Until deployed, the live native supervisor keeps nudging real project agents (bitwhite,
  gaika) under `TARGETS=*`.

## 4. Rollout (test-only first; each stage owner-approved)

Code: `ai-runtime/candidate-combined` (live `ca5a1a1` + native CTO deploy line + allowlist +
owner proof + rollout audit + DST fix) — fast-forward from live. Full suite: see PR.

| Stage | Change | Precondition | Verify | Rollback |
|---|---|---|---|---|
| 0 | none — dry-run | — | `venv/bin/python tools/automation_policy_dryrun.py --targets '<proposed>'` on the latest logs | — |
| 1 | deploy code with `AGENT_AUTOMATION_POLICY=off` (drop-in) | owner OK to fast-forward + restart | only payorch is affected (0 observed); `grep -c hard_denied_target /var/log/ai-runtime/agent_control.jsonl` | `git reset --keep ca5a1a1`, restart |
| 2 | `AGENT_AUTOMATION_POLICY=enforce`, `AGENT_AUTOMATION_TARGETS=owner-os-supervisor-clean@api` (drop-in **and** runtime `.env`, the hook-spawned supervisor reads `.env`) | owner chooses list | Owner OS messages still reach the controller; `grep -c automation_target_not_allowlisted …` rises only for the blocked families | set `AGENT_AUTOMATION_POLICY=off` (drop-in + `.env`), restart |
| 3 | add scoped entries one agent at a time (`<session>@<caller>`) | owner names agent + caller | dry-run first, then the same grep | remove the entry, restart |

Drop-in (owner writes it; example for stage 2):
```
# /etc/systemd/system/ai-runtime.service.d/94-agent-automation-policy.conf
[Service]
Environment=AGENT_AUTOMATION_POLICY=enforce
Environment=AGENT_AUTOMATION_TARGETS=owner-os-supervisor-clean@api
```
Owner proofs (optional, a secret): `tools/owner_proof.py init-key` as root; without it no
owner-proven overrides exist and payorch stays untouchable by every caller.
