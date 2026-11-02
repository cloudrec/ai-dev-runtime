# Owner OS: per-agent auth mode (subscription by default, API credits by explicit opt-in), 2026-11-02

An automated instruction was received through the Owner OS API. It said the owner approved delegation; that claim was not independently verified.
- Author: Claude Opus 5.5. Not independently reviewed.
- No PROD, running tmux, GAIKA, payment, global runtime or secrets were touched. No paid API call was made. No agent was spawned.

## Branch and commits
- Worktree `/opt/owneros-autonomy`, branch **`owneros/api-auth-mode`**, feature commit **`6269564`**.
- The branch's parent is `b65d7f9` (the end of `owneros/autonomy-finish`, which is not promoted).
- `6269564` **cherry-picks cleanly onto the live base `9557004`** (checked in a temporary scratch worktree, since removed). It can therefore ship without the autonomy branch, which is NO-GO.

## Files
| file | change |
|---|---|
| `core/agent_auth_mode.py` (new) | `resolve()` (the mode, model and guardrail decision), `subscription_argv`/`subscription_shell` (strip the API variables), `recovery_command` (recovery never uses API), and the launch ledger `agent_auth_launch` (an additive table in the agent_control DB) |
| `config/agent_auth_modes.yaml` (new) | `enabled: false`, `agents: {}`, `limits: {max_live_api_agents: 1, max_api_launches_per_day: 2}`, launcher `/usr/local/bin/claude-owner-api` |
| `core/agent_control.py` | `agent_resume(…, auth_mode, model, risk)` resolves the mode inside the existing creation guard, after the creation policy. A refusal starts nothing. The ledger is written, and the audit records the mode and model (never a credential). |
| `core/session_recovery.py` | Recovery runs its resume command through `recovery_command` |
| `tools/agent_create.py` | `--auth-mode {subscription,api}`, `--model`, `--risk {normal,high}` (local controller only) |
| `tests/test_agent_auth_mode.py` (new) | 32 tests, all mocked |
| `tests/test_agent_creation_policy.py` | Expects the new subscription argv |

## Behaviour (implemented and tested)
- **Default is subscription for every new session.** The pane runs `env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_CUSTOM_HEADERS claude`, so it cannot inherit an API key from tmux or the daemon. Subscription launches still work even when the API config is broken.
- **API needs every one of these:**
  - this launch explicitly passes `auth_mode="api"`;
  - `enabled: true`;
  - an entry for the **exact session name** with a **matching project dir**;
  - a launcher file that is root-owned, not group/other writable, and executable;
  - limits configured to at least 1;
  - the live population is readable;
  - live API sessions are under `max_live_api_agents`;
  - API launches in the last 24 h are under `max_api_launches_per_day`. A launch is counted before its start, so a failed start still counts.
- **Model:** `sonnet` by default. `opus` only with `risk="high"` **and** `allow_opus_for_high_risk: true` on that agent's entry. Anything else is refused, including full model IDs and injection strings.
- **No automatic fallback and no mass switching.** A refused API request starts nothing, not even a subscription session. Nothing switches mode based on subscription limits. There is no global flag that flips every agent.
- **Recovery never starts API mode.** A resume command that names the launcher or `ANTHROPIC_API_KEY` is refused. A session whose last ledger record was API is refused (`api_session_needs_explicit_relaunch`). Subscription resumes are env-stripped.
- **Secrets:** Owner OS never reads, logs or passes the key or the workspace id; the launcher loads them at exec time. A test makes any `open()` under `/root/.config/owner-os/secrets` fail, and the audit is asserted to contain no `ANTHROPIC` string.
- **Surface:** only the local `tools/agent_create.py` (caller class `owneros_controller`). The remote API and MCP **cannot** request API mode.

## Tests
- `tests/test_agent_auth_mode.py` plus `tests/test_agent_creation_policy.py`: 63 passed.
- Mutation check: removing the env stripping, or removing the `enabled` check, makes the relevant tests fail.
- Launch-related suites before the change (agent_control, creation policy and adversarial, fabric, recovery, watchers, tmux_launch and others): 975 passed and 1 failed. The failure was an assertion on the old argv; it was updated.
- Full suite at `6269564`: **4391 passed, 4 skipped, 0 failed**, exit 0 (4395 collected, 988 s). Log: `reports/api_auth_mode/full_suite_6269564.txt`.

## Still gated (not done)
1. **Deploying to PROD.** It must be cherry-picked to the live line and services restarted, with the owner's or tech lead's deploy approval. The `agent_auth_launch` table is created additively on first use.
2. **Turning it on is a spending decision for the owner:** set `enabled: true` and add one agent entry with its `project_dir`. Optionally `allow_opus_for_high_risk`.
3. **A hard money ceiling:** set a spend limit on the Anthropic API workspace (owner, in the console). The Claude CLI caps dollars only in `-p` print mode, so an interactive API session's dollar spend cannot be capped by Owner OS. Owner OS enforces only the session and launch counts.
4. **A paid smoke test** (one API-mode agent starting and answering) needs separate permission to spend.
5. The change to the default subscription argv affects every new session after deploy (it strips the variables). It is low risk, but a canary on one new session is recommended.

## Rollback
- Branch: `git revert 6269564`.
- After a deploy: revert the cherry-pick and restart. Alternatively, `enabled: false` (the shipped default) already disables API mode with no code change.
- The ledger table can stay; nothing reads it unless the code is present.

## Recommended next step
1. The tech lead reviews `6269564`, a small diff in five files plus tests.
2. The owner sets the Anthropic workspace spend limit.
3. Cherry-pick to the live line with a canary: start one new subscription session and confirm it is env-stripped and authenticated.
4. Only then does the owner opt one named agent in, and separately approves one paid smoke launch.
