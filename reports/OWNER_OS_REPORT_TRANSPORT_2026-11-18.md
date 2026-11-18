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
