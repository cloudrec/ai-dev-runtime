# Stage 15 handoff for the next implementer, 2026-11-04

Read `reports/OWNER_OS_STAGE15_INDEPENDENT_PREAUDIT_2026-11-04.md` first. It is a Sonnet pre-audit. **Fable audit: PENDING. PROD: NO-GO.** Nothing was edited or deployed. Do not amend `426861c` / `06313c6`; add new commits.

Next DEV stage: GO on a branch off `owneros/offline-acceptance` (`53e7a2b`), only after the F1-F3 fixes below.

## Next concrete safe task (branch only, tests offline)
1. **F1:** make decide + audit and ack + audit one transaction in `core/fullcycle/engine.py`. Add a fault-injection test: raise inside the audit and assert the review stays `pending`.
2. **F2:** add a shared env-strip helper in `core/agent_auth_mode.py`. Use it in `core/ai_planner.py:312` and `core/native_sessions.py:52`. Add a test that spawns a fake `claude` and asserts a sentinel `ANTHROPIC_API_KEY` is absent.
3. **F3:** require a valid single-use `owner_proof` bound to `{session, project_dir, model}` for `mode == api` in `agent_resume`. Test: opt-in present but no proof is refused.
4. **F4/F7:** fail closed on recovery shapes that do not start with `claude`; extend the strip list.
5. **F6:** make the `sqlite3.connect` wrapper raise before connecting; make the acceptance credential check non-vacuous.
6. Run the focused suites, then the full suite once. Record it with the SHA. Then request the Fable audit with this report attached.

## Do not
Touch `/root/ai-dev-runtime`, the live `control_plane.db`, secrets, or `enabled: true`; start paid or API launches; deploy.

## Remediation plan for a fresh Opus session (priority order)
Branch: `owneros/stage15-remediation` off `owneros/stage15-preaudit` (the commit holding these reports). One commit per finding, so each can be reverted alone.

| order | finding | isolated regression test (offline, tmp DB / fake `claude`) |
|---|---|---|
| 1 | F1 | `test_decide_audit_atomic`: patch `_audit` to raise on `accepted`; assert review still `pending` and zero audit rows. Same for `ack_review`. Replay a spent key after reopen: refused |
| 2 | F2 | `test_every_claude_spawn_strips_api_env`: set sentinel `ANTHROPIC_API_KEY`, point `_CLAUDE` / `CLAUDE_BIN` at a fake script that echoes env; assert absent. Add a grep-style test failing on any new `claude` spawn without the helper |
| 3 | F3 | `test_api_launch_needs_owner_proof`: valid YAML opt-in, no / wrong / reused / expired proof, or proof for another session or dir → refused; valid single-use proof → allowed once. Ledger-row deletion must not reset caps (counter in append-only table) |
| 4 | F4 | table test over shapes (`cd x && claude`, `FOO=1 claude`, abs path, `bash -lc`) → refused or wrapped; plain `claude …` → wrapped |
| 5 | F5 | signed `ts\|method\|path\|body_sha`; replay on another path or body → 401; nonce reuse → 401 |
| 6 | F6 | wrapper raises before connect on a path under the live tree; sentinel key proves the strip in scenario F |
| 7 | F7 | extend `API_ENV_VARS`; test the stripped set against a spawned fake `claude` |

Gate each step: focused suite, then the full suite once at the end, evidence saved with the SHA. All tests use `tmp_path` DBs and `tests/conftest.py` isolation; never open `/root/ai-dev-runtime`.

## Rollback
Branch only, nothing deployed. Per-finding `git revert <sha>` on `owneros/stage15-remediation`; whole stage: delete the branch. F3 ships with `enabled: false` unchanged, so API mode stays off regardless. Any later deploy needs backup + canary + rollback plan per the 2026-11-04 canonical handoff.
