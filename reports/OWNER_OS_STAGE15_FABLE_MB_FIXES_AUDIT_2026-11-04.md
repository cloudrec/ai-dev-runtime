# Stage 15 Fable audit of the M-B fixes, 2026-11-04

Audit of `acee756..ddc75f3` on `owneros/stage15-final-fixes`, requested by the owner in this session. Run as a subagent through the `Agent` tool with `model: "fable"`.
Model: self-reported Claude Fable 5.1 (`claude-fable-5-1`); the client gives no independent proof. Billing was not checked.
**Verdict: sandbox E2E yes with one condition (fix or accept LOW-1). PROD not granted, PROD: NO-GO. Nothing deployed.**

Filed by the Opus 5.5 session, condensed. Auditor's evidence: read-only; `tests/test_stage15_final_fixes.py` 110 passed; 9 probes on tmp DBs, anchor and key, plus a tokenizer comparison with `acee756`. Repo untouched.

## Findings
**LOW-1, CONFIRMED, regression from L-G.** `core/fullcycle/hosts.py:109-110`. The fd pop fires on any digit word before a redirect, so `nice -n 10 >/dev/null claude -p x`, `timeout -k 5 >out claude` and `sudo -u 0 >f claude` were seen at `acee756` and missed at `ddc75f3`. The child gets the denylist env and escapes the F7 report. API variables are still stripped, so no spend. Fix: remove the fd before lexing, only when adjacent to the redirect.

**LOW-2, CONFIRMED, pre-existing.** A separator glued to a redirect is one token (`;>`, `&&>`, `|>`, `(>`), so the target becomes the command word: `echo;>f claude`, `x &&>log claude`, `(>f claude)`. Process substitution `diff <(claude -p x) f` is also missed.

## INFO
- The rows-0 anchor is a per-key constant, minted by any refused attempt while the chain is empty. A kept copy, a chain cut to one row or less and a rebuilt spent table reach a clean state. This is the documented "old anchor copy" class and needs DROP and CREATE of both tables plus a file write. Nothing cheaper exists.
- A missing anchor directory makes `_anchor_lock` raise out of `commit_api_launch`; nothing is spent, but `agent_control.py:2419` does not catch it. API path only. A subscription-only host never reaches it.

## Checked and sound
Order of `_chain_problem`, `_close_anchor_lag` and the proof check; lag seal; key outage with a rows-0 anchor; concurrent first launches; key rotation before the first launch; `reanchor` on an empty chain; L-E retry (`restored_triggers` is empty on retry, but the first audit record and printed JSON hold it); backslash lint.

## Status
M-A closed. M-B closed within the documented model. L-E closed. L-G partly (LOW-1, LOW-2).

## Handoff accuracy
Suite numbers match the logs. Claims 1-6 hold. Overstated: "sees through fd redirects", and the residual list lacked the LOW-1, LOW-2 and `<( )` forms.

## Another round
Not a full one: "the ledger code is sound after four rounds". A short diff read of the LOW-1 fix is enough.

## Not checked by the auditor
Mutants, the full suite, `SshHost`, the live host, sandbox DB state, two-process concurrency beyond the flock, `_api_proof_problem` and the network path (unchanged in this range), how `agent_resume` callers handle an exception from `commit_api_launch`.
