# Stage 15 Fable audit of the latest fixes, 2026-11-04

Audit of `a15dd26..acee756` on `owneros/stage15-final-fixes`, requested by the owner in this session. Run as a subagent through the `Agent` tool with `model: "fable"`.
Model: self-reported Claude Fable 5.1 (`claude-fable-5-1`); the client gives no independent proof. Billing was not checked.
**Verdict: sandbox E2E yes with 3 conditions. PROD not granted, PROD: NO-GO. Nothing deployed.**

Filed by the Opus 5.5 session, condensed. Auditor's evidence: read-only; `tests/test_stage15_final_fixes.py` 96 passed; 10 probes on tmp DBs, tmp anchor and tmp key. Repo untouched.

## Conditions
1. Fix M-B, or after the first API launch confirm the anchor exists with `rows: 1` and treat any reanchor with `anchored_rows_before: 0` and `rows >= 1` as review-worthy.
2. Keep `AGENT_AUTH_SETTINGS_GUARD` unset unless the settings files are owner-written.
3. Read `restored_triggers`: `owner_proof_used_*` listed after a chain exists means the spent table may have been rebuilt, so a general proof could have been replayed inside its 300 s TTL.

## Findings
**M-B, MEDIUM, CONFIRMED: a missing anchor file vouches for a one-row chain.** `core/agent_auth_mode.py:272-273`. Every anchor state is keyed except "no file", which a script with directory access produces by unlink. 2 launches; both tables rebuilt with triggers, minus row 2 and its nonce; anchor unlinked: `_chain_problem` is None and the freed proof launches again. One launch hidden, one nonce replayed. A flagless `reanchor` also seals 2 to 1 and 1 to 0. Not a regression; bounded to +1 launch. M-A is closed only for chains of 2 rows or more.

**L-E, LOW, CONFIRMED: a reanchor whose anchor write fails after COMMIT restores triggers unaudited.** `_write_anchor` raises outside the try and `ac.audit` is never reached. `ac.audit` also swallows `OSError` by design, so the audit claim is best-effort.

**L-G, LOW, CONFIRMED: redirects with a leading fd number or `>|` hide a command-position `claude`.** `2>/dev/null claude -p x`, `1> out claude`, `>| out claude`. Also missed at `a15dd26`.

## INFO
- Against `a15dd26`, 45 strings: only intended flips, and no run seen then is missed now. Unseen: backticks or `$()` inside double quotes, `coproc NAME { claude; }`, `env -S`.
- Launcher lint: a backslash-quoted name passes; the docstring should say so or the lint should strip it.
- `_chain` recreates a dropped chain before any check; still refused (0 rows, spent > 0), but the evidence changes form.
- L-D reasoning holds. `_close_anchor_lag` verified. `reanchor` writes nothing before a refusal apart from `_chain`'s own creation.
- Operator text: nothing says `anchored_rows_before > rows` means deleted rows, or that a flagless reanchor before a key rotation records the vouched count. The "not detected" docstring line should cover the no-anchor case.
- Tests not vacuous. The timing test has a 14x margin under load. `test_ma_the_tool_passes_the_flag` uses a cwd-relative path.

## Status
M-A partly closed (M-B). L-A, L-B, L-C, L-D closed. Previous conditions: 3 closed except M-B; 2 no longer needed for correctness; 1 is procedure; 4 unchanged.

## Not checked by the auditor
The 14-mutant claim, the full suite, the live host, sandbox DB state, `SshHost`, two-process concurrency beyond reasoning, whether a text-injected spec author can reach a trusted check.
