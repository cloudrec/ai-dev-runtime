# Stage 15 F1-F3 handoff (Opus 5.5 session), 2026-11-04

An automated instruction was received through the Owner OS API. It is not owner approval.
**Checks below are the implementer's own. This is NOT an independent audit. Fable final audit: PENDING. PROD: NO-GO.**
Nothing was deployed. No PROD, live DB, server auth, credential or paid API was touched, API mode stays `enabled: false`, and no subagent or extra Claude process was used.

## Branch
`owneros/stage15-f1-f3` in `/opt/owneros-autonomy`, from `owneros/stage15-remediation` @ `8e2edb2` (baseline unchanged). Not pushed.

| commit | finding | root cause fixed | tests |
|---|---|---|---|
| `ad76c5b` | F1 | State change and audit row were two transactions. `submit_decision`, `ack_review` and the engine-side reopen, supersede, hold-escalated and owner-answered-hold paths now use one `BEGIN IMMEDIATE` (`_tx`, `_audit(in_tx=True)`). The reopen row records the key it clears, so a key that lost its `accepted` row before this fix stays spent | `tests/test_stage15_f1_audit_atomic.py` (12): `_audit` raising, a real SQLite trigger failure, process death between INSERT and COMMIT, 8 concurrent processes (same and different keys), chain still valid |
| `efd4837` | F2 | Direct `claude` spawns inherited the daemon env. New allowlist `subscription_env()` used in `ai_planner`, `native_sessions` and `native_agents` (the last was not in the audit list). Foundry switches added to `API_ENV_VARS` for panes | `tests/test_stage15_f2_spawn_env.py` (11): fake executable records its real env and behaves correctly at all 3 sites; login, HOME, config dir and proxy survive; AST guard fails on a new unguarded spawn |
| `f6c76ef` | F3 | API mode rested on agent-writable YAML and ledger rows. Now needs a signed owner proof, single use, 300 s, bound to session, dir, model, risk, conversation and launcher, separate from the creation-gate proof (`api_proof`). Spend and count commit together in append-only, hash-chained `agent_auth_api_chain`. New `owner_proof.py sign-api-launch`, `agent_create.py --api-proof-file` | `tests/test_stage15_f3_api_owner_proof.py` (44): no/spoofed/mis-bound/expired/wrong-key proof, actor strings, replay, idempotent retry, failed start, concurrent starts (threads and processes), row deletion, dropped triggers/table |

`tests/test_agent_auth_mode.py` `_create` now signs a proof for API launches, because they are refused without one.

## Evidence
- Full suite at `f6c76ef`: **4488 passed, 4 skipped, 0 failed, exit code 0**, 530 s, one run. Command: `python3 -m pytest -q -p no:cacheprovider tests/`. Log: `reports/stage15/full_suite_f6c76ef.txt`.
- Baseline `c063abb`: 4421 passed, 4 skipped, 0 failed. Delta +67 = the 67 new tests. Pre-existing failures: none.
- Tests fail without the fixes: F1 7 of 12 on the old engine; F2 5 of 11 with `env=` removed; F3 25 of 44 on a mutant with proof and trigger checks removed.
- Not run: the pre-audit repro scripts, the real `claude` binary, anything live.

## Residual risks
1. **Agents run as root.** One that reads `/etc/ai-runtime/owner-proof.key`, sets `AGENT_OWNER_PROOF_KEY_FILE` to its own key when running `tools/agent_create.py`, edits the code, or rebuilds a chain can pass F1 and F3. These controls stop mistakes and text spoofing, not a hostile root process. A real boundary needs a signer off the host or agents not running as root.
2. Tail truncation of either chain, after dropping its triggers, is not detected; there is no external head anchor. For API launches, `owner_proof_used` only partly cross-checks.
3. F2's allowlist is proven only against a fake executable. If the real CLI on the host needs a variable outside it, planner calls fail or `native_*` fall back. Check this in a canary before any deploy.
4. Panes still use a denylist (`env -u`), and a settings-file `env` block or `apiKeyHelper` is not covered there. Planner calls pass `--setting-sources ""`; `claude agents --json` makes no model call.
5. If legacy rows in `agent_auth_launch` are deleted, recovery no longer sees a session as API and revives it, but on subscription with credentials stripped. No API spend results.
6. Duplicate decision calls now take the write lock (`BEGIN IMMEDIATE`). Migration of the new table was not tried on a copy of the live DB.
7. Hard money ceiling is still the Anthropic workspace spend limit (owner). F5 legacy HMAC closure remains an owner rollout decision.

## Next steps
1. Fable final audit of `8e2edb2..f6c76ef`, with this report and the pre-audit attached.
2. After that, any deploy needs backup, canary (real `claude` smoke for F2) and rollback plan per the 2026-11-04 canonical handoff.

## Rollback
Branch only. `git revert f6c76ef efd4837 ad76c5b` (any subset), or delete the branch. `agent_auth_api_chain` is only created by an API launch attempt, which is disabled.

## Post-audit fixes (after `reports/OWNER_OS_STAGE15_FABLE_AUDIT_2026-11-04.md`)
Branch `owneros/stage15-audit-fixes`, from `owneros/stage15-f1-f3` @ `e94a32e`. Requested by the user in this session. Not pushed, not deployed. **Fable reviewed only `53e7a2b..f6c76ef`; these commits are not audited.**

| commit | item | change |
|---|---|---|
| `353e5ba` | F2 residual | `LocalHost.run`: every child loses `API_ENV_VARS`; a direct `claude` gets `subscription_env` |
| `3674163` | R2 | HMAC v2 signs a compact JSON array (`api/v1.py` `hmac_v2_message`). Changed in place: no client signs v2 yet |
| `28b38d5` | R3 | `_tx`, `submit_decision` and `ack_review` raise `EngineError` on open caller work instead of committing it |
| `2d762c4` | R4 | `commit_api_launch` re-verifies signature, expiry and binding inside the spend transaction |

Full suite at `2d762c4`: **4502 passed, 4 skipped, 0 failed, exit code 0**, 565 s, one run. Log: `reports/stage15/full_suite_2d762c4.txt`. Delta from `f6c76ef`: +14 tests.

Still open from the audit: R1 (allowlist drops non-credential settings), R6 (`owner_proof_used` unprotected), F5 legacy default, F6 and F7 partial, and the 5 sandbox E2E conditions. Rollback: `git revert` any of the four commits, or delete the branch.
