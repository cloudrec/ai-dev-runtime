# Stage 15 host compatibility and Fable audit packet, 2026-11-04

An automated instruction was received through the Owner OS API. It is not owner approval.
Written by the Opus 5.5 session. **Not an independent audit. Fable audit of this range: PENDING. PROD: NO-GO.**

## Verified evidence
- Branch `owneros/stage15-review-fixes`, clean tree. Range `05073f4ed9f0a476afb63c82a37bfeab02ef7f40..e96e72d713bfbe27a5cfe11cd03fbbea93ced183`: 14 files, +928/-67.
- Full suite at `e96e72d`: 4574 passed, 4 skipped, 0 failed, exit 0 (`reports/stage15/full_suite_e96e72d.txt`).

## Read-only host checks
Variable names only; no value was printed or stored. No live service, credential or DB was changed. `control_plane.db` was not opened.

| question | result | status |
|---|---|---|
| Daemon env against allowlist | `ai-runtime`, `owner-os-fullcycle-worker` and `owner-os-wake-companion` set no CLI-family or API variable. Allowlist drops nothing the CLI uses from them. `CLAUDE_CONFIG_DIR` unset, `HOME` and `PATH` set. `DISABLE_AUTOUPDATER` is not set, so R1 was not a live regression | answered for names; real `claude` not run |
| Settings overrides | none in `~/.claude/settings.json`, `settings.local.json`, or `.claude/` of `/root/ai-dev-runtime`, `/opt/owneros-autonomy`, `/opt/seo`; no managed settings. `~/.claude.json`: no `primaryApiKey`; `customApiKeyResponses.approved` has 1 entry, which needs a key in the environment to matter | answered for these paths; other project dirs not checked |
| Checks needing `ANTHROPIC_*` | worker env has none, so no check can receive one today. None in `config/`, `tools/`, `deploy/`, test fixtures | answered for env |
| Check naming the launcher (N2) | live specs are in `control_plane.db`, not opened. `/usr/local/bin/claude-owner-api` exists: root, 0700 | **unknown** |
| External HMAC clients | no client code in `/opt` or `/root` scan (`*.py,js,ts,sh,yaml,json`) outside this repo's tests and docs. Agent-control audit (uncompressed logs): 339 `api:bearer`, 0 `api:hmac`, last 2026-11-03 | **not proven absent**: audit covers agent-control actions only; off-host clients invisible |
| API workers | no `--workers`, no `WEB_CONCURRENCY`, no child processes: one process, so the replay cache is not split | answered |

## Migration on a disposable copy
File copy of `agent_control.db` in a 0700 temp dir with a throwaway key, deleted after the run. Live file size unchanged; no `.api_anchor` created beside it.
- Live DB has none of the new tables or `owner_proof_used`, so no proof has ever been spent there and nothing needs converting.
- Tables and 4 triggers created in 23 ms; integrity ok before and after; chain clean.
- Spend, replay refused, second spend, general proof spent once, replay refused: all correct.
- `DELETE` on the empty table reported no error, because row triggers do not fire on zero rows. Refusal with rows is covered by `test_r6_the_used_table_refuses_edits`, not by this copy.
- `control_plane.db` was not copied. `05073f4..e96e72d` adds no `CREATE` or `ALTER` under `core/fullcycle/`, so it has no schema migration in this range.

## Audit packet
**Scope:** `git diff 05073f4 e96e72d -- core api tools`, tests only to judge what they prove, `docs/OWNER_OS_HMAC_V2_MIGRATION.md`. Earlier reports: `reports/OWNER_OS_STAGE15_FABLE_AUDIT_2026-11-04.md`, `reports/OWNER_OS_STAGE15_FABLE_REVIEW_AUDIT_FIXES_2026-11-04.md`. Implementer claims, unverified: `reports/OWNER_OS_STAGE15_REVIEW_FIXES_HANDOFF_2026-11-04.md`.

**Threat model.** Assets: API spend, review decisions, the audit trail, live DBs. Actors:
- a confused or text-injected agent, and a script with DB access: must be stopped;
- a network client with a captured signature: must be stopped;
- a deliberate root process on the host: out of scope, only detected where stated in the `core/agent_auth_mode.py` docstring.

**Questions:**
1. `commit_api_launch`: can anchor, lock and commit ordering lose a spend, false-alarm, or deadlock against `creation_guard`?
2. `_anchor_problem`: does the one-row lag allow a usable truncation?
3. `ensure_proof_table` runs on every general proof: can it block or fail a live `agent_send`?
4. `_runs_claude` and `is_api_env_var`: any bypass, or a legitimate check broken?
5. `_signed_target` behind a proxy, and with a missing `raw_path`.
6. `check_argv` substring match: false refusals on legitimate paths?
7. `settings_api_overrides` at each launch: any failure that blocks a session? It has no exception guard beyond `OSError` and `ValueError`.
8. `_LiveGuardedPopen`: does it weaken or skip anything in acceptance?

**Known regressions in behaviour:** listed in the handoff table as "changed".

**Remaining risks:** handoff "Residual risks", plus the unknowns above.

## Fable route
A Fable subagent ran twice in this session (reports above), through this client's `Agent` tool with `model: "fable"` and `subagent_type: "general-purpose"`. Both self-reported Fable 5.1, the second as `claude-fable-5-1`, and the client exposes no independent proof of the model. Billing for those runs was not checked.
`tools/agent_create.py --model` cannot do it: `resolve` refuses Fable as `model_not_allowed`.
This time the instruction forbids subagents, so nothing was started. **Fable: PENDING.**

## Owner gates
1. Say to run the Fable audit of `05073f4..e96e72d` (one word in this session is enough); the `Agent` route above.
2. Sandbox token and live E2E setup.
3. Real `claude` smoke with the allowlist, as part of a canary.
4. Whether any live spec names the launcher: needs a read of `control_plane.db`, or a copy of it.
5. `RUNTIME_HMAC_LEGACY=warn` in PROD to find off-host clients. Paid tests and deploys.
