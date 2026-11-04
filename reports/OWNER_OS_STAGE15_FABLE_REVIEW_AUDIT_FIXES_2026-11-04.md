Reviewed: e94a32e..2d762c4
Model: Fable 5.1 (claude-fable-5-1)
Repo: /opt/owneros-autonomy, branch owneros/stage15-audit-fixes (read-only; no edits, no stash, no branches)
Scope: commits 353e5ba, 3674163, 28b38d5, 2d762c4; audit items R2, R3, R4 and the F2 residual on core/fullcycle/hosts.py.

## 1. Status of the four items

### R2 (HMAC v2 canonical form) — CLOSED, with one residual canonicalisation quirk (LOW)
- Fixed: `api/v1.py:50-54` `hmac_v2_message` is a compact ASCII JSON array; fields cannot run into each other. `api/v1.py:78-80` uses it. Pinned by `tests/test_stage15_remediation.py:128-148` (separator collision, documented bytes, old pipe format refused).
- Verified by repro (`/tmp/claude-0/-opt-owneros-autonomy/f577cb74-e132-4d41-a859-9cdee9a5d412/scratchpad/repro_r2_url.py`, python3 -I, no network): non-ASCII path -> `é` escape, lone surrogate -> `\udcff` escape (no exception), percent-encoded query kept raw, method upper-cased. All ASCII bytes, no ambiguity between fields.
- Residual (see finding N1): the message is built from `request.url.path` / `request.url.query`, which Starlette 1.3.1 rebuilds via `SplitResult(...).geturl()` then `urlsplit` (`starlette/datastructures.py:40-72`). The decoded scope path is re-parsed, so `?` and `#` that came from `%3F`/`%23` move characters between path and query. The audit already noted the decoded-path point; the re-parse was not in the audit and is new evidence, not a regression of this diff.

### R3 (`_tx` commits caller's open work) — CLOSED for the three guarded entry points; residual INFO
- `core/fullcycle/engine.py:209-222` `_refuse_open_tx` + `_tx` raise `EngineError` instead of committing. `engine.py:320` (`ack_review`) and `:350` (`submit_decision`) refuse before `connect()`, which still commits (`engine.py:184`).
- Test `tests/test_stage15_f1_audit_atomic.py:137-146` proves the submit path and that the caller's UPDATE is not committed.
- Residual: `connect()` still commits whatever a caller left open for every other entry that takes a `conn` (`Engine.__init__` `engine.py:455`, `pending_reviews`, `verify_decision_audit`, `project_status`, `attempt_in_flight_for_agent`, `settled_project_for_agent`). The same "commit the caller's open work" pattern remains in `core/agent_auth_mode.py:163-164` (`_chain`) and `:220-221` (`commit_api_launch`). Outside R3's wording, same defect class; no live caller passes a conn, so INFO.

### R4 (`commit_api_launch` trusts its caller) — CLOSED
- `core/agent_auth_mode.py:209-245`: inside `BEGIN IMMEDIATE`, `_chain_problem(c) or _api_proof_problem(..., c, ...)`; signature, binding (session, realpath(project_dir), model, risk, conversation_id, launcher) and nonce reuse are re-checked before the spend. Nonce is taken only after the check passes (`:230`), and `_NONCE_RE` (`core/agent_control.py:1897`) guarantees it is non-empty hex.
- Nested use of the connection: `_api_proof_problem` receives `c` and `_ledger(c)` / `_chain(c)` reuse it (`:140-143`, `:160-161`: table exists, so no commit, no second BEGIN). `check_owner_proof` (`agent_control.py:1938-1963`) touches no database. No second connection is opened inside the write transaction, so no self-deadlock and no busy-timeout wait. Confirmed by running the f3 tests, including the 6-process nonce race (`tests/test_stage15_f3_api_owner_proof.py:286-308`).
- Expiry between resolve and spend: both checks use `time.time()` when `now` is None; a proof that expires in between returns `owner_proof_expired`, nothing is spent, `agent_resume` audits and returns refused before `new-session` (`core/agent_control.py:2407-2420`). Fail closed.
- Launcher from `agent_resume`: `auth["launcher"]` is set only on the API-ok branch of `resolve` (`agent_auth_mode.py:427-429`) and read only under `auth["mode"] == API` (`agent_control.py:2404-2413`); no KeyError path. No test stubs `aam.resolve`. Fields match between resolve and commit (`use` lowercased model, `risk or ""`, realpath in `api_launch_text`).
- Tests `tests/test_stage15_f3_api_owner_proof.py:188-206` fail without the fix (old code returned "" for a bare nonce dict, and AttributeError for a string).

### F2 residual (`LocalHost.run` merges the full environment) — PARTLY CLOSED
- Closed: `core/fullcycle/hosts.py:56-62,72`: a child whose `argv[0]` basename is `claude` gets `subscription_env` (allowlist); every other local child gets `os.environ` + overlay minus `API_ENV_VARS` (denylist). Proven with a real fake executable in `tests/test_stage15_f2_spawn_env.py:187-203`.
- Still open: see findings N2 and N3. The allowlist applies only to a direct `claude` basename; the configured API launcher is not treated as credentialed at all; SshHost's remote side is untouched (by design, but the docstring claim is local-only). The AST guard still does not cover hosts.py (unchanged from the audit).
- Breakage check: git (`engine.py:764`, `:1424-1428`), spec checks (`engine.py:807-808`, `CHECK_ENV` kept — verified by the `sh -c env` test keeping `CHECK_VAR` and `PROJECT_SETTING`), `write_file`/`read_file`/`exists` (sh/cat/test) and SshHost's local `ssh` child lose only the twelve `API_ENV_VARS`, none of which those programs need. `env=None` now becomes an explicit dict equal to `os.environ` minus the denylist — equivalent. For a direct `claude` check the `CHECK_ENV` overlay (`PYTHONDONTWRITEBYTECODE`) and anything outside `SUBSCRIPTION_ENV_KEEP` are dropped; intended.

## 2. New defects / regressions in the diff

N1 — CONFIRMED (repro), LOW. v2 message is built from Starlette's re-parsed URL, not from the ASGI scope. `api/v1.py:78-79` passes `request.url.path` and `request.url.query`. Starlette joins decoded `scope["path"]` and raw `query_string` into one string and `urlsplit`s it (`starlette/datastructures.py:58-72`). Repro output:
  - scope path `/api/v1/jobs?a=1`, query `` -> signed as path `/api/v1/jobs`, query `a=1` (identical bytes to the legitimate `/api/v1/jobs?a=1`).
  - scope path `/api/v1/jobs#x`, query `a=1` -> signed as path `/api/v1/jobs`, query `` (the real query is NOT in the signed message).
  Routing uses `scope["path"]`, so a bare route is unreachable this way, but every `{param}` route (`api/v1.py:302-333, 539-561, 943, 1190-1360`) and the `{ref:path}` routes (`:1128-1148`) accept `?`/`#` inside the parameter (`[^/]+` / `.*`). So a signature computed for `GET /api/v1/jobs/x` (no query) is also valid for `GET /api/v1/jobs/x%23y?anything`, which dispatches to `job_id="x#y"` with unsigned query parameters. Exploitability is limited: the signature is single-use per process (`api/v1.py:34-46`) and the body hash is bound, so an attacker needs a captured-but-undelivered signature; and the re-aimed path parameter is a different, probably nonexistent, resource. Fix before any external client is written: sign `scope["raw_path"]` (or `scope["path"]`) and `scope["query_string"]` directly, and state the client contract (decoded path, raw query, sha256(body), upper-case method) in one place.

N2 — CONFIRMED by reading (not executed), MEDIUM-LOW. The API launcher is runnable as a trusted check through `LocalHost.run` with no proof, cap or ledger. `gates.check_argv` returns None for any argv list when `trusted=True` (`core/fullcycle/gates.py:96-97`); the engine runs it via `self.host.run` (`engine.py:807`). `_child_env` only looks for basename `claude` (`hosts.py:60`), so `["/usr/local/bin/claude-owner-api", ...]` gets the denylist strip, which is irrelevant because the launcher sources its own credentials. Neither `resolve` nor `commit_api_launch` is on this path. Preconditions: a spec author (owner or tech lead, `specs.put_spec`) writes that check, and the engine's uid can execute the root-owned launcher (the engine runs as root here: `id -u` = 0, and `_launcher_problem` requires root ownership). This is the handoff's admitted "API session started outside Owner OS is not counted" residual, now reachable from a spec check rather than only by hand. Suggested fix: `gates.check_argv` refuses the configured launcher basename and `DEFAULT_LAUNCHER` for trusted and untrusted checks alike (`recovery_command` already treats those names as forbidden, `agent_auth_mode.py:438-446`).

N3 — CONFIRMED by reading, LOW. Basename-only detection means an indirect `claude` gets the denylist, not the allowlist: `["sh", "-c", "claude -p ..."]`, `["env", "X=1", "claude"]`, `node .../cli.js`, a wrapper script, `claude-code`. The docstring says so (`hosts.py:57-58`), and the twelve named variables are stripped, but the audit's F2 rationale ("a credential nobody has named yet stays out") does not hold for these shapes. Interaction with N2: a trusted check `["sh","-c","claude-owner-api ..."]` is the same bypass.

N4 — PLAUSIBLE, LOW. `_tx` now raises where it used to commit, and one in-tick path can leave DML open on the shared connection: `_event` -> `_offer_to_wake_bridge` (`engine.py:417-436`) swallows every exception; if `wb.should_wake`/`wb.record` (`core/wake_bridge.py:763, 866`) raise after DML on `self.conn` (record's INSERT at `wake_bridge.py` record:18 precedes its commit), the connection stays `in_transaction` and the next `_tx` in the same tick (`_supersede_stall`, `_blocked_review`, `_hold`, `_owner_answers`) raises `EngineError`. The suite runs with `WAKE_BRIDGE_ENABLED` unset (`wake_bridge.py:540`), so the bridge returns at `engine.py:425-427` and the tests cannot show it; live has the bridge on. Consequence is bounded: the worker logs the error, drops the engine and rebuilds it next round (`tools/fullcycle_worker.py:72-74`), and the dropped connection rolls the leak back; also `_lead()` -> `cp.acquire_lease` commits on the shared conn at the start of every tick and before every step (`engine.py:1562-1570`), so a leak never survives into the next tick. I traced every writer that takes `conn=self.conn` and all commit before returning: `cp.append_event/acquire_lease/release_lease/open_gate`, `tc.create/transition`, `specs.put_spec`, `wb.record`; `wb.should_wake` and `_migrate` only SELECT/DDL. No engine path reaches `_tx` with its own uncommitted DML. `connect()` is not called inside any `_tx`.

N5 — INFO. R3 residual as above: `connect()` and `commit_api_launch`/`_chain` still commit a caller's open transaction. No live caller passes a conn (API endpoints `api/v1.py:539-560` and the worker use their own), so no runtime effect today.

N6 — INFO. `SshHost.run` serialises the `env` overlay into the remote argv (`hosts.py:107-108`); the local strip does not reach the remote, and an overlay containing a credential would be visible in the remote `ps`. Pre-existing; the new docstring's "No local child" is accurate but easy to over-read.

No defect found in: the HMAC byte format itself (N1 is about the inputs, not the encoding); `commit_api_launch`'s transaction shape; `agent_resume`'s launcher plumbing; the strip for git/check/ssh children.

## 3. Tests that pass vacuously or would survive removing the fix

- `tests/test_stage15_f2_spawn_env.py:187-194` (direct claude via LocalHost): fails without the fix — old `{**os.environ, **env}` passes `ANTHROPIC_API_KEY=x` (caught by the `ANTHROPIC_` prefix assert) and `NODE_OPTIONS=sentinel`. Not vacuous.
- `tests/test_stage15_f2_spawn_env.py:197-203` (`sh -c env`): fails without the fix (sentinels in `API_ENV_VARS` leak). Not vacuous. Gap: no test for an indirect claude (`sh -c claude`), which is exactly the shape N3 leaves on the denylist.
- `tests/test_stage15_f1_audit_atomic.py:137-146`: fails without either guard (old `_tx` commits and the decision proceeds; removing only `engine.py:350` makes `connect()` commit `payload='uncommitted'`, failing the final assert). Not vacuous. Gap: nothing exercises `ack_review`'s guard; `engine.py:320` can be deleted without a failure.
- `tests/test_stage15_remediation.py:128-148`: the collision test fails on the old pipe format; the documented-bytes test pins the format. Not vacuous. Gap: nothing covers N1 (the test's `_req` stub at `:73-77` hands `_auth` a pre-split `url.path`/`url.query`, so the Starlette re-parse is never exercised).
- `tests/test_stage15_f3_api_owner_proof.py:188-206`: fail without the fix except the `None` parameter, whose expected string `api_owner_proof_required` is what the old code returned too (vacuous for that one row only). `:286-308` six-process race still asserts exactly one spend.
- Ran (focused, tmp DBs from `tests/conftest.py:25-43`, fake tmux, fake launcher, tmp owner key): the 14 tests named above plus the 12 `r2`/`f5` tests in test_stage15_remediation.py — all pass. Full suite not run; mutants not executed, reasoned only.

## 4. Verdict

Fit to carry forward to a live sandbox E2E: YES, with conditions. PROD stays NO-GO.
1. Before any spec with trusted checks runs in the sandbox: either make `gates.check_argv` refuse the configured launcher and `DEFAULT_LAUNCHER` basenames (N2), or confirm the sandbox launcher is absent or non-executable by the engine's uid.
2. Before writing the external v2 client: sign `scope["path"]`/`raw_path` and `scope["query_string"]` directly (N1) and write down the client contract. The current server format is fine to keep; only the inputs move.
3. Run the sandbox with `WAKE_BRIDGE_ENABLED` as in production and watch the worker log for `EngineError: transaction already open` (N4); one occurrence is a self-healing tick error, repeated ones are a leak to find.
4. Add tests for `ack_review`'s open-transaction guard and for an indirect `claude` through `LocalHost` (N3), so the next mutant run has teeth there.

## Not checked
- The real `/usr/local/bin/claude-owner-api` launcher content, live `control_plane.db`, `/etc/ai-runtime`, `/root/.config/owner-os`, `/root/ai-dev-runtime` — not opened.
- The seo-repo MCP client that calls the fullcycle endpoints; no v2 HMAC client exists in this repo.
- uvicorn's exact path decoding (assumed standard `unquote` with replacement, so no surrogates arrive from the wire; the repro fed a surrogate directly to prove the encoder does not raise).
- `wb.should_wake` internals beyond grepping its body for DML; `WindowsBridgeHost` (refuses everything).
- Anything in the audit outside R2/R3/R4/F2-residual.
