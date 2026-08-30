# PAYMENT ACCESS — seo-backend agent_notifier path (classification correction)

**2026-08-03.** Apply the proven payment access-blocker classification to the seo-backend
agent_notifier producer. Read-only tracing + an in-scope source-side fix. No credentials, no
external push, no live flag/cutover broadening, no destructive action, no Git push, payment
execution untouched.

## Producer trace (read-only)

- **Service:** `seo-backend-1`, image `seo-backend` — a **baked container** (Up 5 days). Editing
  `/opt/seo/backend/services/agent_notifier.py` on disk does NOT change the running process.
- **Repo:** `/opt/seo` on branch `feat/social-stage4-...`, remote `github.com/cloudrec/seo`
  (push prohibited; a separate repo from this one).
- **How it gets state:** `agent_notifier` imports `services.runtime_client`, which calls an
  **external ai-runtime service over HTTP** (`RUNTIME_URL` → `/agents`, `/agents/report`,
  orchestrator report). The notifier's `obs.state` == the state **ai-runtime reports**.
- **Notify trigger:** `derive_phase` emits an `OwnerEvent` when `state ∈ {working, waiting_owner,
  externally_blocked, completed, dead, stale}` and it changed. `externally_blocked` →
  `"blocked on an external dependency"` = the repeated payment "install keys" owner path.

## Boundary (why the seo file is NOT edited)

The seo-backend notifier **cannot be safely modified from this repo**: it runs from a baked
image on a separate no-push repo; making it effective would require an image **rebuild +
redeploy** (a deploy/cutover action, prohibited). Therefore the file was **not** edited.

**Because the notifier consumes ai-runtime's reported state, the fix is applied at the source**
— the state ai-runtime serves — which propagates to the notifier with no seo change, no rebuild,
no push.

## Fix (in this repo)

`core/control_plane/access_recovery.py::reported_state(agent, state, tail)` — pure:
- For a recovery agent (`payment:0.0`), an `externally_blocked` whose evidence is a recoverable
  **key/credential/user selection** issue (publickey, permission denied, credentials required,
  no identity, IdentityFile, could not resolve, unknown user, ssh/.ssh/host key) → **downgraded
  to `idle`** (not a notifiable state → the seo notifier stops emitting repeated "install keys"
  owner events).
- **Genuine vendor blocks** (quota / rate-limit / 429) → left as `externally_blocked` (owner still
  sees them).
- **Exhaustive absence/revocation** (all keys removed / access revoked / account disabled) → left
  as `externally_blocked` so it still escalates once.

Wired into `core/agent_control.py::agent_list` right after `classify_state` — scoped to recovery
agents, best-effort (never breaks inventory), pure/no side effects.

## Live evidence (read-only, confirms owner truth)

Payment's live pane is a Claude Code **tool-permission dialog** to run an internal recovery SSH
command **with an already-installed key**:
`ssh -o IdentitiesOnly=yes -i /root/.ssh/server2_deploy andy_admin@<host> 'hostname; uname; ...'`
— i.e. selecting the historical **IdentityFile / user / host**. Its task list includes
*"Correct false 'no SSH access' in DR + Git reports"*. This is exactly the **internal
connection-mapping / key-selection recovery** the owner described — keys installed, payment
recovering the right user/IdentityFile/alias. No owner credential gate.

## Verification

- Unit: `reported_state` — `publickey → ('idle', True)`; `quota → ('externally_blocked', False)`;
  `all keys removed/revoked → ('externally_blocked', False)`. `agent_list` integration test:
  a payment pane classify_state=externally_blocked is reported as `idle`.
- Relevant suite (access + pipeline + pinger + agent_control/state): green.
- **Full suite: 1023 passed, 0 failed.**
- Redeployed `ai-runtime` only; loops alive, `restart_safe`, `consistent`. Live checks pass.

## Limitations / exact boundary

1. **seo-backend code unchanged.** The correction reaches it only through the ai-runtime state
   feed. If the seo side has `RUNTIME_URL` unset, its notifier is `not_configured` and this has
   no effect there.
2. **Only `externally_blocked` is downgraded.** Payment's `waiting_owner` **tool-permission
   dialogs** (e.g. approving the recovery SSH command above) are **NOT** suppressed — that is
   payment's own execution/approval flow, which is out of safe scope (must not touch payment
   execution), and suppressing it could mask a genuine stall. If the owner wants those
   tool-permission notifications dampened too, that is a separate, explicit decision.
3. **Requires the ai-runtime redeploy** (done) to take effect for the live notifier.
4. No seo rebuild/redeploy, no Git push, no credentials, no flag/cutover change, no payment
   execution touched.

## Files

- `core/control_plane/access_recovery.py` (`reported_state` + `_SELECTION_EXTERNAL_RE`).
- `core/agent_control.py` (`agent_list` reclassification hook).
- `tests/test_access_recovery.py` (+6 tests).
