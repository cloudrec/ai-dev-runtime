# Deployment readiness — staged server fixes + Windows client skew (2026-08-29)

Everything below is verified. **Nothing is deployed, pushed, or restarted.**
Deployed line remains `ai-runtime/220-windows-bridge` @ `5618ce3`, local == remote.

## A. Windows client artifacts — verified

| Artifact | sha256 |
| --- | --- |
| `clients/windows/owner_os_agent.py` | `dd6434576bbfc58fd2acbd3a3efe67e78c1276add737fb5f86f45f7f0d8d947a` |
| `clients/windows/install.ps1` | `4e06c222643adf450f4d00845c3edbca38f6960d6c7c2b732380bb8a81b88586` |

* `owner_os_agent.py` compiles (`py_compile` clean).
* **stdlib-only claim holds** — every import is stdlib (`secrets`, `typing`,
  `urllib.parse` included). No third-party dependency.
* `install.ps1` passes the repo's own PowerShell lint (19 tests).
* Full `tests/test_windows_client.py`: **75 passed** on the deployed tree,
  including the server/device action-contract tests.

**Signatures: not applicable.** `install.ps1` sources the agent from
`$PSScriptRoot` via `Copy-Item` (line 132-135) — there is no download, so there
is no fetch to checksum or signature to verify. The hashes above are the
provenance record for whatever the owner copies to the PC. The installer never
echoes the enrollment code or the device secret.

## B. Client version skew — LIVE, and it is already causing failures

| | Version |
| --- | --- |
| Repo client (`AGENT_VERSION`, line 56) | **0.2.0** |
| Enrolled device `win-92840f98d82ad3fe` (DESKTOP-HI6L6AD) | **0.1.0** |

The device has been offline since 2026-08-27T23:27Z. It runs a client built
before `workspace.inspect` existed, which is exactly why three commands failed on
2026-08-27 with `unsupported action 'workspace.inspect'`.

**Consequence, today:** the server's allowlist advertises 7 actions, but that
device can only execute 6. Any `workspace.inspect` issued to it fails after a
full round trip — regardless of anything in the staged branches. The in-tree
contract test proves server and client agree *in the repo*; it cannot prove what
version the PC is running.

Fixing this is an **owner action on the Windows machine**, not a server deploy:
re-run `install.ps1` on the PC with a freshly minted enrollment code (codes
expire in 15 minutes). It is independent of decision C below.

## C. Server-side deploy — exact steps and targets

Target branch `ai-runtime/220-windows-bridge` (the deployed line; NOT `main`).
`ai-runtime.service` has `WorkingDirectory=/root/ai-dev-runtime`, so the checkout
IS the deploy and the restart activates it. Service listens on `172.17.0.1:8199`;
`api/v1` authenticates on `RUNTIME_TOKEN` (not `AI_RUNTIME_API_KEY`).

Staged branches, all local-only (`pushed=0` verified on every one):

| # | Branch | Head | Effect |
| --- | --- | --- | --- |
| 1 | `fix/test-step-process-group` | `d7749a9` | test-step timeout reaps its process group (`job_executor`, `deliver`) |
| 2 | `feat/salvage-observability` | `80d66d5` | a salvaged plan is logged + recorded, not inferred |
| 3 | `fix/windows-late-result-after-expiry` | `8e50ae1` | carries `62df2dc`; both Windows command-lifecycle fixes |
| 4 | `docs/staged-integration-probe` | this branch | docs only |

Steps, if authorized: cut a fresh pre-deploy point; merge the chosen subset into
`ai-runtime/220-windows-bridge`; `git diff` over `core/ api/ cli/ clients/ tests/`
must be empty against the tested tree; push; `systemctl restart
ai-runtime.service`; verify `/api/v1/health`, `/windows/policy`,
`/windows/devices`, `/fabric/agents`.

## D. Readiness evidence

* All four merge clean in any order (throwaway detached worktree, no ref moved).
* **Combined full suite: 2565 passed, 0 failed.**
* Every fix mutation-verified: reverting it fails its own test.
* No change in any branch to credentials, schema, wire protocol, status values,
  `configs/.env`, or validation policy.

## E. Rollback

Pre-existing point: tag `rollback/pre-planner-salvage-20260829T162442Z` ->
`b30ebf8`, plus `backups/predeploy_planner_salvage_20260829T162442Z/` (4 db
copies + `ROLLBACK.md`). A fresh point would be cut before any new deploy.

Every staged change is confined to four files (`ai_planner.py`, `job_executor.py`,
`deliver.py`, `windows_bridge.py`) with **no schema or config change**, so
rollback is: restore the affected files and restart. Restore files rather than
`git reset --hard`, which would discard the 29 unrelated dirty `reports/*`.

Client rollback is independent: reinstall the previous `owner_os_agent.py` on the
PC. Enrollment survives — device identity is in `agent.json`, not the script.

## F. Not in scope / still withdrawn

* `RUNTIME_TEST_TIMEOUT` 600 -> 1200 stays **withdrawn**. Five timing samples of
  the same suite: 742 / 832 / 1117 / 1171 / 1622s. A 1200s cap would have been
  blown twice over.
* Scoping `ai_planner.default_test_commands()` — the only non-degrading fix — is
  an owner policy decision, untouched.
* Telegram dead-letter / `notifications_red` remains credential-gated, untouched.
* Runtime jobs 84 and 86 are already satisfied and were not retried.

## The decision required

Authorize a specific subset of #1-#4 for merge + push + restart of
`ai-runtime.service` (6 workers; 0 in-flight jobs at last check) — and,
separately, decide whether to update the Windows PC from client 0.1.0 to 0.2.0.
The two are independent; neither has been done.
