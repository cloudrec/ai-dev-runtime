# Stage 15 deploy plan, 2026-11-06

**Prepared, not run. PROD: NO-GO until the owner runs `--apply`.** `AGENT_RUN_AS` stays off: live agents keep running as they do today.

Moves: Owner OS `9557004` to `319012a` (118 commits, 45 files under core, api, tools, hooks, config), seo `7039d6c` to `5253d7c` (8 files). Scripts: `deploy/stage15/install.sh`, `rollback.sh`.

## Steps, owner runs each
1. `bash deploy/stage15/install.sh --tokens`: note the id of the live ChatGPT connector's token.
2. `bash deploy/stage15/install.sh --dry-run`: applies the migration to copies of the live DBs and counts pending reviews.
3. `bash deploy/stage15/install.sh --apply --reviewer-token-id N`

`--check` passed from this session. `--tokens` was refused to this session as a production read, and `--dry-run` was not tried, because an earlier read of `control_plane.db` was refused. Neither was worked around, so neither has been run yet. `--apply` and `rollback.sh` are syntax-checked only.

## What --apply does, in order
1. Refuses unless refs, trees, services and settings match.
2. Backs up 4 SQLite DBs and `/opt/seo/.env`; tags `seo-backend:rollback-pre-stage15`.
3. Creates the client token: its hash in `/etc/ai-runtime/runtime-clients.json`, the raw value in `/opt/seo/.env`. Never printed.
4. Adds `FULLCYCLE_REVIEWERS=api:client/chatgpt-tech-lead` as an `ai-runtime` drop-in.
5. Fast-forwards live. Restarts `ai-runtime`, `owner-os-fullcycle-worker`, `owner-os-wake-companion`.
6. Fast-forwards seo, builds and recreates `backend` only.
7. Verifies: shared token gets 403, MCP client token passes auth, units active, tmux count printed.

## Order matters
Live accepts decisions by actor name. The new code refuses the shared token. So between steps 5 and 6 of `--apply` reviews get 403 and stay pending; nothing is lost. Doing only one side leaves reviews refused.

## What changes on live
- Reviews: need the client token. They now carry the git diff. Decision and audit are one transaction.
- New tables and triggers: additive, created on first use. No data rewritten.
- Wake queue: an unrouted event no longer blocks the routes behind it.
- Spawned `claude` gets a stricter environment. Earlier host check: no live unit needs a dropped variable. Real `claude` under this code ran in the sandbox, not on live.
- `recovery_command` refuses any shape that is not plain `claude`. `config/managed_sessions.yaml` shapes were all plain when checked.
- HMAC: legacy still accepted by default.
- Off by default, unchanged: API auth mode, strict base ref, planner replan, settings guard, `AGENT_RUN_AS`.

## Risks
- Restart of `ai-runtime`: the tmux server was moved out of its cgroup on 2026-10-07, so panes should survive. Compare the printed count: 10 now.
- Companion restart drops an in-flight wake delivery, as in earlier deploys.
- The backend image is rebuilt from `/opt/seo`. `backend/` is clean against HEAD, checked. Backend is briefly down during the swap.
- Whether a live full-cycle spec names the launcher (N2 refusal) is unknown: step 2 does not test it.
- Full suite passed at this code in a worktree, not with live config.

## Rollback
`bash deploy/stage15/rollback.sh <backup dir>`, printed by `--apply`: code, drop-in, token file, seo tree, `.env`, image. DBs stay: new tables are harmless and a restore would lose events. Backup copies stay for a manual restore.

## Afterwards
- Send one small review through the live chat to prove the path.
- `AGENT_RUN_AS` on live is a separate decision: it needs a user per instance, `AGENT_CONTROL_DB` in every unit, and `project_supervisor` off.

## Result, 2026-11-06
Applied by the owner at 21:23Z with `--apply --reviewer-token-id 3`. Live `319012a`, seo `5253d7c`. After apply: three units active with 0 restarts, no error-level journal lines, shared token 403 on ack, 10 tmux sessions as before, `seo-backend-1` healthy with 0 restarts.
Backup and rollback: `/root/ai-dev-runtime/backups/stage15-deploy-20261106T212256Z`.
**Not proven: a real review decided through the live chat after deploy.** `AGENT_RUN_AS` stays off on live.

## Live proof, 2026-11-07
Project `owneros-taskboard-test`, task `version`: agent wrote `app/version.py`, checks passed, review 2 went to the bound chat, ChatGPT approved it, the engine applied the decision and committed `d22397c`. The audit row was not read by the session.
Delay: the ChatGPT session on the live browser expired around 19:41 CET on 2026-11-06, before the deploy; cause not determined. Every wake failed for about 11.5 hours with no alert to the owner. After sign-in at 07:04 CET the queue drained by itself.
**Open: no alert for an expired ChatGPT session.**
