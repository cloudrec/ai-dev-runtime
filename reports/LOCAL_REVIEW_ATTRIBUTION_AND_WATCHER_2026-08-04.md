# LOCAL INDEPENDENT REVIEW — delivery attribution + watcher

**2026-08-04.** Local review of the recent work: `f9c06ee` (fail-closed RU/EN dialogs),
`9fbb7f4` (M2 unobservable pane), `8e2b1ee` (M1 structural dialog), `0839ff3` (actuator
blind-pane guard), `5647b6d` (delivery attribution). Reviewed by Opus, not Fable — per the
owner's budget rule, mechanical verification does not spend Fable. No deploy, no restart,
no settings change, no external system touched, nothing pushed.

## Verdict

The reviewed commits do what they claim, and their own tests are non-vacuous (each round's
baseline proof was re-checked with the conftest trap defused). **Two real gaps survived
them**, both from the same failure mode: **a guard trusted a value it was handed instead of
reading the pane or the ledger itself.** Both are fixed here with tests that fail on
`5647b6d`.

## Gap 1 (HIGH) — rotation could `/clear` a dialog or an unreadable pane

`core/context_budget.py:100` `phase()` decided the rotation boundary from the
caller-supplied `state`:

```python
if state == "waiting_owner":
    reasons.append("permission_dialog_open")
```

Verified on `5647b6d`:

| input | pre-fix result |
|---|---|
| `phase("idle", "Продолжить? (да/нет)", "")` | `safe_boundary=True` |
| `phase("idle", "Do you want to proceed?\n❯ 1. Yes\n 2. No", "")` | `safe_boundary=True` |
| `phase("idle", "Allow this tool to run?\n> approve / deny", "")` | `safe_boundary=True` |
| `phase("idle", "", "")` | `safe_boundary=True` |

So a pane **visibly showing a dialog** while the state it was handed said `idle` was a safe
boundary — and `/clear` pasted there ANSWERS the dialog. An **empty tail** (which is what
`capture-pane` failure produces) was equally "safe", meaning rotation — the most
destructive action in the system — could fire on a pane nobody could read.

This is exactly what M1/M2 closed for `cw.decide`, `ap.evaluate` and `actuate()`. Rotation
was the one consumer never rewired: it kept trusting `state`.

**Fix** (`core/context_budget.py:105-124`): `phase` now calls `looks_like_dialog(tail)`
directly — the same fail-closed RU/EN + structural detector — and refuses an empty or
whitespace-only tail with `unobservable_pane`.

## Gap 2 (MEDIUM) — a replayed idempotency key left no trace

`_deliver`'s duplicate branch recorded only "duplicate, not delivered". A **second caller**
replaying another caller's key was therefore invisible — the precise blind spot the
attribution work existed to remove. Key reuse across callers is also the shape a
credential-sharing or replay problem would take.

**Fix** (`core/agent_control.py`): `_note_duplicate_attribution` attributes the replay
without ever overwriting the original (first writer wins — the original caller is the one
that actually reached the pane). A replay by a **different** caller is audited as
`delivery_duplicate_other_actor` with both identities; a retry by the same caller is normal
idempotency and is not flagged; a duplicate of an unattributed row records the replayer,
since that is then the only identity available. Wrapped so it can never raise into the
delivery path.

## Also checked, no gap found

- Attribution sidecar vs. columns: the rollback pin holds — the running build's positional
  6-value INSERT still works against a migrated DB.
- `caller_identity` degrades to `api:unknown` / `unknown` on a broken request and sanitises
  the self-declared name; no safety gate reads either field.
- Watchdog `decide`, autopilot `evaluate`, `actuate()` blind-pane and dialog gates: each
  refuses on the shapes claimed, with zero keystrokes.
- Precedence unchanged: an active-execution marker still beats a printed question, so a
  busy agent is not stalled by the structural detector.
- No existing test or fixture was modified by this review.

## Tests — `tests/test_review_gaps_2026_08_04.py`, 15 tests

Baseline proof in a `git worktree` at `5647b6d`, conftest sys.path **and** PYTHONPATH
repointed, import origin asserted; the pre-fix probes printed
`blind phase safe? True`, `dialog phase safe? True`,
`has _note_duplicate_attribution: False`.

**12 FAIL on pre-fix `5647b6d`.** **3 pass both sides by design** (anti-overcorrection /
scope pins): a readable clean pane still rotates, the pre-existing phase refusals still
fire, and the `_deliver` duplicate branch still touches no pane.

**Full suite: 1215 passed, 0 failed** (1200 before).

## Limitations

- `phase` now refuses on any dialog-shaped text in the captured tail, including a dialog
  already answered but still visible in scrollback — rotation is then delayed until it
  scrolls off. Fail-closed direction, and the alternative (trusting a possibly stale
  `state`) is what this gap was.
- Attribution of replays starts now; the 2026-08-03 rows remain unattributed by design.
- **Nothing here is live.** The service still runs `45cfb37` (PID 4063628, started
  2026-08-04 00:29:37 CEST); all six commits take effect only on an owner-approved restart.
