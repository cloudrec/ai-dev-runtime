# Owner delegation grants — 2026-10-27

Branch `ai-runtime/owner-delegation-grants` (from live `ee3f523`). Not deployed. Requested by
an automated instruction; nothing here grants authorization.

## Problem

Live is fail-closed (`AGENT_AUTOMATION_TARGETS=owner-os-supervisor-clean@api`, creation
`scopes: {}`), so ChatGPT/MCP can only talk to the controller. Owner approval today means
typing into this tmux pane for each change.

## Finding: the existing "owner decision" API is not owner provenance

`POST /api/v1/control-plane/owner/decision` stores `actor="owner:<method>"`,
`source_channel="owner_api"`, `authenticated=True` for ANY caller holding the shared runtime
token — ChatGPT/MCP included. It therefore cannot prove an owner decision and is NOT used
here. Changing it alters live gate resolution → owner decision (left unchanged).
`/jobs/{id}/approve` is the same (shared token). No inbound owner channel existed; the
Telegram keys in `configs/.env` are empty.

## Design (implemented)

`core/automation_grants.py` — a grant = project + exact agent sessions + actions
(`send`, `answer`, `create`) + optional create spec (explicit `/opt` worktrees, session
prefix, `max_sessions` 1..10) + TTL (≤ 7 days). Stored with a content hash.

| Step | Who | How |
|---|---|---|
| request | ChatGPT/MCP | `POST /api/v1/grants/request` → `pending`; validated: exact names only (no `*`), never the payment agent, real non-symlink `/opt` worktrees, not hard-denied dirs |
| approve | **owner only** | (a) host shell over SSH: `tools/owner_grant.py approve <id>` — root + interactive TTY, shows the SERVER copy + content hash, owner retypes the id; or (b) Telegram from the owner's user id: `/approve <id> <code>` replying to the server-sent summary (one-time code, private chat, no forwards/edits/bots) |
| use | ChatGPT/MCP (caller `api`) | existing MCP send/answer/resume tools work unchanged on the granted agents; every action audited with `authorized_by=grant:<id>` |
| revoke | anyone (narrowing) | `POST /api/v1/grants/{id}/revoke`, `tools/owner_grant.py revoke`, Telegram `/revoke` |
| expire | automatic | `expires_at`; pending requests lapse after 15 min |

There is no API route that approves. A request body saying "да"/"owner approved", an
`X-Runtime-Actor: owner` header, or a channel name other than the two owner channels
activates nothing.

Enforcement: `agent_control.automation_decision` (send/answer): owner proof → payment
hard deny → policy off → allowlist → **active grant** → refuse; bare keys (`control_keys`)
are never covered. Creation gate: static scope → **active grant** (exact worktree, name
`<prefix><key>`, one live per worktree incl. reservations, `max_sessions`) → refuse.
Grants apply only to caller class `api`; in-process senders (native supervisor, CTO
bridge, recovery) get nothing from them. A tampered stored grant (hash mismatch) is void.

## Tests

`tests/test_automation_grants.py` (55): pending allows nothing; no channel name but the two
owner channels approves; API exposes request/list/revoke only; owner text/actor in the
request changes nothing; CLI refuses non-TTY and wrong retype, shows the server copy;
granted worker created once, messaged by `api` with `grant:<id>` in the audit; all other
agents and the payment agent denied; non-api callers denied; bare keys not covered; wrong
worktree / name refused; expiry, revocation, lapse, tampering, changed content refused;
spec validation (wildcards, payment agent, non-/opt, symlink, caps, keys, TTL); Telegram:
server summary + code to the owner chat, approval, replay, other user / group / forwarded /
edited / bot / wrong or missing code ignored, revoke, disabled without config, token never
leaked. Sabotage: removing the caller check, the channel check or the Telegram sender
check fails 5 / 7 / 5 tests. Full suite: 3953 passed / 0 failed.

## Limitations

- Caller class `api` = every holder of the runtime token (ChatGPT/MCP, any API client);
  a grant cannot tell them apart without per-caller tokens.
- A malicious ROOT process on this host could write the grant table or drive a pty; like
  every local control, this defends against remote/API callers and injected text.
- The MCP server (outside this repo) needs the three new routes exposed as tools to let
  ChatGPT request/list/revoke; until then ChatGPT can call the HTTP API directly.
- Telegram polling (`tools/owner_grant.py telegram-poll-once`) is not wired to a timer.

## How the owner authorizes once (after deploy)

1. ChatGPT: `request_delegation_grant` e.g. `{"project":"owneros-sandbox","actions":
   ["send","answer","create"],"create":{"worktrees":{"w1":"/opt/owneros-sandbox-worker-20261027"},
   "session_prefix":"owneros-sandbox-","max_sessions":1},"ttl_secs":3600}` → id `g…`.
2. Owner, over SSH on the host: `/root/ai-dev-runtime/tools/owner_grant.py approve g…`,
   read the summary, retype the id. (Or, once configured, tap the Telegram `/approve`.)
3. ChatGPT creates and drives that one worker until expiry; `revoke` any time.

## Minimal rollout (owner-gated)

1. Deploy this branch (fast-forward from `ee3f523`; restart ai-runtime + wake companion).
   No config change: with no grants, behaviour is identical to today.
2. First grant: the single `/opt/owneros-sandbox-worker-20261027` worker, `max_sessions: 1`,
   1 h. Live supervisors stay blocked from it by the existing send allowlist.
3. Optional: owner creates a Telegram bot (free) and sets `OWNER_GRANT_TELEGRAM_BOT_TOKEN`
   and `OWNER_GRANT_TELEGRAM_USER_ID`, plus a timer for `telegram-poll-once`.
4. Rollback: `git reset --keep ee3f523`, restart both; or revoke all grants.


## Follow-up: working ChatGPT path + client binding (same branch)

Requested by an automated instruction; nothing deployed, `/opt/seo` untouched (`git apply
--check` only).

**Where the connector lives.** The ChatGPT "owner-os" MCP connector is the SEO project's
backend: `/opt/seo/backend/services/mcp_server.py` (container `seo-backend-1` at
172.20.0.4 → runtime `http://172.17.0.1:8199/api/v1`). Tools are a hand-written `TOOLS`
registry + `_FULL_SCHEMAS`; each connector authenticates with its own hashed `mcp_…`
token (`McpToken`, scopes read/write). The connector called the runtime with the shared
bearer only and no `X-Runtime-Actor`, so every MCP call arrived as plain `api:bearer`.

**Patch for the connector** (`integrations/seo-mcp/owner-os-grants.patch`, base seo
`f3eb765`, applies cleanly): `call_tool` sets `runtime_client.CURRENT_ACTOR =
mcp-token-<id>` from the verified token (reset after the call) and `runtime_client` sends it
as `X-Runtime-Actor`; new tools `grant_request` (write), `grant_list` (read),
`grant_revoke` (write). No approve tool.

**Binding (runtime).** A grant belongs to the exact requesting actor and only that actor
can use it (send/answer/create). Unnamed callers (`api:bearer` with no name) cannot request.
Bindings are labelled honestly:
- `declared` — shared runtime token + a name set by its holder. The MCP server names the
  verified MCP token, so one connector cannot use another connector's grant; any other
  holder of the shared runtime token could still declare the same name.
- `authenticated` — optional per-client runtime token (`/etc/ai-runtime/runtime-clients.json`,
  root 0600, `{client_id: sha256(token)}`): `_auth` resolves the client, the actor becomes
  `api:client/<id>` and declared names are ignored; once that file exists, only
  authenticated clients may request grants (`GRANT_REQUIRE_AUTHENTICATED_CLIENT` overrides).

**E2E tests** (`tests/test_automation_grants.py::test_mcp_*`): the real seo `mcp_server.py`
and `runtime_client.py` copied to a temp dir, patched, DB models stubbed, HTTP routed by
ASGI into this branch's real runtime API (real `_auth`, routes, agent_control on mocked
tmux): MCP token 7 requests → pending, nothing allowed → owner CLI approves → token 7
creates exactly one worker in the granted /opt worktree and sends (audited
`grant:<id>`) → token 8, other agents and payorch refused → list → revoke → refused; actor
taken from the verified token, not params; read-only tokens cannot request. Binding tests:
other actors, unidentified requesters, authenticated client overrides declared names,
shared token cannot impersonate `api:client/…`, unsafe client-token files ignored.
Full suite: 3972 passed / 0 failed.

### Remaining owner gates (exact)

1. Deploy this branch to the runtime (fast-forward from `ee3f523`; restart ai-runtime +
   wake companion). With no grants, behaviour is unchanged.
2. Apply `integrations/seo-mcp/owner-os-grants.patch` in `/opt/seo`, commit on its deployed
   branch, rebuild and restart `seo-backend` (SEO project; baked image).
3. Optional, recommended: per-client runtime token for the connector (file above + the seo
   backend's `RUNTIME_TOKEN`) to turn `declared` into `authenticated`.
4. First grant: ChatGPT calls `grant_request` for the single `/opt/owneros-sandbox-worker-20261027`
   worktree (`max_sessions: 1`, 1 h); owner runs `tools/owner_grant.py approve <id>` over SSH.
