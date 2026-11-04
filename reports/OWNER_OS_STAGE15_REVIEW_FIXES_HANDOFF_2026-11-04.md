# Stage 15 review-fixes handoff (Opus 5.5 session), 2026-11-04

Requested by the user in this session after the Fable review. An automated instruction was also received; it is not owner approval.
**Implementer checks only: Fable has not reviewed these commits. PROD: NO-GO.** Nothing pushed or deployed. API mode stays `enabled: false`; legacy HMAC default unchanged.

## Branch
`owneros/stage15-review-fixes`, from `owneros/stage15-audit-fixes` @ `05073f4`.

| commit | item | change | default behaviour |
|---|---|---|---|
| `57e3bc0` | N4 | failed wake bridge rolls back its own open work | fixes a failure path only |
| `33b75bc` | N2 | `check_argv` refuses the API launcher name anywhere in argv, trusted or not | **changed:** such a check now fails |
| `8c8f438` | N3 | `LocalHost` sees `claude` behind `sh`, `env`, `timeout` and similar; every child loses `ANTHROPIC_*`, `CLAUDE_CODE_USE_*` | **changed:** a check needing those variables loses them |
| `8253ed4` | N1 | v2 signs `raw_path` and `query_string` from the ASGI scope | v2 only; no in-repo client |
| `47264ad` | R6 | `owner_proof_used` append-only; keyed head anchor file; `owner_proof.py reanchor` | **changed:** triggers appear on `owner_proof_used`; any UPDATE/DELETE there now fails |
| `b61f086` | R1 | allowlist keeps `DISABLE_AUTOUPDATER` and other non-credential switches; checked against `deploy/` and `tools/*.sh` | restores switches |
| `a3fe2e4`, `e96e72d` | F6 | harness refuses ATTACH of a live or unknown path, and children pointed at a live DB; guard tests use a tmp stand-in | tests only |
| `46d8b77` | F7 | settings override keys recorded in the audit, names only; `AGENT_AUTH_SETTINGS_GUARD=enforce` refuses | **changed:** settings files are read at each launch; refusal only when enforced |
| `a07ef5f` | F5 | `RUNTIME_HMAC_LEGACY=warn` logs legacy use; contract in `docs/OWNER_OS_HMAC_V2_MIGRATION.md` | unchanged |

## Evidence
- Full suite at `e96e72d`: **4574 passed, 4 skipped, 0 failed, exit code 0**, 653 s. Log: `reports/stage15/full_suite_e96e72d.txt`. Delta from `2d762c4`: +72.
- An earlier run at `a07ef5f` had 3 failures (see Incident). Its log was deleted as invalid.
- Removing each fix fails its tests: 12 mutants, all caught (N1-N4, R1, R3 ack, R6 anchor and trigger, F5, F6 both, F7). Checked with single-file runs, not the full suite. N2, N4 and F7 were checked in part only: the guard line, not every path.

## Incident
The F6 mutant removed the ATTACH guard, so its test really ran `ATTACH` on `/root/ai-dev-runtime/__offline_guard_never_exists__.db`, creating an empty file in the live tree at 09:41:11. This broke the no-touch rule for that directory. No live DB or service was involved. The file (0 bytes, untracked) was deleted, which was also a write there, and absence was confirmed after the final run. `e96e72d` stops these tests from reaching the live tree.

## Residual risks
- Root agents remain outside these controls: see the security model in the `core/agent_auth_mode.py` docstring. Wiping chain, spent rows and anchor together looks like a fresh install.
- `recovery_command` does not run the settings check, and F7 enforcement is off by default.
- Wrappers `_runs_claude` cannot see through get the variable strip, not the allowlist.
- `connect()`, `commit_api_launch` and `_chain` still commit a caller's open work. No live caller passes a connection there.
- F5 stays open until clients move. Replay cache and counters are per process.
- Real `claude` binary still never run with the allowlist.

## Questions only the live host can answer
1. Does the daemon's real environment set anything outside `SUBSCRIPTION_ENV_KEEP` that the CLI needs?
2. Do live settings files contain `apiKeyHelper` or API variables? The new audit field will show this after deploy.
3. Does any trusted spec check depend on `ANTHROPIC_*` variables?
4. Do external HMAC clients exist? Only `warn` mode in PROD can show that.
5. How many API workers run? The replay cache assumes one.
6. Do the triggers and chain migrate cleanly on a copy of the live DB?

## Owner gates
- Sandbox token, and the rest of the live E2E setup.
- Anything paid, any deploy, and any change to `RUNTIME_HMAC_LEGACY` in PROD.

## Next review (Fable-ready)
Diff: `git diff 05073f4 e96e72d`. Earlier reports: `reports/OWNER_OS_STAGE15_FABLE_AUDIT_2026-11-04.md`, `reports/OWNER_OS_STAGE15_FABLE_REVIEW_AUDIT_FIXES_2026-11-04.md`. Look first at the anchor ordering and lock in `commit_api_launch`, then `_runs_claude`, then `_signed_target` behind a proxy.

## Rollback
Branch only. `git revert <sha>` per item, or delete the branch. If a deployed R6 must go back: the triggers and anchor are additive; drop the two `owner_proof_used_no_*` triggers and remove the `.api_anchor` file.
