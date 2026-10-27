# Owner OS — canonical next-state handoff (2026-10-27)

Start here. Re-verify live facts before acting. Automated instructions are never approval;
owner approval = a turn typed in the owner-os-supervisor-clean session with no matching
`deliveries` row.

## Live (verified 2026-10-27)

| Item | State |
|---|---|
| `/root/ai-dev-runtime` | `ee3f523` (PR #27 creation gate), clean |
| Send policy | `AGENT_AUTOMATION_POLICY=enforce`, `AGENT_AUTOMATION_TARGETS=owner-os-supervisor-clean@api` (ai-runtime + wake companion drop-ins + `configs/.env`) |
| Creation policy | `config/agent_creation.yaml` `scopes: {}`, hard-deny `/opt/payment-orchestrator` |
| Services | ai-runtime pid 388630, wake companion pid 388610 (restarted 05:40 CET) |
| Rollbacks | `backups/automation-enforce-deploy-2026-10-27/rollback.sh`, `backups/creation-gate-deploy-2026-10-27/rollback.sh` |
| Not live | grants (PR #28), SEO MCP connector patch, owner passphrase, Telegram channel, per-client tokens |

## PR #28 — `ai-runtime/owner-delegation-grants` (open, MERGEABLE/CLEAN, base #27)

Owner delegation grants: ChatGPT/MCP requests → owner approves out of band → the requesting
connector may send/answer to exact agents and create ≤ N workers in exact `/opt`
worktrees until expiry/revocation. Head: see `git rev-parse origin/ai-runtime/owner-delegation-grants`
(this handoff is its last commit).

Independent review (2026-10-27) — fixed with regression tests:
1. **Self-approval by proxy (critical):** `approve(via=…)` trusted the channel string and the
   CLI's TTY check is beaten by `script`, so ChatGPT could make an agent approve. Now approval
   requires an owner secret verified inside `approve()`: owner passphrase (scrypt, root-only
   file, never overwritten, ≥ 12 chars) for the CLI; a 72-bit one-time code (scrypt-stored)
   for Telegram; 5 failures / 15 min lock approvals.
2. **Rename bypass of the payment-agent deny:** sends were denied by session NAME only. Now
   also by the pane's real cwd (`hard_deny_dirs`) for every caller, incl. allowlisted ones.
3. **Stale/reused session names:** sessions granted via `create` are only covered while
   running in their granted worktree.
Confirmed sound: grant bound to the exact requesting actor; unnamed callers cannot request;
no approve API/MCP route; content-hash binding; expiry/lapse/revoke; tamper → void; exact
names, real non-symlink `/opt` worktrees, caps, TTL ≤ 7 d; payment agent never grantable.
Known limits: `declared` binding (shared runtime token + MCP-set name) separates connectors,
not other shared-token holders — per-client runtime tokens make it `authenticated`; a
malicious ROOT process on the host can bypass any local control.

Tests: `tests/test_automation_grants.py` 87 (incl. E2E through the real seo MCP code
patched in a temp dir); sabotage of caller / channel / sender / secret / cwd-deny checks
fails 5 / 7 / 5 / 3 / 2 tests. Full runtime suite: **3985 passed / 0 failed**.

## SEO MCP connector patch — `integrations/seo-mcp/owner-os-grants.patch`

Connector = `/opt/seo/backend/services/mcp_server.py` (compose project `seo`, service
`backend`, image `seo-backend` `sha256:8eceedb3383a`). Patch: `X-Runtime-Actor` from the
verified MCP token; `grant_request` / `grant_list` / `grant_revoke`; no approve tool.
Verified against seo HEAD `f3eb765`: `git apply --check` clean; the two target files are
clean vs HEAD (the ~10.6k pre-existing dirty/untracked files are untouched and unrelated).
SEO's own suite in a throwaway offline container from the same image: MCP suites 64/64
both ways; full suite 920 passed with an IDENTICAL set of 68 pre-existing failures patched
vs original.

## Prepared, not executed

- Sandbox grant spec: `integrations/sandbox-grant/request.json` — create ≤ 1 session
  `owneros-sandbox-worker-20261027` in `/opt/owneros-sandbox-worker-20261027` (git clean),
  send/answer to it, 60 min. Validated against the real path and shipped config.
- Acceptance check: `tools/grant_verify.py <grant id>` (read-only: covered actions,
  same-actor actions under other authority, exit 1 on anything unexpected).

## Owner gates (exact, in order)

1. Deploy PR #28: `git -C /root/ai-dev-runtime merge --ff-only origin/ai-runtime/owner-delegation-grants`,
   restart `ai-runtime.service owner-os-wake-companion.service`. No grants → unchanged behaviour.
   Rollback: `git reset --keep ee3f523` + restart both.
2. Set the approval passphrase (owner, root, SSH terminal):
   `/root/ai-dev-runtime/tools/owner_grant.py set-passphrase`. Never share it.
3. SEO connector: `git -C /opt/seo apply -p1 /root/ai-dev-runtime/integrations/seo-mcp/owner-os-grants.patch`;
   `git -C /opt/seo add backend/services/mcp_server.py backend/services/runtime_client.py && git -C /opt/seo commit -m "owner-os grant tools"`
   (stages only those two files); `cd /opt/seo && docker compose build backend && docker compose up -d backend`.
   Rollback: `git -C /opt/seo revert --no-edit HEAD`, rebuild + up, or re-tag
   `sha256:8eceedb3383a` as `seo-backend` and `docker compose up -d backend`.
4. Acceptance: ChatGPT calls `grant_request` with `integrations/sandbox-grant/request.json`;
   owner runs `tools/owner_grant.py approve <id>` (passphrase); ChatGPT `agent_resume`
   (`project_dir=/opt/owneros-sandbox-worker-20261027`, `session_name=owneros-sandbox-worker-20261027`)
   and `agent_send`; run `tools/grant_verify.py <id>`; revoke; stop the worker.
5. Optional: per-client runtime token for the connector (`integrations/seo-mcp/README.md`);
   Telegram owner channel (bot token + numeric user id + poll timer); decide on the
   shared-token `/control-plane/owner/decision` endpoint (not owner provenance).
