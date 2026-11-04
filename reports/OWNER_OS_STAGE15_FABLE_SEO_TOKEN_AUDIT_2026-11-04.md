# Stage 15 Fable audit of the seo client token branch, 2026-11-04

Audit of `7039d6c..414c4e5` on `feat/mcp-fullcycle-client-token` (`/opt/seo-fullcycle-token`), requested by the owner in this session. Subagent through the `Agent` tool with `model: "fable"`.
Model: self-reported Fable 5.1 (`claude-fable-5-1`); the client gives no independent proof. Billing not checked.
**Verdict: safe to merge to a branch, yes. Do not set `RUNTIME_FULLCYCLE_TOKEN` on any instance until H1 and M1 are decided. PROD: NO-GO. Nothing merged, built or deployed.**

Filed by the Opus 5.5 session. Editor's check, code read at `414c4e5`: M1 gate, M2 server path, L2, L3, L4 and I6 match the code. H1 is a custody finding: the compose `env_file` lines match, live uid of agents was not checked. L1's h11 behaviour was not reproduced by the filer. The auditor cites `mcp_server.py`; the file is `backend/services/mcp_server.py`.

Auditor's evidence: read-only, `tests/test_mcp_fullcycle_tools.py` offline, 32 passed. Filer's tree check: seo worktree clean after the run.

## Findings
**H1 (PLAUSIBLE, custody): the reviewer secret sits where the reviewed agent can read it.** The token comes from `/opt/seo/.env` through `env_file`, also passed to the `worker` service, which never needs it. Reviewed agents run in tmux on the same host, and if root they can read the file or `docker inspect` the container, then POST a decision directly: recorded as `api:client/<id>`, `client_token`. Before this branch no credential accepted by `_reviewer_principal` lived on that path. Fix is a deployment rule: set it only where reviewed agents cannot read it (non-root sandbox agent), scope it to `backend`, rotate after a sandbox run.

**M1 (CONFIRMED): every write-scope MCP token becomes the reviewer.** `backend/services/mcp_server.py:789,792,1053`: the only gate is the generic `write` scope, shared with `agent_send`, `approve_task` and others. The runtime ignores `X-Runtime-Actor` on these calls, so it records one client for all of them; which MCP token decided is only in seo's `McpAudit`. Fix: an env allowlist of reviewer token ids, fail closed when unset. Runtime side: store the declared actor beside the decision.

**M2 (CONFIRMED): a token the runtime does not accept breaks reads too.** `api/v1.py:133-142` gives 401 for an unknown bearer, and `_client_tokens` returns `{}` for a missing or unsafe file. So a stale token or a permission slip takes down `fullcycle_reviews_list`, which worked on the shared token. Fail closed, keep it. Fix: separate `detail` for 401 and 403. A valid client token outside `FULLCYCLE_REVIEWERS` still reads.

**L1 (PLAUSIBLE): a malformed value is echoed.** `.strip()` only; an interior newline reaches h11, whose error carries the token into `McpAudit.detail` and the RPC error. Same hazard exists for `RUNTIME_TOKEN`. Fix: printable ASCII check at load, or catch the error.

**L2 (CONFIRMED): silent fallback when empty.** Nothing reports it; the first failed ack looks the same as a client not on the reviewer list. Fix: a boolean in `status()` and a clearer 403 detail.

**L3 (CONFIRMED): variable not documented.** Not in any example env or compose block. Setting it needs a container recreate, and the code needs an image rebuild.

**L4 (CONFIRMED): test 3 is close to vacuous.** `test_the_client_token_goes_only_to_full_cycle_calls` sends nothing, so moving another call to `_fullcycle_headers()` would not fail it. Fix: send `agent_get` and `status` through the `wire` fixture.

**I6 (CONFIRMED): the round trip claim has no committed test.** `tests/test_stage15_real_http.py` does not use `runtime_client`. The run in the requirements report was a manual one and cannot be replayed from the repo.

Other INFO: no redirects or retries in httpx 0.27.0; `trust_env=True`, so a proxy variable in the container would see the token, as it does the shared one; `/api/mcp` is reachable through nginx with a bearer and no IP allowlist (existing).

## Per question
1. Override scope: exactly `fullcycle_get` and `fullcycle_post`. Pass with L1.
2. Header construction: one `Authorization`, no HMAC, client file checked before the shared token. Pass.
3. Empty and malformed values: L1, L2.
4. Read paths: M2.
5. Import time env: matches `RUNTIME_TOKEN`; L3.
6. Authority: nothing widens while unset; once set, H1 and M1.
7. Tests: 1, 2 and 4 real, 3 not (L4).

## Not checked by the auditor
Live uid of agents, container env, `/opt/seo` dirty tree, runtime unit `User=`, the real client tokens file, any DB. Compose parser delivery of an interior newline. Mutation runs. Wider seo suite.

## Fixes at `fc97908` (Opus 5.5 session, not audited by Fable)
Owner said to fix M1, M2 and L1-L4 on the branch. H1 stays a deployment rule. Not merged, built or deployed.

| finding | change |
|---|---|
| M1 | `call_tool` refuses ack and decision unless the MCP token id is in `MCP_FULLCYCLE_REVIEWER_TOKEN_IDS`; unset refuses all, audited as `denied`. The runtime side (declared actor beside the decision) is not done |
| M2 | 401 and 403 name the side to fix; reads stay fail closed |
| L1 | a malformed token is dropped at load, the value is never logged |
| L2 | `status()` gives `fullcycle_client_token`: `set`, `unset` or `malformed` |
| L3 | both variables in the three example env files |
| L4 | the test now sends `status` and `agent_get` and checks the bearer on the wire |

Evidence:
- `test_mcp_fullcycle_tools.py`: 65 passed, in the host venv and in `seo-backend:latest`.
- 13 mutants on a disposable copy outside both repos: all caught.
- Real loopback run (seo client, uvicorn, runtime API from this branch, test tokens): shared ack 403 "not set"; wrong token list 401 "did not accept"; unlisted client list ok, ack 403 "not listed"; listed client ack and decision reach the engine (`no such review 1`). Manual script, still no committed test (I6). The MCP gate was not in that run; it is covered by unit tests.
- Full seo suite in `seo-backend:latest`, `--network none`, read-only mount, no env file: branch 985 passed, 50 failed, 18 errors, 1 skipped; base `7039d6c` 948 passed, 50 failed, 18 errors, 1 skipped. The 68 failing test ids are identical on both. Delta +37 is the 37 new tests. The cause of the 68 was not investigated, so that suite is not green; this shows only that the branch added no failure in these conditions.
