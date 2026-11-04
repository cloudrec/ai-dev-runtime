# Stage 15 final fixes handoff, 2026-11-04

Written by the Opus 5.5 session. Not an independent audit.
**Fable reviewed everything up to `acee756`: sandbox E2E yes with conditions. `a0b6409..ddc75f3` (M-B, L-E, L-G) has NOT been reviewed by Fable. PROD: NO-GO. Nothing deployed.**

## Branch
`owneros/stage15-final-fixes` @ `ddc75f3`, off `owneros/stage15-review-fixes` @ `acdeb95`. API mode stays `enabled: false`. No live DB, secret, service, paid call or `claude` launch was touched.

## Evidence
- Full suite at `ddc75f3`: 4690 passed, 4 skipped, 0 failed, exit 0, 661 s (`reports/stage15/full_suite_ddc75f3.txt`).
- Earlier on this branch: `a15dd26` 4651 passed, `acee756` 4676 passed, 0 failed each.
- Mutants, in a disposable repo archive outside the repo, guard code never mutated: 15 at `a15dd26`, 14 at `acee756`, 7 at `6150727`. One survived the last round: the rows-0 anchor check. `ddc75f3` adds its tests and 3 variants of that check were then caught. All others caught. Scoped to `tests/test_stage15_final_fixes.py`.

## Audit reports
- `reports/OWNER_OS_STAGE15_FABLE_REVIEW_FIXES_AUDIT_2026-11-04.md` (`05073f4..e96e72d`)
- `reports/OWNER_OS_STAGE15_FABLE_FINAL_FIXES_AUDIT_2026-11-04.md` (`acdeb95..a15dd26`)
- `reports/OWNER_OS_STAGE15_FABLE_LATEST_FIXES_AUDIT_2026-11-04.md` (`a15dd26..acee756`)
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
- `_runs_claude` misses fall to the denylist: `env -S`, `coproc NAME { claude; }`, substitution inside double quotes, indirect children such as pytest calling `claude`.
- The launcher check is a lint on specs. A glob, a spliced variable or an indirect call gets past it.
- `ac.audit` is best-effort; the tool's printed JSON is the owner's record.
- F5 stays open until clients move. Replay cache and counters are per process.
- `connect()`, `commit_api_launch` and `_chain` still commit a caller's open work. No live caller passes a connection there.
- Real `claude` binary never run with the allowlist.

## Conditions for sandbox E2E
1. Keep `AGENT_AUTH_SETTINGS_GUARD` unset unless the project settings files are owner-written.
2. Read `restored_triggers` in any reanchor output.
3. Have Fable review `a0b6409..ddc75f3`, or accept that range unreviewed.

## Owner gates
- Sandbox token and live E2E setup.
- Real `claude` smoke with the allowlist, as part of a canary.
- Whether a live spec names the launcher: needs a read of `control_plane.db` or a copy.
- `RUNTIME_HMAC_LEGACY=warn` in PROD to find off-host clients.
- Anything paid, any deploy, `enabled: true`.

## Rollback
Branch only. `git revert <sha>`, or delete the branch. If deployed and M1 or M-A must go back: triggers and anchor are additive, as in the review-fixes handoff.
