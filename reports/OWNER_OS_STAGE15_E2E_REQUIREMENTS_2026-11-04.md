# Stage 15 live E2E requirements, 2026-11-04

An automated instruction was received through the Owner OS API. It is not owner approval.
**Live E2E: NOT PROVEN, not run. PROD: NO-GO. Nothing deployed.** No PROD, service, secret or live DB was changed. `control_plane.db` was not read: the earlier scan was refused and not worked around. No value of any credential or env variable was printed.

## A. The "sandbox token"
It is a per-client bearer token, not an external credential. Nothing issues it but the owner, on this host.
- Check: `api/v1.py` `client_for_bearer` compares sha256(token) with `{client_id: sha256}` in `RUNTIME_CLIENT_TOKENS_FILE` (default `/etc/ai-runtime/runtime-clients.json`). The file must be a regular file, root-owned, no group or other access; anything else means no clients.
- Scope: `api:client/<id>` must also be listed in `FULLCYCLE_REVIEWERS`. There is no default reviewer. Its only special power is ack and decision.
- State: `runtime-clients.json` **MISSING**. `FULLCYCLE_REVIEWERS` **MISSING** from the unit environments of `ai-runtime`, fullcycle worker and wake companion. No sandbox credential is provisioned.
- Needed: yes, for any real decision. The shared `RUNTIME_TOKEN` can never decide. Do not reuse it.

To provision, owner only, for the sandbox only: generate a random token on the host; write its sha256 to a sandbox-only 0600 file, not `/etc/ai-runtime`; run the sandbox API with `RUNTIME_CLIENT_TOKENS_FILE` at that file and `FULLCYCLE_REVIEWERS=api:client/sandbox-gpt`; give the raw token only to the sandbox MCP process through its env. It never passes through chat. Client tokens have no expiry or rotation, so delete the file after the run.

## B. The ChatGPT review path
Wake (Owner OS to ChatGPT): event, `core/wake_bridge` (needs `WAKE_BRIDGE_ENABLED`, no kill switch, cooldown clear, and the agent inside `WAKE_BRIDGE_ONLY_AGENTS` when that canary filter is set; the name is set on the companion, the value was not read), `tools/wake_companion` tick, `tools/cdp_composer` on CDP 9222 (the live shared browser), route `owner-os`, chat `6ae320ec-...`.
Return (ChatGPT to Owner OS): MCP tools in `/opt/seo` `backend/services/mcp_server.py`, `runtime_client.fullcycle_*`, `RUNTIME_URL`, `/api/v1/fullcycle/reviews/{id}/ack` and `/decision`.

**Blocker found, by code: the return path cannot work.** `backend/services/runtime_client.py` `_headers()` sends only the shared `RUNTIME_TOKEN` plus `X-Runtime-Actor`. `_reviewer_principal` refuses that with 403 whatever the actor says. So `fullcycle_review_ack` and `fullcycle_review_decision` fail today, on any target, even with a token provisioned. `fullcycle_reviews_list` works because reading needs only the shared token, so its `[]` proves nothing about decisions. It also reads the API behind `RUNTIME_URL`; by design that is PROD, not a sandbox (value not read).

Ack needs: client token, listed reviewer, an existing review; idempotent. Decision also needs: a decision valid for the review's kind and an `idempotency_key`, spent forever. It is recorded, and the engine applies it on its next tick. `next_tasks` get contract defaults (no push, no deploy).

### Live verification checklist
Pass only if no human touches any link. Record every id.
1. Sandbox API from this branch on `127.0.0.1:18080`, with its own `CONTROL_PLANE_DB`, `AGENT_CONTROL_DB`, `TMUX_TMPDIR`, throwaway repo, and own route table.
2. Sandbox wake route bound to a dedicated chat, not `owner-os`, so nothing lands in the tech lead's chat.
3. `CDP_PORT` at a dedicated profile, not 9222. Sandbox session named in the filter, if one applies.
4. One-task spec; real agent reaches `task_completed`; `fc_review` row appears.
5. Wake delivered: real user turn confirmed in the chat, not only sent.
6. ChatGPT calls list, ack, decision through the sandbox MCP; `fc_decision_audit` shows `api:client/sandbox-gpt`, auth `client_token`.
7. Shared token on the same endpoints gets 403 during the run.
8. Engine applies the decision; project reaches its end state; a replayed key is refused.

## C. What can run without a token
| level | what is real | status | counts as live? |
|---|---|---|---|
| Mock | `tests/test_autonomy_e2e_authenticated.py`: temp DBs, private tmux, real router; agent, ChatGPT page, reviewer and clock simulated | 1 passed | **no** |
| Local integration | new `tests/test_stage15_real_http.py`: real uvicorn on loopback, test tokens in tmp | 8 passed | **no** |
| Live sandbox | real agent, real ChatGPT, real MCP | **not run**; needs the gates below | yes, only this |

A real agent with a scripted reviewer is possible as a middle step, but it needs the agent approval and still does not touch the ChatGPT path.

## D. Done now
- Added `tests/test_stage15_real_http.py`. It closes the audits' open item: uvicorn really passes `raw_path`, `%3F` paths and queries are bound over a socket, a signature works once, the shared token and an unlisted client get 403 even when they name the reviewer, the listed client gets past auth.
- Focused runs only. The last full suite is `7559ed2`: 4707 passed, 0 failed.
- The MCP fix was NOT made: `/opt/seo`'s deployed line is that working branch, and it is baked into an image.

## Owner gates
1. Approve one sandbox Claude agent. `config/agent_creation.yaml` has `scopes: {}`, so it needs an owner proof.
2. Approve the reviewer path. Recommended: a separate sandbox MCP instance with `RUNTIME_URL` at the sandbox API. ChatGPT must be able to reach it, so check whether that needs new exposure, which is itself an owner decision.
3. Done on a branch, on the owner's word: see the update below. Merge and image build remain owner decisions.
4. Issue the sandbox token as in section A.
5. A dedicated ChatGPT chat and a logged-in browser profile on its own CDP port.

## Next step
Owner decides gates 1, 2, 4 and 5. Stop here until then. No PROD canary without separate owner approval.

## Update: seo branch
`feat/mcp-fullcycle-client-token` @ `414c4e5` in worktree `/opt/seo-fullcycle-token`, off `7039d6c`, requested by the owner directly. Local only, not merged, not built, not deployed. `/opt/seo` itself was not touched.
- `fullcycle_get` and `fullcycle_post` send `RUNTIME_FULLCYCLE_TOKEN` when set; unset keeps today's behaviour. Other calls keep the shared token.
- Real round trip with test tokens: seo client, uvicorn, API from this branch. Shared token: ack 403, list ok. Client token: ack and decision pass auth and reach the engine (`no such review 1`). So the 403 blocker is closed in code. Local integration, not live: no real review was decided.
