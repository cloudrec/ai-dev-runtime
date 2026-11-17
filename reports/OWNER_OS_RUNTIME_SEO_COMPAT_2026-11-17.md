# Runtime + SEO compatibility and rollback, 2026-11-17

Candidates:
- **Runtime:** `owneros/autonomy-loop-20261117` @ d052832.
- **SEO:** `owneros/mcp-send-queue-20261117` @ ae60962, branched from the live seo commit 6114af8.

Live now:
- **Runtime:** 3423f4d, plus the live vtb deny edit in `config/agent_creation.yaml` and the companion drop-in.
- **SEO:** the image built from 6114af8.
- **Send queue:** `SEND_QUEUE_ENABLED` is unset in `configs/.env` and in the ai-runtime drop-ins, so the queue is off.

## Matrix
| Runtime | SEO MCP | `agent_send` (old call) | `agent_send` + `queue_if_busy` | queue tools (status/cancel/reconcile) |
|---|---|---|---|---|
| live 3423f4d | live 6114af8 | works as today | not offered (schema closed) | not offered |
| live 3423f4d | **ae60962** | works: payload byte-identical | old pydantic model ignores unknown fields (checked: `AgentSendReq(..., queue_if_busy=True).model_dump()` drops them), so it is a plain send: a busy agent answers `refused: working`. Never `queued:true`, so nothing is falsely claimed. | 404 from runtime → error to caller |
| **d052832**, queue off | live 6114af8 | works as today | not offered | not offered |
| **d052832**, queue off | **ae60962** | works as today | `refused: send_queue_disabled` (E2E step 1) | status `unknown` key; cancel no-op |
| **d052832**, `SEND_QUEUE_ENABLED=1` | **ae60962** | works as today | `queued:true, delivered:false` → delivered once when idle (E2E) | as designed |

Safe order: **runtime first** (queue still off), then the **SEO image**, then turn the queue on (**SEND_QUEUE_ENABLED=1** plus an ai-runtime restart). Each step can be undone on its own.

## Rollback
| Step | Undo |
|---|---|
| Runtime apply | `deploy/adaptive_offer/rollback.sh <backup>`. It restores the previous SHA, `configs/.env` and the companion drop-in, and re-applies the live vtb deny (it refuses to finish without it). |
| Queue on | Remove `SEND_QUEUE_ENABLED=1` and restart ai-runtime. Queued rows stay in the DB, undelivered, and expire; `status` reports them as expired. Rows in `unknown` do not expire: they stay `unknown`, and loop_watch flags them after 30 min. |
| SEO image | Re-tag the previous image (`seo-backend:rollback-*` pattern, as in earlier deploys) and recreate the backend container. |

Payment deny is identical in every cell. It is enforced in the runtime at enqueue, at delivery and at reconcile; SEO only forwards. One exception: if the target pane is gone, `reconcile` outcomes `delivered` or `abandon` skip the authority check. They only change a receipt; nothing is sent.

## Evidence (runtime d052832 / SEO ae60962)
### SEO suite (disposable PostgreSQL)
- PostgreSQL 16 on an `--internal` docker network, synthetic data, schema from `init_db` (alembic from an empty DB fails on base and candidate alike), no route to any live DB.
- Base 6114af8: 48 failed / 1059 passed. Candidate: 48 failed / 1069 passed.
- The failure lists are identical. All 48 are outside MCP: social phase 8*, content intake, editorial, paid growth, rehearsal, niche radar, source factory.
- The 18 errors seen in the offline container are gone with Postgres.

### SEO focused tests
- Suites: MCP, auth, ownership, owner page, release page, security; the full repo is mounted so the compose-dependent tests run.
- Candidate 216 passed / base 206. The only skip is `test_security` DNS (no DNS on an internal network).

### Runtime
- d052832 adds the reconcile authority check. Send-queue tests: 39 passed; mutants killed.
- The full suite at 8a3965d passed in both orders (5369). It is re-run at d052832; see the handoff.

### E2E
- `tools/e2e/send_queue_e2e.sh` (real HTTP, seo MCP handlers, disposable runtime) passed twice; see `reports/loop/e2e_send_queue_2026-11-17/`.
