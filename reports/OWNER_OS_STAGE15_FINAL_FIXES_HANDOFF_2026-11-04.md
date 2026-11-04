# Stage 15 final fixes handoff, 2026-11-04

Written by the Opus 5.5 session. Not an independent audit.
**Every code commit up to `7559ed2` has been read by Fable: sandbox E2E yes under the conditions below, nothing blocking. PROD: NO-GO. Nothing deployed.**

## Branch
`owneros/stage15-final-fixes` @ `7559ed2`, off `owneros/stage15-review-fixes` @ `acdeb95`. API mode stays `enabled: false`. No live DB, secret, service, paid call or `claude` launch was touched.

## Evidence
- Full suite at `7559ed2`: 4707 passed, 4 skipped, 5 warnings in 865.12s (0:14:25) (`reports/stage15/full_suite_7559ed2.txt`, exit 0).
- Earlier on this branch: `a15dd26` 4651 passed, `acee756` 4676 passed, `ddc75f3` 4690 passed, `294e3c1` 4703 passed, 0 failed each.
- Mutants, in a disposable repo archive outside the repo, guard code never mutated: 15 at `a15dd26`, 14 at `acee756`, 7 at `6150727`, 6 at `294e3c1`, 1 at `7559ed2`. One survived: the rows-0 anchor check. `ddc75f3` adds its tests and 3 variants of that check were then caught. All others caught. Scoped to `tests/test_stage15_final_fixes.py`.

## Real claude smoke
Run on the host at `7559ed2`, on the owner's instruction: `claude` 2.1.318 through `LocalHost.run`, in a temp dir, `-p`, one turn, Sonnet, no session persisted.
- Parent env held sentinel `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` pointing at a closed local port. The child env had no `ANTHROPIC_*` name, 12 variables in all.
- rc 0, `result: OK`, `apiKeySource: none`: the allowlist is enough for real subscription auth.
- One live subscription request. Not a canary: no daemon, pane launch or fullcycle task was exercised.
- The CLI waited 3 s on stdin because `LocalHost.run` leaves it inherited when no input is given. Delay only.

## Audit reports
- `reports/OWNER_OS_STAGE15_FABLE_REVIEW_FIXES_AUDIT_2026-11-04.md` (`05073f4..e96e72d`)
- `reports/OWNER_OS_STAGE15_FABLE_FINAL_FIXES_AUDIT_2026-11-04.md` (`acdeb95..a15dd26`)
- `reports/OWNER_OS_STAGE15_FABLE_LATEST_FIXES_AUDIT_2026-11-04.md` (`a15dd26..acee756`)
- `reports/OWNER_OS_STAGE15_FABLE_MB_FIXES_AUDIT_2026-11-04.md` (`acee756..ddc75f3`)
- `reports/OWNER_OS_STAGE15_FABLE_294E3C1_READ_2026-11-04.md` (`294e3c1`, short read)
- `7559ed2`: short read, sound with notes, no separate report; 118 strings compared with `294e3c1`, 6 changed, all old-wrong to new-right
The model of each is a self-report, and billing for those runs was not checked.

## What changed
| finding | commit | change |
|---|---|---|
| M1 | `7301508` | routine proof use no longer repairs the ledger once a chain exists; `reanchor` is the only repair |
| L1 | `9c19f22` | no settings content can stop a launch |
| L3 | `7ea532a` | a lagging anchor row is closed at the next attempt, even a refused one |
| L2, L-B, L-G | `395d892`, `1f11a3d`, `a0b6409` | `_runs_claude` tokenizes shell strings, walks wrappers linearly, sees through fd redirects |
| L4 | `66ff685` | a decoded `?` stays in the signed path without `raw_path` |
| L5, L-C | `e699b55`, `acee756`, `a0b6409` | launcher matched as a whole word, plus its own path spellings |
| F7 gap | `2998dd9` | fullcycle `claude` children are reported, refused under `enforce` |
| M-A | `a401430` | `reanchor` needs the anchor to vouch for the chain, or `--accept-chain`; audited |
| M-B | `6150727`, `ddc75f3` | a rows-0 anchor exists before the first row, so no file under a row means deletion |
| L-A, L-D, L-E | `a401430`, `6150727` | no keyless anchor; old ledgers with no chain get triggers; a failed write is reported and audited |
| LOW-1, LOW-2 | `294e3c1`, `7559ed2` | a redirect's fd is removed before lexing, only when adjacent (LOW-1 was a regression from `a0b6409`); glued operators and `<(` are read; a missing anchor directory is a reason code, not an exception |
| INFO | `a15dd26` | acceptance guard covers unset DB variables and spawns outside Popen; proxy note in the HMAC doc |

Departures from the auditor's suggestions, on purpose:
- M1: with a chain present a missing table comes back bare, else `DROP TABLE` would self-repair.
- M-A: any anchor that does not vouch needs the flag, not only a shorter chain.
- M-B: the rows-0 anchor keeps the self-healing `len(hashes) > 0` alone would have lost.

## Changed behaviour
- A key rotation now needs `tools/owner_proof.py reanchor --accept-chain`. Run a plain `reanchor` first and keep its `rows`.
- A DB with a chain row and no anchor needs `--accept-chain`.
- The first API attempt writes the rows-0 anchor; if it cannot, nothing is spent and nothing starts.
- `reanchor` output has more fields, and the tool's docstring says how to read them.
- `command -v claude` and a redirect target named `claude` no longer count as running it.

## Residual risks
- Root agents remain outside these controls: see the `core/agent_auth_mode.py` docstring. Newly stated: an old copy of the anchor put back with a chain cut to match it is not detected.
- Lag window: a row after a failed anchor write is exposed until the next attempt.
- Settings guard is report-only by default, not run for `recovery_command` or `SshHost`.
- `_runs_claude` misses fall to the denylist and skip the F7 report: `env -S`, `coproc NAME { claude; }`, substitution inside double quotes, indirect children such as pytest calling `claude`.
- False positive, harmless for credentials: a quoted word made only of operator characters reads as an operator, as in `echo '|' claude`. The child gets the allowlist env.
- The rows-0 anchor is a per-key constant. Together with a kept copy, a chain cut to one row or less and a rebuilt spent table, it reaches a clean state: the documented old-anchor class.
- The launcher check is a lint on specs. A glob, a spliced variable or an indirect call gets past it.
- `ac.audit` is best-effort; the tool's printed JSON is the owner's record.
- F5 stays open until clients move. Replay cache and counters are per process.
- `connect()`, `commit_api_launch` and `_chain` still commit a caller's open work. No live caller passes a connection there.

## Conditions for sandbox E2E
1. Keep `AGENT_AUTH_SETTINGS_GUARD` unset unless the project settings files are owner-written.
2. Read `restored_triggers` in any reanchor output.

## Owner gates
- Sandbox token and live E2E setup.
- Whether a live spec names the launcher: unknown. A read-only scan of `control_plane.db` was refused by the session's auto-mode classifier as a production read, and was not worked around.
- `stage_done` refuses this pane: `owneros-fable-audit` is not on the autonomy list. Stages after `c9a0d92` were not delivered to the tech lead.
- `RUNTIME_HMAC_LEGACY=warn` in PROD to find off-host clients.
- Anything paid, any deploy, `enabled: true`.

## Rollback
Branch only. `git revert <sha>`, or delete the branch. If deployed and M1 or M-A must go back: triggers and anchor are additive, as in the review-fixes handoff.
