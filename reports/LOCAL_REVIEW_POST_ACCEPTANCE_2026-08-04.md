# LOCAL REVIEW — Commander watcher after acceptance checkpoint `7ad6b72`

**2026-08-04.** Independent local review of the deployed work: code vs. report vs. tests vs.
git diff, focused on test non-vacuity, fail-closed behaviour for unreadable/dialog panes,
idempotency + delivery attribution, rotation fencing, and the owner-gated poke prohibition.
Reviewed by Opus (Fable spend remains stopped).

**No live contact:** no pane or agent touched, no production tick or actuation run, no
restart/deploy, no unit/env change, no live DB write. Every probe used fake controllers and
throwaway DBs under `/tmp`. Nothing pushed.

## Verdict

One **HIGH** defect found and fixed. The other four review areas held under adversarial
probing.

## HIGH — the per-project owner gate was decorative

`config/commander_autopilot.yaml` documents, per project:

```yaml
  payment:0.0:
    live_actuation: false          # owner gate + payment execution must not be touched
```

`evaluate()` parsed that flag into its assessment (`core/commander_autopilot.py:136`) — and
**nothing ever read it when deciding to actuate**. `deliver_next_step` gated only on
`classify_safety(step_text)` and then handed off to the Actuator, whose confinement is the
env allowlist `CONTROL_PLANE_CANARY_AGENTS`. Two gates were documented; **one existed**.

That matters because widening the allowlist is a one-line systemd drop-in edit — exactly
what the acceptance run performed for the canary. Anyone repeating that step for another
agent would have actuated it while the registry still recorded the owner's "not approved".

**Proven on the acceptance checkpoint `7ad6b72`** (worktree, import origin asserted, fake
controller, `/tmp` DBs — no live contact):

```
shipped registry payment live_actuation: False
PRE-FIX RESULT: {'acted': True, 'verified': True, 'reason': None} | keystrokes: 1
```

With the allowlist widened to `payment:0.0`, the pre-fix code **delivered a keystroke to
payment** despite its own registry withholding permission.

**Fix** (`core/commander_autopilot.py:232-243`): `deliver_next_step` now requires BOTH
gates. The registry check runs only for targets **inside** the allowlist, so the
non-canary path keeps returning the Actuator's `not_canary` refusal unchanged and the
acceptance-era evidence still holds; an allowlisted target missing from the registry is
denied by default. `tick()` passes its registry through.

## Areas that held

**Fail-closed for unreadable / dialog panes.** Re-probed the whole chain: dialog signature
(denylist + structural), `classify_state → waiting_owner`, watchdog
`dialog_open_never_auto_answer`, autopilot `skip_dialog_open`, actuator `dialog_open`,
rotation `permission_dialog_open`; and for blind panes `pane_capture → (False, "")`,
watchdog/autopilot/rotation `unobservable_pane`, actuator `capture_failed` /
`empty_snapshot`. No path found that reaches a keystroke.

**Idempotency + delivery attribution.** `_deliver` is the only writer of `deliveries`;
both `agent_send` and `agent_answer` thread `actor`/`source`; the sidecar keeps
`deliveries` unchanged so the older build's positional INSERT still works; the TTL sweep
prunes attribution on the same retention; a cross-caller replay is audited without
overwriting the original. `caller_identity` degrades to `api:unknown` and sanitises the
self-declared name.

**Rotation fencing.** `/clear` and the resume step go only through `_actuate`, which
acquires a lease per call and routes to the canary-confined Actuator; the Actuator
re-checks false-idle, dialog and pending-input at act time, so a dialog appearing between
the phase check and the clear is still caught; one-rotation-per-conversation is durable via
`_already_rotated`; a missing or unverifiable checkpoint refuses.

**Test non-vacuity.** Spot-checked the recent suites against their own baselines; the new
tests here were proven behaviourally, not just by signature error (the pre-fix probe above
was run separately precisely because a `TypeError` on the new kwarg would have been weak
evidence).

## Tests — `tests/test_post_acceptance_review_gaps.py`, 8 tests

- Allowlisted-but-registry-gated `payment:0.0` and `mess-qa-automation:0.0` → refused
  `registry_live_actuation_disabled`, **zero keystrokes**
- Allowlisted target absent from the registry → denied by default
- Production `tick()` with a widened allowlist → evaluates, refuses, touches no pane
- Anti-overcorrection: the approved canary still actuates and verifies; a non-canary target
  still reports `not_canary` (acceptance evidence preserved); an unsafe step is still
  blocked first
- CI invariant over the **real shipped registry**: exactly one project
  (`cp-canary:0.0`) may be actuated live — a future edit flipping another to `true` fails
  loudly

**Full local suite: 1223 passed, 0 failed** (1215 before). No existing test or fixture
changed.

## Limitations

- The fix binds the registry flag only for allowlisted targets; a target that is neither
  allowlisted nor registered is still refused by the Actuator (`not_canary`), which is the
  pre-existing behaviour.
- Not deployed. The running service (PID 1538853, started 2026-08-04 09:56:19 CEST) is on
  `7ad6b72` code and therefore **still has the decorative-gate defect live**, with the
  autopilot enabled and the allowlist currently `cp-canary:0.0` only. The defect is
  exploitable only by widening that allowlist — which nothing does automatically.
- Review was static + probe-based; no live tick or actuation was run, per the standing stop
  order.
