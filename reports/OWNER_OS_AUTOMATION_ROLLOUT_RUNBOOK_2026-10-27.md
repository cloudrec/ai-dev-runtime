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
| 1 | fast-forward code; `AGENT_AUTOMATION_POLICY=off` in all three contexts (below) | owner OK to fast-forward + restart **both** services | only payorch is affected (0 observed); `grep -c hard_denied_target /var/log/ai-runtime/agent_control.jsonl` | `git reset --keep ca5a1a1`, restart **both** services |
| 2 | `AGENT_AUTOMATION_POLICY=enforce`, `AGENT_AUTOMATION_TARGETS=owner-os-supervisor-clean@api` in all three contexts | owner chooses list | Owner OS messages still reach the controller; `grep -c automation_target_not_allowlisted …` rises only for the blocked families | `AGENT_AUTOMATION_POLICY=off` in all three contexts, restart both |
| 3 | add scoped entries one agent at a time (`<session>@<caller>`) | owner names agent + caller | dry-run first, then the same grep | remove the entry, restart both |

**Three process contexts run automatic senders** (verified 2026-10-27 from live process
environments): `ai-runtime.service` (API, native controller, orchestrators),
`owner-os-wake-companion.service` (`native_supervisor.scan`, `agent_watch.scan`; has its own
`NATIVE_SUPERVISOR_TARGETS=*`), and the hook-spawned `tools/native_supervise_once.py` (reads
`/root/ai-dev-runtime/configs/.env`). A setting missing from any one leaves that context on
the fail-closed default (enforce, empty list) — safe, but it blocks that context entirely.
The code fast-forward and its rollback need both services restarted (each imports
`agent_control` at start); hook-spawned passes pick up new code on their next start.

Drop-ins (owner writes them; stage 2 example — same content in both):
```
# /etc/systemd/system/ai-runtime.service.d/94-agent-automation-policy.conf
# /etc/systemd/system/owner-os-wake-companion.service.d/94-agent-automation-policy.conf
[Service]
Environment=AGENT_AUTOMATION_POLICY=enforce
Environment=AGENT_AUTOMATION_TARGETS=owner-os-supervisor-clean@api
```
plus the same two `KEY=value` lines in `/root/ai-dev-runtime/configs/.env`; then
`systemctl daemon-reload && systemctl restart ai-runtime.service owner-os-wake-companion.service`.
Owner proofs (optional, a secret): `tools/owner_proof.py init-key` as root; without it no
owner-proven overrides exist and payorch stays untouchable by every caller.


## 5. Stage 0 result (executed 2026-10-27, read-only)

Live env (from `/proc/<pid>/environ`): no `AGENT_AUTOMATION_*` anywhere (policy code not
deployed); `NATIVE_SUPERVISOR_TARGETS=*` in ai-runtime, wake companion and `configs/.env`;
no owner key. Dry-run with this branch's policy code:

| Source | Window (UTC) | Empty list | `owner-os-supervisor-clean@api` |
|---|---|---|---|
| live `deliveries` ledger | 2026-10-26 05:37 → 2026-10-27 02:41 | 52/55 blocked | 38/55 blocked; 14 Owner OS→controller allowed |
| audit logs (current + rotated) | 2026-10-19 22:37 → 2026-10-27 02:41 | 516/575 blocked | 452/575 blocked; 64 Owner OS→controller allowed |

Blocked under the minimal scope (audit window): native supervisor → giftcards 156 (closed),
bitwhite-backend-v3 130, gaika-ru-sonnet-v1 101, gaika-release-0563 4, serverwarden-livefire 4,
aes-canary 2; API/MCP → gaika-ru-sonnet-v1 28, owner-os-gaika-watchdog 18, serverwarden-livefire
2, aes-canary 2, gaika-release-0563 1, mess-telegram-connector 1; native goals → aes-canary 3.
Unaffected: agent_stop (confirm-gated) and agent_resume. payorch-ha-fresh: 0 events.
No writes: ledger schema_version 31 → 31; rotated audit log sha256 unchanged.

**Smallest scope preserving Owner OS control: `owner-os-supervisor-clean@api`.**

Owner approval needed for Stage 1 (exact): fast-forward `/root/ai-dev-runtime` to
`origin/ai-runtime/candidate-combined`, add `AGENT_AUTOMATION_POLICY=off` to the two
drop-ins and `configs/.env`, `systemctl daemon-reload`, restart `ai-runtime.service` and
`owner-os-wake-companion.service`. Stage 2 is a separate approval of the list above.
