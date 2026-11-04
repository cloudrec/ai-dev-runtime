# Stage 15 Fable audit of the final fixes, 2026-11-04

Audit of `acdeb95..a15dd26` on `owneros/stage15-final-fixes`, requested by the owner in this session. Run as a subagent through the `Agent` tool with `model: "fable"`.
Model: self-reported Claude Fable 5.1 (`claude-fable-5-1`); the client gives no independent proof. Billing was not checked.
**Verdict: sandbox E2E yes with 4 conditions. PROD not granted, PROD: NO-GO. Nothing deployed.**

Filed by the Opus 5.5 session, condensed. Auditor's evidence: read-only; `tests/test_stage15_final_fixes.py` and `tests/test_offline_acceptance.py -k test_G_` offline, 86 passed; repros on tmp DBs, tmp anchor and tmp key. Repo untouched. One of the auditor's own probe generators ran to 11 GB and was killed; it was not the code under test.

## Conditions
1. Run `tools/owner_proof.py reanchor` once on the sandbox DB before the first API launch and read its `rows` (M-A).
2. Keep the owner key present and 0600 for the whole sandbox run (L-A).
3. Treat `reanchor` as an owner decision, not a routine answer to `api_ledger_tampered`, until M-A is fixed.
4. Leave `AGENT_AUTH_SETTINGS_GUARD` unset unless the project settings files are owner-written.

## Findings
**M-A, MEDIUM, CONFIRMED: `reanchor` silently seals a truncated chain.** `core/agent_auth_mode.py:411-446`, `tools/owner_proof.py`. It checks the rows only, never the existing anchor. A 3-row chain anchored at 3, truncated to 2 with its nonce deleted, gave `{"ok": True, "rows": 2, ...}` and a clean state. The refusal string is the same for an upgrade and for deleted rows, and nothing records the reanchor. A script with DB access can get a truncation laundered by the owner's own hand.

**L-A, LOW, CONFIRMED: a refused attempt can write a keyless anchor.** `_close_anchor_lag` with no owner key writes `mac: ""`, which later reads as tampering. Fails closed, costs an owner step.

**L-B, LOW, CONFIRMED, impact PLAUSIBLE: `_runs_claude` is quadratic on long wrapper chains.** 200k `exec` words took 32.7 s. Only a trusted spec or a direct caller can supply such argv.

**L-C, LOW, CONFIRMED by probe, impact PLAUSIBLE: relative-path rule is a small N2 regression.** Missed: `~/../usr/local/bin/claude-owner-api`, `$HOME/../...`, `cd / && usr/local/bin/claude-owner-api`, `claude-owner-api,`, `claude-owner-api:`.

**L-D, LOW, PLAUSIBLE: a pre-R6 DB that never attempts an API launch keeps a trigger-less `owner_proof_used`.** Nothing reports it, since `_chain_problem` runs only at API launch attempts.

## INFO
- `_runs_claude` misses (denylist env, no credential leak, no F7 check): backticks, `bash --rcfile F -c claude`, `exec -a name claude`, `env -S`, `coproc claude`, `builtin claude`. False positives: `command -v claude`, a redirection target named `claude`.
- `_close_anchor_lag` re-reads the anchor without type checks; a garbage anchor written between the two reads raises `TypeError` (rollback happens).
- A row forged in the lag window is sealed by any refused attempt. Not a regression.
- `_settings_refusal` is safe on missing and relative `cwd`. No check for `SshHost` children. It reads the daemon's config dir, not one passed in `env`.
- Acceptance guard: `sh -c "python3 ..."` with a variable unset is not caught; `os.fork`, `os.execv`, `os.spawn*` are not covered (not claimed).

## Status
M1 closed (with M-A and L-D on the repair path). L1, L2, L4, L5 (regression L-C) closed. L3 closed as claimed (side effect L-A). F7 gap closed for spawns `_runs_claude` sees. Acceptance guard closed for what it claims. Conditions of the previous audit closed. The tests are not vacuous.

## Not checked by the auditor
The 15-mutant claim, the full suite, the live host, sandbox DB state, `raw_path` empirically, `SshHost` remotely, two-process concurrency in `verify_owner_proof` beyond reasoning, the engine's handling of rc 126 beyond `_run_checks`.
