# Owner OS — automated writes into the payment agent

**Status:** diagnosed, built, tested, **not applied.** Awaiting one directly
owner-authored line.
**Date:** 2026-09-16 · **Repo:** `/root/ai-dev-runtime`

---

## 1. Root cause: the ChatGPT wake path, not the native supervisor

The obvious suspect is wrong. `native_supervisor` sent **127 messages in 48 hours and zero
to `payorch-ha-fresh:0.0`** — `payment-orchestrator` is on the live denylist and that door
is already shut.

Every delivery to the payment agent — **116 of 116** — is:

```
actor  = api:bearer
source = 172.20.0.4  ua=python-httpx/0.27.0
```

Split by action over 48h: **67 `agent_send`** and **142 `agent_answer`**. The answer path is
the larger and less obvious door: it resolves permission prompts, so automation is granting
the payment agent permissions, not merely continuing it.

The chain is: an event mints a wake → the companion delivers it into a ChatGPT
conversation → that conversation calls back through `/agents/send` and `/agents/answer`.
**The wake is the cause of the write.**

### Why the API cannot be filtered instead

`api/v1.py:79` mints `actor = f"api:{auth_method}"` plus an optional caller-declared name.
One endpoint each for send and answer, one auth method in use, one source address. The only
field that could separate an automated continuation from the owner's own command is
`X-Runtime-Actor` — a header the caller sets about itself, absent from every recorded
delivery, and documented three lines above its own use as *"Self-declared and therefore NOT
trustworthy for any decision."*

A guard keyed on it is defeated by setting it. A guard on `api:bearer` blocks the owner
too. So the lever is the wake, and only the wake.

---

## 2. The dual identity — why a one-line rule would have been wrong

This pane's events arrive under **two** identities. Over seven days:

| identity | events | actionable wakes |
|---|---|---|
| `payorch-ha-fresh:0.0` | 398 | 342 |
| `session:6db4919a-460…` | 1031 | 10 |

A rule matching the tmux target alone leaves the larger half waking normally while the
audit claims the agent is suppressed.

The guard bridges the two through the `agent_identity_source` sidecar, and **only** where
`source='runtime'`. The cwd fallback hands several panes in one directory the same session
id — `hostsecure-clean:0.0` demonstrably carries another pane's — so a guessed link would
silence a *different* agent's alerts. Suppression is fail-closed for automation and
fail-**open** for the owner's visibility, so it must not rest on a guess.

---

## 3. The design

In `core/wake_bridge.py`, at `should_wake` — the earliest point where exact agent identity
is available, after the kill-switch and disabled checks so those still answer first:

```python
WAKE_SUPPRESSED_AGENTS = {"payorch-ha-fresh:0.0"} | {env additions}

if agent_wake_suppressed(agent_id, conn=conn):
    return {"wake": False, "reason": "agent_wake_suppressed",
            "actionable": is_actionable(event_type), "suppressed_agent": agent_id}
```

* **Union, never replacement.** `os.getenv(NAME, default)` is how `diamond` silently left
  the live denylist while the commit adding it sat in the tree looking effective. Env may
  add here; it cannot remove.
* **Auditable, not silent.** `record()` writes every refusal to `wake_audit` as
  `decision='skip'` with reason `agent_wake_suppressed`, the agent named, and the
  `actionable` class preserved — it *was* actionable and was refused anyway.
* **Notifications preserved.** `cto.emit` enqueues the owner notification (`cto.py:51`)
  **before** it consults `should_wake` (`cto.py:78`). The 525 notifications this agent
  produced in seven days are unaffected.
* **Manual MCP send untouched.** `agent_send` / `agent_answer` never pass through the wake
  bridge. Pinned by a test that reads `agent_control`'s source and fails if it ever imports
  the rule.

---

## 4. Artifacts (durable, in this repo)

| what | where |
|---|---|
| wake suppression + its tests | `reports/artifacts/payorch-wake-suppression.patch` |
| unknown-identity routing fix + tests | `reports/artifacts/unidentified-project-fails-closed.patch` |

Both verified with `git apply --check` against this tree. Apply with
`git apply <path>`. They were previously only in an ephemeral session scratchpad.

---

## 5. Verification

* **20 new cases** — `tests/test_wake_suppressed_agents.py` — covering: suppressed under
  both identities; no suppression without a runtime-sourced link; a guessed identity never
  suppresses; an empty or short session ref cannot match everything; other agents untouched;
  another agent in the *same project* still wakes (with a route bound, or the assertion
  proves nothing); the refusal audited under its own reason; env adds but cannot remove; an
  unreadable sidecar does not raise into the bridge; kill-switch and disabled still answer
  first.
* **221 adjacent cases** green in the patched copy: `test_wake_bridge`,
  `test_wake_assistant_proof`, `test_rebind_chat`, `test_owneros_hook`,
  `test_owner_os_adversarial`.
* Full repo suite before these changes: **3325 passed**.

Two defects the tests caught in the first draft, both of which would have shipped silently:
`_suppressed_conversation_ids` caught only the `SELECT` while `_conn()` also raises on an
unreadable store, which would have taken the wake bridge down with the sidecar; and the
control test asserted a wake against an empty routes table, so it was really observing
`no_route_bound` and would have passed just as happily with a project-wide block.

---

## 6. Not applied, and why

`wake_bridge` is reached from `cto.emit`, which every hook calls in a fresh interpreter
importing the working tree. There is no deploy step: **the next hook event runs it.** No
restart activates it and no restart undoes it — `git checkout -- core/` restores the code
but not the decisions taken in between. That is why this waits.

Measured effect if applied: the **518 wakes** this agent produced in seven days become
audited `agent_wake_suppressed` refusals.

---

## 7. A separate provenance defect, found while looking for gate evidence

Independent of the above and worth its own decision.

* **`owner_api` and automation share one secret.** `_auth` (`api/v1.py:35-54`) accepts an
  HMAC over a timestamp *or* a bearer token, and both are `RUNTIME_TOKEN`. The HMAC path is
  the same secret used as a signing key rather than a password, so anyone holding the
  bearer token can compute a valid signature. `POST /control-plane/owner/decision` then
  writes `authenticated=True`, `source_channel="owner_api"` — the one channel
  `provenance.TRUSTED_CHANNELS` accepts — so the automated channel can mint a `trusted`
  owner decision for any open gate. `actor="owner:hmac"` is a durable label, not stronger
  authority.
* **Reported tasks are not backed by this control plane.** `POST /agents/orchestrator/task`
  writes to `agent_control.db → orchestrator_task` (3 rows, ids 1–3, all completed), not to
  `control_plane.db → os_task` (11 rows, all `cp-canary:0.0`, dormant since 2026-08-06).
  It also calls `get_active_goal()` first and raises `400 "no active goal"` — the single
  `orchestrator_goal` row has been `complete` since 2026-08-08 — so no task can be created
  at all right now. Tasks 302, 303 and 304 do not exist in any store, and `approve_task`
  exists nowhere in the codebase.
* Even fully repaired, task creation and task-status pass the same bearer dependency as
  everything else: a caller that can create a task can approve it. An approved task is
  provenance the automated channel issues to itself.

---

## 8. Exact next safe step

One line, written by the owner directly, naming the change — for example
*«ставь подавление wake»*. On it:

```
baseline   git rev-parse HEAD                 # tree clean
apply      git apply reports/artifacts/payorch-wake-suppression.patch
verify     pytest tests/test_wake_suppressed_agents.py tests/test_wake_bridge.py \
                  tests/test_owneros_hook.py tests/test_owner_os_adversarial.py
observe    wake_audit rows for payorch-ha-fresh:0.0 and session:6db4919a-460…
           must read decision='skip', reason='agent_wake_suppressed'
rollback   git checkout -- core/wake_bridge.py
```

Effective immediately on the next hook event; no restart, no deploy, no push.
