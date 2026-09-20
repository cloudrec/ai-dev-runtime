# Event 55938 — native continuation exhausted on a parked, deny-listed auction agent

**Provenance:** an automated instruction was received asking why native continuation kept
retrying `diamond-auction:0.0` until exhaustion, control-plane side only. Nothing here is
owner sign-off. Diamond Auction was not touched: no send, no answer, no state change.

## Verdict

**Control-plane defect, fixed.** The supervisor was never allowed to type into that pane —
`auction` is in the running denylist and registration refused the target — but the send
predicate `is_supervised` read the denylist against the resolved project name alone
(`diamond`), not against every name the path can be filed under (`project_candidates`,
which registration already used). Under `NATIVE_SUPERVISOR_TARGETS=*` with the project
supplied, the predicate never consulted the registry, so a never-registered, deny-listed
agent was continued anyway — until the anti-loop cap gated it (event 55938, and 55184 the
day before).

Classification/suppression: no new mechanism is needed for this case. The denylist IS the
parking for a value-bearing project, and with the predicate fixed every pass records a
terminal `value_bearing_send_blocked` skip (lifecycle still observed, nothing typed, no
cap, no gate churn). For a non-denied agent that is deliberately parked, the existing
`mark_external_wait` declaration is the tool — it was used for exactly this agent on
2026-08-30 and expired 08-31.

## Evidence

- `native_supervised_target`: no row for diamond-auction (deregistered 09-17 `purge_stale`,
  project `diamond`); registration refuses it because candidates `{diamond, auction, opt}`
  intersect the denylist.
- Live env (companion pid 3508): `NATIVE_SUPERVISOR_DENY_PROJECTS=capacity,auction,payment-orchestrator,payorch,email,xmrig,mess`
  — overrides the code default, so the default's `diamond` entry (47243fc) is not in force;
  `auction` is, and only matches through the path segment.
- Reproduced on the live process: `is_supervised("diamond-auction:0.0", project="diamond")`
  → **True**; `send_block_reason(...)` → `value_bearing_send_blocked`. Both scan paths
  pass `project` from the cwd, neither passed the cwd.
- `native_supervision` journal for the target: 09-04…09-06 seven `value_bearing_send_blocked`
  skips (mapping then resolved to `auction`); 75c57d5 (09-14) made the mapping resolve to
  `diamond`; from 09-13/14 the idle sweep continued it 5×, then 09-18 6×, 09-19 6× → gate
  55184, 09-20 9× (+2 idle sweep) → gate 55938. **26 continuations typed into a parked
  auction agent, two exhaustion gates**, 6 h each.

## Fix (local commit)

`core/native_supervisor.py`
- `is_supervised(target, *, conn, project, cwd="")`: denial is `_denied_for(project, cwd)`
  — the same candidate-set test registration uses — instead of `project in DENY`.
- Both scan paths (event path and idle sweep) pass the agent's cwd.
- Registry fallback (no project supplied) reads candidates from the registered project
  AND registered cwd (`_registration_of`).

Live re-check after the patch, same process env: `is_supervised(..., project="diamond",
cwd="/opt/diamond/auction")` → **False**.

Full suite: 3353 passed. Tests (`tests/test_native_supervisor.py`), all three fail on the pre-fix tree:
- `test_the_predicate_reads_the_denylist_against_every_candidate_name`
- `test_under_the_wildcard_the_event_path_never_types_into_the_auction`
- `test_under_the_wildcard_the_idle_sweep_never_types_into_the_auction`

## What loads it

The companion (`owner-os-wake-companion`) runs `native_supervisor.scan`; the fix is inert
until it is restarted — an owner gate, not taken. Until then the 6 h gate from 55938
(until ≈21:43Z 09-20) suppresses continuation; after it clears, the pre-fix predicate will
resume typing into the pane on the next idle pass. Post-restart proof: the next
`native_supervision` row for `diamond-auction:0.0` reads `skip / value_bearing_send_blocked`,
and no further `continue` rows appear for it.

## Not touched
- `configs/.env` denylist (adding `diamond` would also work but is a config gate; the code
  fix makes it unnecessary).
- Diamond Auction pane, its external wait, its gate rows, all Auction state.
