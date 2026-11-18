# agent_report read returns `{}` in ChatGPT: findings and proposed fix (DEV, not implemented)

Trigger: an automated instruction was received. The tech lead observed `Object.keys(result) = []` for `agent_report` with `path=docs/LEAD_REVIEW_HANDOFF.md`, while listing works.

## Facts
- **Runtime** `GET /agents/report?path=…` returns `{project_dir, path, truncated, content: <str>}`. Read directly: 15 524 chars.
- **seo MCP** `_t_agent_report` returns the same dict. Replayed in the live backend container: `content` present, 15.5–16.6 KB, 0 redaction markers.
- **MCP audit:** all of the tech lead's read calls are `ok` and carry `path`.
- **Envelope** (`mcp_server.py:1480`, all tools):
  - `{"content":[{"type":"text","text": json.dumps(result)}], "isError": false}`;
  - no `structuredContent`, no `outputSchema`;
  - identical for list and read, with no size cap in `routes_mcp.py`.

## Cause (most likely; ChatGPT client not observable from here)
The read payload is the only tool result in the server whose **top-level key is `content` holding a string**. A client that parses the JSON text and treats an object with `content` as an MCP result would find `content` not to be an item array, and would drop it to `{}`. The list payload (`reports`, `count`) has no such key, which is why it works.

Size is a weaker alternative: 16 KB is small, and other tools return larger JSON. Ruling it out needs the comparison below.

## Minimal compatible fix (seo repo, needs an image rebuild and deploy, later)
In `_t_agent_report`, when `path` is given:
- return `{project_dir, path, truncated, text: <str>, bytes: n}`, with no top-level `content` key;
- keep `content` only on the REST/runtime side, unchanged.

Optionally, the tools/call envelope can carry the report text as a second `{"type":"text"}` item and add `structuredContent: result` for every tool. Both are additive, so existing clients keep working.

## Regression tests to add (seo `backend/tests/`, synthetic, non-secret fixtures)
1. `_t_agent_report` with `path` and a patched `runtime_client.agent_get` returning `{"content": "x"*N}` for N = 10, 16 000 and 300 000. The result has `text` of length N, and **no top-level `content`**.
2. tools/call envelope:
   - `result.content[0].type == "text"`;
   - `json.loads(text)` has `text`;
   - with the structuredContent option, `result.structuredContent == payload`.
3. Guard over the whole tool table: no tool payload may have a top-level `content` key that is not a list of items (fails today for agent_report read).
4. The list path is unchanged (`reports`, `count`).

## Next
Implement in an seo DEV branch, run its suite, then rebuild and deploy on a word. Until then, the tech lead gets report text by asking the agent.

## Implemented in DEV: seo 1065b32 (not built, not deployed)
Trigger: an automated instruction was received. Branch `owneros/report-transport-20261118` in worktree `/opt/seo-report-transport`, base 39603fb (the live seo line). Not pushed.

**Change** (`backend/services/mcp_server.py`, report-specific; the tools/call envelope is not touched):
- `agent_report` with `path` returns `{project_dir, path, truncated, text, chars}`. There is no top-level `content` key.
- Unchanged:
  - the listing (no `path`), which passes through as before;
  - runtime refusals and errors, which pass through untouched;
  - `project_dir required`;
  - redaction and bounding, which stay in the runtime (`agent_report_read`); the text is not altered.
- The tool description now says the text is in `text`. This changes the catalog fingerprint, so clients get `listChanged`.
- Rejected: global `structuredContent` or a second text item. Not needed for this defect.

**Verification of the collision** (synthetic; the real ChatGPT client is not observable):
- A strict stand-in client drops an object with a non-list `content` to `{}`.
- On the released shape (15 524 chars), the text is present in the envelope, but the client keeps `{}`.
- The listing shape survives.
- This shows the mechanism, not ChatGPT's code. A live read after deploy is the real proof.

**Tests:** new `tests/test_mcp_agent_report_transport.py`, 12 tests.
- Text sizes through the real `dispatch_rpc` envelope, serialized to the wire: short ASCII, short Unicode, 16 000-char Unicode and 300 000-char Unicode with emoji.
- Truncation and redaction marker pass-through, listing unchanged, refusal untouched, missing `project_dir`, odd shapes, description.
- On base 39603fb: 7 fail, 5 pass. The 5 are the defect demo and the unchanged behaviours.
- Targeted MCP suites: 76 passed.
- Full seo suite in a throwaway `seo-backend` container with no network:
  - fix: 1074 passed, 50 failed, 18 errors;
  - base: 1062 passed, 50 failed, 18 errors;
  - **the failure set is identical** (sqlite tables missing in this runner, e.g. `editorial_pass`). No new failure.
- Evidence: `reports/loop/report_transport_2026-11-18/{seo.diff, results.txt}`.

**Release, when authorised:** merge 1065b32 onto the deployed seo line, rebuild the backend image, then restart seo-backend. Rollback is the previous image. After that, one live `agent_report` read from the tech lead should show `text`.

## Released 1065b32 (owner's direct word "деплой 1065b32")
- Installer `deploy/seo_report_text/install.sh`: `--check` clean (only mcp_server.py and the new test), then `--apply`. Deployed seo branch fast-forwarded 39603fb → 1065b32, backend image rebuilt, backend healthy, post-check "report text payload installed".
- Rollback: `deploy/seo_report_text/rollback.sh` (image `seo-backend:rollback-pre-report-text`).
- Live proof pending: one `agent_report` read with `path` from the tech lead.

## ServerWarden chat switched (owner's direct word, 16:39:50Z)
Route `hostsecure` rebound from 6afbff8d… to https://chatgpt.com/c/6afd9cb3-41ec-83ed-91ae-d64c941a14bc (`rebind_chat.py --by owner`, verified). Backup: `/root/ai-dev-runtime/.ai-runtime-backups/wake_target/wake_target_20261118T163942Z.sql`. Before the switch, the old chat received and answered every wake (10 in 2 h); the new chat had no route, which is why it stayed silent.
