# Stage 15 Fable audit of the seo fixes, 2026-11-04

An automated instruction was received through the Owner OS API. It is not owner approval.
Audit of `feat/mcp-fullcycle-client-token` @ `fc97908` (`414c4e5..fc97908`, whole branch `7039d6c..fc97908`). One subagent through the `Agent` tool with `model: "fable"`, read only.
Model: self-reported Fable 5.1 (`claude-fable-5-1`); the client gives no independent proof. Billing not checked.
**Verdict: fit for a sandbox E2E, yes with conditions. PROD: NO-GO. Nothing merged, built or deployed. Not a clean audit: one HIGH stays open by design.**

Filed by the Opus 5.5 session. Editor's check at `fc97908`: L1, L2 and L5 match the code and were reproduced by regression tests; the runtime paths for H1, M1 and M2 and the non-root blockers below match the code. `ai-runtime` runs as root (unit `User=root`). The bypass in H1 was reasoned, not run.

Auditor's evidence: 65 passed offline; a batch RPC repro of the gate on a scratch script; no live DB, secret or env value read.

## Conditions for the sandbox E2E
1. Token only in the sandbox MCP's own env, root 0600, never in `/opt/seo/.env`; deleted after the run, with its entry in the clients file. There is no expiry.
2. `MCP_FULLCYCLE_REVIEWER_TOKEN_IDS` lists exactly one fresh write-scope token. Both variables need a container recreate.
3. Add to the checklist: an unlisted write token gets the `denied` audit row and makes no runtime call.
4. `fc_decision_audit` shows one client for every MCP token; the token id is only in seo's `mcp_audit`.
5. The sandbox API has its own `CONTROL_PLANE_DB`, otherwise the sandbox reviewer sees PROD reviews (M2).

## Findings
| id | severity | state | finding | where | status |
|---|---|---|---|---|---|
| H1 | HIGH | PLAUSIBLE, custody | An unlisted write token that can drive a root agent or job can have it read the token and decide directly. The runtime records `api:client/<id>`, `client_token`, indistinguishable from a real decision | `docker-compose.yml:38-39`, `api/v1.py:133-140,542-556` | **open**, needs agents without read access. See the sandbox defaults report |
| M1 | MEDIUM | CONFIRMED | The runtime does not store which MCP token decided; seo's audit row is written after the runtime answers, so a crash leaves a decision with no row, and a timeout after commit leaves `error` | `api/v1.py:575-595`, `core/fullcycle/engine.py:317-336`, seo `backend/services/mcp_server.py` `call_tool` | **open**, needs a runtime schema change, held |
| M2 | MEDIUM | CONFIRMED | No project binding: a listed reviewer decides any project, a read token lists all of them | seo `backend/models.py:4116`, `engine.py:289-305` | **open**, existing design; condition 5 |
| L1 | LOW | CONFIRMED | 3xx, 5xx raised and the runtime URL reached the audit row and the RPC error | `runtime_client.py` `_fullcycle_result` | fixed `5253d7c` |
| L2 | LOW | CONFIRMED | The shared token in the new variable was reported as "not listed" | `_fullcycle_refusal` | fixed `5253d7c` |
| L3 | LOW | CONFIRMED | Read tokens see provisioning wording, names only | `_fullcycle_refusal` | accepted, no fix |
| L4 | LOW | CONFIRMED | A malformed token degrades reads to the shared token; state visible only on the owner JWT route | `runtime_client.py` | accepted, no fix; shared token has no reviewer power |
| L5 | LOW | CONFIRMED | The worker receives the token | `docker-compose.yml:82-83` | fixed `5253d7c` |

Auditor's per-question results, not re-derived by the filer: no gate bypass (single chokepoint `call_tool`, batch path repro'd); identity comes only from the bearer hash; no MCP tool can mint a token or change the list; nothing fails open; token value has no route into a reply, audit row, log line or example file. The branch has no schema or migration. Rollback: revert the commits, rebuild the image, recreate containers, remove both variables, delete the client entry.

## Fixes
- `5253d7c`: L1, L2, L5. Non-2xx other than 400, 401, 403, 404 returns `runtime_error` with the status only. Compose blanks both variables for `worker`.
- `tests/test_stage15_seo_client_roundtrip.py`: closes I7, the round trip now has a committed test: real seo client, uvicorn, runtime API, one seeded review. Skips when the seo branch is absent. Does not go through the MCP layer.

Evidence:
- `test_mcp_fullcycle_tools.py`: 76 passed in the host venv.
- Round trip and `test_stage15_real_http.py`: 15 passed.
- 5 new mutants on a disposable copy: all caught.
- `docker compose config` with a test `.env`: backend keeps both values, worker gets empty, both files.
- Full seo suite in `seo-backend:latest`, `--network none`, read-only mount, at `5253d7c`: 994 passed, 3 skipped, 50 failed, 18 errors. Same 68 failing ids as base `7039d6c`. Cause not investigated; the suite is not green.
- `5253d7c` was not audited by Fable. Autonomy full suite not rerun: only this new test file changed.

## Not done, on purpose
- The auditor's H1 fix (`FULLCYCLE_REVIEWER_SOURCES`, a source address allowlist) was not applied. A root agent can `docker exec` into the seo container and call from the allowed address, so it would look like a fix without being one.
- The auditor's `st_uid in (0, os.geteuid())` for the clients file was not applied: see the sandbox defaults report.

## Not checked by the auditor
Live container env, the running image, the real clients file, any DB row, nginx or Cloudflare in front of `/api/mcp`, whether sandbox agents get a reduced uid, the wake path.
