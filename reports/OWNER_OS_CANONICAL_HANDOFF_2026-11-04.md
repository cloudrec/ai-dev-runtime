# Owner OS: canonical handoff for a clean session, 2026-11-04

An automated tech-lead instruction was received through the Owner OS API. It is not owner approval.
- Author: Claude Opus 5.5. This is the implementer's own review, **not independent**.
- **The Fable audit is PENDING. PROD: NO-GO. Live unattended E2E: NOT PROVEN.**
- No PROD writes, deploys, credential changes, paid API calls, agents or subagents. Nothing was cherry-picked to the live line, and nothing was switched to `enabled: true`.

**Start a new session from this file. Do not reread the old session.**

## Branches (worktree `/opt/owneros-autonomy`; live tree `/root/ai-dev-runtime` is at `9557004`, OFF LIMITS)
```
9557004 (live) ── … owneros/autonomy-finish  b65d7f9   Full Cycle hardening (Stages 3-14)
                      └─ owneros/api-auth-mode   426861c   per-agent auth mode (6269564) + ordering fix
                           └─ owneros/offline-acceptance  HEAD  offline acceptance harness (06313c6) + docs
```

| commit | what |
|---|---|
| `6269564` | Per-agent auth mode. Subscription is the default, with the API variables stripped; API credits only through explicit per-agent opt-in. Ships `enabled: false`. |
| `426861c` | **Fix found by this review:** the auth decision now comes before every tmux mutation. Previously a refused API request could still kill an all-dead zombie session. The regression test fails on `6269564`. |
| `06313c6` | `tests/test_offline_acceptance.py` (6 scenarios) and `tools/offline_acceptance.py` (writes SHA-pinned JSON/MD evidence) |

`6269564` cherry-picks cleanly onto `9557004`, checked in a scratch worktree on 2026-11-02. `426861c` has to go with it.

## Evidence
| what | result | path |
|---|---|---|
| Offline acceptance @06313c6 (clean tree) | **PASS 6/6**: A reject→fix→approve; B crash→retry; C duplicate wake/decision; D stall→answer→idle window→one resend; E owner gate stops the loop; F API mode off by default | `reports/offline_acceptance/acceptance_06313c6.{json,md}` |
| Harness mutation check | When the shared bearer token is allowed to act as the reviewer, 5 of the 6 scenarios fail | (this session; `api/v1.py` restored) |
| Auth-mode + creation/control/recovery suites @426861c | 232 passed | — |
| Full suite @06313c6 | **4398 passed, 4 skipped, 0 failed**, exit 0 (4402 collected, 909 s) | `reports/offline_acceptance/full_suite_06313c6.txt` |
| Full suite @6269564 | 4391 passed, 4 skipped, 0 failed | `reports/api_auth_mode/full_suite_6269564.txt` |

**Invariants the harness checks in every scenario:**
- Decisions come only from the `api:client/<id>` principal (the real `_auth` with test tokens).
- Every shared-token spoof is refused (403).
- The audit hash chain is valid.
- No `ANTHROPIC_*` credential variable appears in any event.
- **No SQLite file under `/root/ai-dev-runtime` is ever opened.** Every `sqlite3.connect` is recorded and checked. Live-DB mtimes are useless as a check, because PROD itself writes `agent_control.db` about every 20 s.

**Simulated:** the LLM agent, the ChatGPT page, the reviewer's judgment, and the clock in scenario D.

## Auth-mode review notes (`6269564`)
1. Fixed in `426861c`: the decision now comes before every tmux mutation.
2. Residual: an API session started **outside** Owner OS (someone running `claude-owner-api` by hand) is not counted against the caps. Detecting it would mean reading process environments, which contain the secret, so it was not done.
3. Residual: no dollar cap per interactive session. The CLI caps spend only in `-p` mode. **Owner: set the Anthropic workspace spend limit.**
4. Compat: every new session's argv becomes `env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_CUSTOM_HEADERS claude …`. The `exec` keeps the process name `claude`, so the detectors still work (the suites are green). A canary is still recommended.

## Owner gates (isolated, none taken)
- **Auth mode:** set the workspace spend limit; give deploy approval (cherry-pick `6269564` and `426861c`, then a canary of one new subscription session); opt in with `enabled: true` plus one agent entry; give separate permission for one paid smoke launch.
- **Live E2E:** approve one sandbox Claude agent; provide a ChatGPT reviewer path (log in a dedicated profile, using `CDP_PORT`, or register a sandbox MCP connector); issue a sandbox reviewer client token.
- **Full Cycle promotion:** back up `control_plane.db`; issue the reviewer client token for the PROD connector (until then every decision returns 403).

## Independent Fable audit packet
- Scope:
  - `git diff b65d7f9..426861c -- core tools config`: `core/agent_auth_mode.py`, `core/agent_control.py`, `core/session_recovery.py`, `tools/agent_create.py`, `config/agent_auth_modes.yaml`.
  - The Full Cycle scope in `reports/OWNER_OS_AUTONOMY_FABLE_REVIEW_BUNDLE_2026-11-01.md` (range up to `b9c8cec`).
- Critical tests: `tests/test_agent_auth_mode.py`, `tests/test_offline_acceptance.py`, `tests/test_fullcycle_review_fixes.py`, `tests/test_fullcycle_api.py`.
- Questions:
  - Can any path start an API-mode session without an explicit request, the opt-in entry and the caps?
  - Can a subscription session inherit a credential?
  - Can recovery revive an agent into API mode?
  - Is counting the ledger before the start correct under concurrent launches (the host-wide `creation_guard`)?
  - Is the harness's isolation guard sufficient?
- Commands, run only in the worktree:
  - `python3 -m pytest -q -p no:cacheprovider tests/test_agent_auth_mode.py tests/test_offline_acceptance.py`
  - `python3 tools/offline_acceptance.py --out /tmp/x`

## Rollback (branches only)
`git revert 06313c6 426861c 6269564` on the respective branches. `enabled: false` alone disables API mode without any code change.

## Next concrete step
1. The tech lead commissions the independent Fable audit using the packet above.
2. In parallel, the owner sets the Anthropic workspace spend limit.
3. Only after both: a deploy decision on `6269564` and `426861c`, with a canary.
