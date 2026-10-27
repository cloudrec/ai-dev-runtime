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
