# Stage 15 Fable audit of the review fixes, 2026-11-04

Audit of `05073f4..e96e72d` on `owneros/stage15-review-fixes`, requested by the owner in this session. Run as a subagent through the `Agent` tool with `model: "fable"`.
Model: self-reported Claude Fable 5.1 (`claude-fable-5-1`); the client gives no independent proof. Billing was not checked.
**Verdict: sandbox E2E yes with 4 conditions. PROD not granted, PROD: NO-GO. Nothing deployed.**

Filed by the Opus 5.5 session. Editor's check, code read only, findings not reproduced by the filer: M1, L1, L2, L4 and L5 match the code at `e96e72d`. One correction under M1. Repo was clean after the run. The auditor's repro script was left in the session scratchpad, outside the repo.

Auditor's evidence: read-only; `tests/test_stage15_review_fixes.py`, `test_stage15_f3_api_owner_proof.py`, `test_stage15_f2_spawn_env.py` offline, 128 passed. Repros used tmp DBs, a tmp owner key and a tmp anchor only.

## Conditions for sandbox E2E
1. Before the first API launch in the sandbox, make sure the two `owner_proof_used_no_*` triggers exist on that DB (M1).
2. Correct the R6 claim (docstring and handoff) that a dropped trigger is detected: true for the chain triggers, not the proof triggers (M1).
3. Fix the exception gap in `settings_api_overrides` (L1) before an agent can write a project settings file in the sandbox.
4. Know that trusted checks through `sh`, `env` or `timeout` with the bare word `claude` in any argument lose their environment (L2).

## Findings

### MEDIUM
**M1: `ensure_proof_table` is both the detector's precondition and its silent repair. CONFIRMED.**
`core/agent_auth_mode.py:281-285` refuses when the two `owner_proof_used_no_*` triggers are missing. `_chain` (`:250-256`) returns early when the chain table exists, so it never adds them to a DB created before `47264ad`. On such a DB every API launch is refused as `api_ledger_tampered`. Then `verify_owner_proof` (`core/agent_control.py:1976`, any `agent_send` or `agent_stop` with a valid proof) runs `ensure_proof_table`, whose `CREATE TRIGGER IF NOT EXISTS` recreates the triggers, and the tamper state vanishes.
- Migration trap: a sandbox DB that ran `05073f4`-era code refuses API launches until an unrelated proof use happens.
- The R6 claim is not durable for proof triggers: a DB-only script can drop `owner_proof_used_no_delete`, delete spent non-API nonces (replayable for the rest of their 300 s), and the next legitimate proof use erases the evidence. API nonces stay blocked by the chain's own nonce check, so no money impact; the audit-trail impact is real.
- Smallest fix: `verify_owner_proof` creates only the table; triggers are created in `_chain()` at first creation and, for existing DBs, by an explicit owner upgrade step.
- Filer's correction: the auditor names `owner_proof.py reanchor` for that step. `reanchor` does not call `ensure_proof_table` at `e96e72d` (the only callers are `core/agent_auth_mode.py:269` and `core/agent_control.py:1976`), so that is a change to make, not existing behaviour.

### LOW
**L1: `settings_api_overrides` lets `RecursionError` escape and blocks the resume. CONFIRMED.** `core/agent_auth_mode.py:577-581` catches `OSError, ValueError` only. A `.claude/settings.json` of 100k nested `[` raises `RecursionError` out of `agent_resume` inside `creation_guard` (the guard is released, no stuck lock). A text-injected agent can so make every resume of its own project raise, in the default mode too. `MemoryError` on a huge file likewise. Fix: `except Exception` gives `unreadable`; optionally cap the size before `json.load`.

**L2: `_runs_claude` false positives switch legitimate checks to the allowlist env. Mechanism CONFIRMED, impact PLAUSIBLE.** `core/fullcycle/hosts.py:60-71`: with `argv[0]` a wrapper, any argument containing the bare word counts: `["bash","-c","pytest -k claude"]`, `["env","FOO=claude","pytest"]`, `["sh","-c","git log --grep claude | wc -l"]`, `["timeout","60","npm","test","--","--grep","claude"]`. The child gets `subscription_env` and loses `PYTHONPATH`, `VIRTUAL_ENV`, `CI`, `GIT_*` and `CHECK_ENV` (`PYTHONDONTWRITEBYTECODE`, so the stale-.pyc problem at `engine.py:803-805` can return for such a check). Misses go to the denylist, not a leak of `ANTHROPIC_*`: `'claude'`, `setsid claude`, `flock … claude`, `npx claude`, `node …/claude`, `sh ./run_claude.sh`, `claude-code`. Fix: for `sh -c` style, match only the first word of each simple command; for `env`, `nohup`, `timeout`, `nice`, `xargs`, `sudo`, the first non-option, non-`K=V` word.

**L3: one-row anchor lag is usable, only in the failed-write window. CONFIRMED.** Normal truncation is caught. After `api_anchor_write_failed` (`core/agent_auth_mode.py:354-357`; chain N+1, anchor N, proof spent, nothing started), a DB-only script can drop the two delete triggers, delete the last chain row and its spent nonce, and recreate the triggers: `_chain_problem` is None and the same proof launches again within its 300 s TTL. The window lasts until the next successful launch. Gain: one uncounted launch and one replay; both need a write failure or a crash in the COMMIT-to-file gap. Fix: in `commit_api_launch`, under `_anchor_lock`, if the anchor lags by exactly one row, rewrite it to the head before the proof check.

**L4: `_signed_target` fallback drops characters after a decoded `?`. CONFIRMED, unreachable with the shipped server.** `api/v1.py:81-82` applies `split(b"?")` to the fallback built from the decoded `scope["path"]`, so without `raw_path` a request for `/jobs/x%3Fevil` is signed as `/jobs/x`. uvicorn 0.51.0 (Dockerfile) and TestClient set `raw_path`. Fix: split only `raw_path`, or refuse v2 when it is absent.

**L5: `check_argv` substring match. CONFIRMED.** `core/fullcycle/gates.py:97`: a configured `launcher: /usr/local/bin/api` refuses `pytest tests/api`; the default name refuses a path such as `/opt/claude-owner-api-docs`. Fails closed. Fix: compare path components or whole words. By-design limit: a launcher invoked indirectly (npm script, Makefile, shell file) is invisible to argv, and the agent in its pane can exec it anyway, so N2 guards the engine, not the agent.

### INFO
- `tests/test_offline_acceptance.py:78`: `_LiveGuardedPopen` checks a DB variable only when set; unset means the live default path. Safe today because `tests/conftest.py:26-36` sets all three. `os.system` and `posix_spawn` are not covered.
- `reanchor` on an empty chain (`core/agent_auth_mode.py:373-375`) leaves a stale anchor; after a legitimate reset the owner must delete the file by hand.
- With an anchor present and the owner key missing or mis-permissioned, the refusal reads `api_ledger_tampered`, not `owner_proof_unconfigured`.
- `RUNTIME_HMAC_LEGACY=warn` logs `request.client.host`; the Dockerfile runs uvicorn without `--proxy-headers`, so behind docker or nginx it names the gateway. Deployment note for the migration doc.

## Verdicts per question
1. `commit_api_launch`: no lost spend, no deadlock. Lock order is `creation_guard`, anchor flock, `BEGIN IMMEDIATE` everywhere; `reanchor` takes only the flock and reads. A failed anchor write spends and counts, starts nothing. The residual `if c.in_transaction: c.commit()` would commit a caller's open work, but no live caller passes a connection. OK.
2. `_anchor_problem`: one row, only while the anchor lags, until the next successful launch (L3). Not usable otherwise.
3. `ensure_proof_table`: `IF NOT EXISTS` on existing objects takes no write lock (verified against a held `BEGIN IMMEDIATE`). The first run on a DB writes schema, same risk class as the earlier `CREATE TABLE`. Cannot block a live `agent_send` beyond what the following INSERT could. The real problem is M1.
4. `_runs_claude`: no credential bypass; false positives break legitimate checks (L2). `is_api_env_var`: correct; it also drops `ANTHROPIC_MODEL` and `ANTHROPIC_DEFAULT_*` from every child, acceptable but worth stating.
5. `_signed_target`: correct with uvicorn 0.51 and TestClient. A rewriting proxy only causes false 401s, and the doc says so. Missing `raw_path` reintroduces a weaker N1 (L4).
6. `check_argv`: false refusals only with a short configured basename or a path containing the default name (L5); fails closed.
7. `settings_api_overrides`: `RecursionError` on a crafted file blocks the session (L1), `MemoryError` on a huge file likewise. Missing, unreadable, non-dict, BOM and bad JSON are handled.
8. `_LiveGuardedPopen`: weakens and skips nothing; it only raises before a child starts, and a guard trip fails the test loudly. Gaps are in INFO.

## Status of the claimed items
- N1: closed; the tests go through the real ASGI stack. Fallback quirk L4.
- N2: closed for any argv naming the launcher; indirect invocation remains, as it must.
- N3: closed for the listed wrappers; other wrappers go to the denylist (acknowledged); introduces L2.
- N4: closed.
- R1: closed.
- R6: partly. Chain triggers, keyed anchor, truncation, forged, old and garbage anchors, key rotation and concurrent spends are proven. Not closed: self-repairing proof-trigger detection and the migration trap (M1); lag window (L3).
- F5: partly, by design (default unchanged; `warn` and `0` proven; counters per process).
- F6: closed for what it claims; blind to unset-variable defaults and non-`Popen` spawns.
- F7: partly, by design: report-only unless `enforce`; runs only in `agent_resume`; not for `recovery_command` (acknowledged) and not for fullcycle `claude` children spawned in the project dir, where an agent-written `.claude/settings.json` with `apiKeyHelper` would apply (**not acknowledged in the handoff**).

## Not checked by the auditor
The handoff's "12 mutants caught" claim (no mutation runs, by rule). The live host, its unit environment, settings files and the sandbox DB's state. uvicorn's `raw_path` behaviour empirically. Whether middleware in `api/main.py` rewrites the scope before `_auth`. `SshHost` remote behaviour. The full suite. Docs-only commits after `e96e72d`.
