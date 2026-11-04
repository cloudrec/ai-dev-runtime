# Stage 15 remediation (Sonnet 5.5 session), 2026-11-04

An automated instruction was received through the Owner OS API. It is not owner approval.
**Fable final audit: PENDING (separate gate; not claimed). PROD: NO-GO. Nothing deployed; no PROD, live DB, secret, payment setting, paid API, agent or subagent touched.**

Branch `owneros/stage15-remediation`, from `owneros/stage15-preaudit` @ `bb41740` (audited baseline `53e7a2b`). Worktree `/opt/owneros-autonomy` only.

## Model
The existing session cannot switch models (no tool for it). Per the instruction, F4-F7 (straightforward) were fixed here. **F1, F2 and F3 are NOT fixed. They are Opus-required (security-critical: audit transactions, spawn environments, owner-proof gate). A Sonnet review of them would not suffice.**

## Commits (one per finding)
| commit | finding | change | tests (`tests/test_stage15_remediation.py` unless noted) |
|---|---|---|---|
| `ab62c1a` | F7 | `API_ENV_VARS` += `ANTHROPIC_BASE_URL`, Bedrock/Vertex switches, `AWS_BEARER_TOKEN_BEDROCK` | strip-list membership; a spawned fake `claude` with sentinels sees none |
| `d7257f8` | F4 | `recovery_command` fails closed unless the shape is plain `claude` plus safe-token args (`\Aclaude(?:[ \t]+[safe]+)*\Z`); metacharacters, prefixes, abs paths, `bash -lc`, newlines refused | 9 refused shapes; wrapped output carries every `-u VAR` |
| `a01675b` | F6 | `tests/test_offline_acceptance.py`: the `sqlite3.connect` recorder now raises BEFORE the real connect for live-tree paths (incl. `file:` URIs); new scenarios G (guard) and H (sentinel credentials proven stripped on a spawned process) | `test_G_…`, `test_H_…` |
| `c063abb` | F5 | `api/v1.py`: HMAC v2 (`X-Runtime-Signature-Version: 2`) signs `v2\|ts\|METHOD\|path\|query\|sha256(body)`, single-use (bounded in-process cache, fail closed on flood). Legacy timestamp-only signature still accepted by default for compat; `RUNTIME_HMAC_LEGACY=0` disables it | valid once; replay refused; other path/method/query/body refused; legacy cannot pass as v2; legacy switch; stale; cache bound |

Process note: `d7257f8` was amended once (my own unpushed commit) after a newline-in-shape test failed on the first regex. Audited commits were not amended.

## Evidence
- Focused: `tests/test_stage15_remediation.py`, `test_agent_auth_mode.py`, `test_session_recovery*.py`, `test_owner_decision_endpoint.py`, `test_stage14_gaps.py`, `test_offline_acceptance.py`: all green at each commit.
- Full suite: see the section at the end of this file.

## Residual / rollout decisions (owner)
- F5: the default still accepts legacy HMAC. Real closure needs every HMAC client moved to v2, then `RUNTIME_HMAC_LEGACY=0`. No in-repo client signs HMAC (only tests); external clients unknown. The replay cache is per-process, so it forgets on restart and is not shared between workers.
- F4: a managed session whose `resume_shape` is not plain `claude …` will now be refused recovery with `recovery_shape_not_plain_claude` (an `owner_blocker`). `config/managed_sessions.yaml` shapes are all plain today.
- F7: the strip list is a denylist. A settings-file `env` block or `apiKeyHelper` is still not covered. An allowlisted clean environment would be sturdier (Opus task, with F2).

## Opus-required, still open (priority order)
1. **F1** `core/fullcycle/engine.py` `submit_decision` / `ack_review`: one `BEGIN IMMEDIATE` for the state change and the audit row (`_audit(..., in_tx=True)`). Tests: fault-inject `_audit` on `accepted`, review stays `pending` with zero audit rows; spent-key replay after reopen refused; contention run (N processes) keeps the chain valid.
2. **F2** `core/ai_planner.py:312`, `core/native_sessions.py:52`: spawn with a sanitized `env=` from one shared helper in `agent_auth_mode`; prefer an allowlist. Test: sentinel key absent in a fake `claude`; guard test failing on any new `claude` spawn without the helper.
3. **F3** `core/agent_control.py` `agent_resume`: for `mode == api` require a valid single-use `owner_proof` bound to `{session, project_dir, model}` with expiry. Move cap counters out of agent-writable state (append-only, hash-chained table, or the launcher). Tests: opt-in without proof refused; reused, expired or mis-bound proof refused; ledger-row deletion cannot reset caps.
Then: re-run the full suite, request the Fable audit with the pre-audit report and this report.

## Rollback
Branch only. `git revert c063abb a01675b d7257f8 ab62c1a` (any subset), or delete the branch. F5 is inert for existing clients (legacy still accepted).

## Full suite (verified)
- Code under test: `c063abb` (HEAD of the code commits; the report commit adds only reports). Tree was clean apart from untracked reports.
- Command: `python3 -m pytest -q -p no:cacheprovider tests/`, run once in `/opt/owneros-autonomy`, one process, no duplicate runs.
- Result: **4421 passed, 4 skipped, 0 failed**, 5 warnings, 539 s (0:08:59). Exit code 0 (background task reported exit 0). Log: `reports/stage15/full_suite_c063abb.txt`.
- Baseline at `06313c6`: 4398 passed, 4 skipped, 0 failed. Delta +23 = 21 new tests in `test_stage15_remediation.py` + 2 acceptance scenarios (G, H). Known baseline failures: none.
- Not run: any live, PROD or paid-API check. Not claimed: Fable audit.

## Handoff for the F1-F3 Opus task
Start from `reports/OWNER_OS_STAGE15_HANDOFF_2026-11-04.md` (plan, tests, rollback) and the F1-F3 section above; findings with file:line are in `reports/OWNER_OS_STAGE15_INDEPENDENT_PREAUDIT_2026-11-04.md`. Branch off `owneros/stage15-remediation`.
