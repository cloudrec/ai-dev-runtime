# Owner OS MCP connector — grant tools patch (not applied)

The ChatGPT "owner-os" MCP connector is served by the SEO project's backend
(`/opt/seo/backend/services/mcp_server.py`, container `seo-backend-1`, network
`seo_traffic_os` → runtime `http://172.17.0.1:8199/api/v1`). It is a different project, so
this repo ships the change as a patch instead of editing it.

`owner-os-grants.patch` (base: seo `f3eb765`, verified with `git apply --check`):
- `runtime_client.CURRENT_ACTOR` + `X-Runtime-Actor` on every runtime call, set by
  `call_tool` from the VERIFIED `McpToken` id (`mcp-token-<id>`), never from tool params —
  the runtime binds grants to that connector (binding `declared`);
- tools `grant_request` (write), `grant_list` (read), `grant_revoke` (write) with schemas.
  There is deliberately no approve tool.

Apply (owner-gated, SEO project): `git -C /opt/seo apply -p1 <this patch>`, commit on the
deployed seo branch, rebuild + restart the `seo-backend` image (the backend image is baked).
Exercised end to end by `tests/test_automation_grants.py::test_mcp_*` against the real seo
files copied into a temp dir (skipped when `/opt/seo` is absent or the patch no longer
applies).

Stronger identity (optional): give the connector its own runtime token — add
`{"<client-id>": "<sha256 of token>"}` to `/etc/ai-runtime/runtime-clients.json` (root,
0600) and set the seo backend's `RUNTIME_TOKEN` to that token. The runtime then
authenticates the caller as `api:client/<client-id>` (binding `authenticated`), ignores
declared names, and requires authenticated clients for all new grants.

## `tools-list-changed.patch` (base seo `af712d2`, NOT applied)

Why ChatGPT kept the old catalog after `owner-os-grants.patch` was deployed (verified
2026-10-27): the backend served 46 tools (incl. grant_*), but `initialize` advertised
`tools.listChanged: false`, and the connector's GET server stream (`/api/mcp/c/{ptok}`)
only emits custom `event: ready` / `event: change` SSE events — the server could never tell
a client its tools changed. ChatGPT (token id 3) opened 5 GET streams and initialized new
sessions after the deploy and still showed the old catalog, i.e. it caches the catalog
beyond a session.

Fix: `listChanged: true`; `tools/list` records the catalog fingerprint served per token in a
small file in the container's temp dir (`MCP_LISTED_CATALOG_FILE`), shared by all gunicorn
workers (prod runs 2); a new GET server stream sends the JSON-RPC
`notifications/tools/list_changed` (SSE `event: message`) when that token's last-listed
catalog differs from the current one — after every deploy (new container), the first stream
announces it once. Existing `ready`/`change` events unchanged; conforming MCP clients only
parse `message` events, so those stay ignored.

Temp-file hardening (CTO review): the record is read with `O_NOFOLLOW`; updates take an
`flock` on a no-follow `0600` lock file, write a `mkstemp` file (`O_EXCL`, `0600`, random
name) in the same directory and `os.replace` it over the record (a rename replaces a planted
symlink instead of following it). Any refusal only costs one extra notification.

Tests (`backend/tests/test_mcp_list_changed.py`, in the patch): 11 — incl. planted symlink at
the record path (not followed on read or write, replaced), planted symlink lock file
(refused), 0600 modes and no leaked temp files, 4 concurrent processes × 20 tokens (none
lost). MCP suites 75/75; full seo suite in an offline throwaway container 931 passed
(920 + 11) with the identical set of 68 pre-existing failures. Sabotage: read without
O_NOFOLLOW, direct open() write, no flock each fail tests. Rollback = the same procedure as
`owner-os-grants.patch` (revert the commit, retag the pre-deploy image,
`up -d --no-build backend`).

Unverified: whether ChatGPT acts on `notifications/tools/list_changed`. If it does not, only
the user-side connector refresh helps.
