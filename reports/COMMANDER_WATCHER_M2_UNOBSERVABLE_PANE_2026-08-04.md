# M2 — UNOBSERVABLE-PANE GUARD

**2026-08-04.** Applies the single MEDIUM finding **M2** from
`reports/FABLE_TARGETED_REVIEW_F9C06EE_2026-08-04.md`. Internal work only: no live pane
contact, no env/unit change, no service restart or deploy, no actuation scope change
beyond `cp-canary:0.0`, payment / arbitrage2-opus / mess-qa-automation untouched, nothing
pushed. Autopilot remains dormant.

## The defect

`core/agent_control._pane_tail` returns `""` when `tmux capture-pane` exits non-zero.
Every tail-based guard then reads that empty string as *"the pane is clear"*:

- `cw.pane_shows_dialog("")` → `False` (the fail-closed wrapper short-circuits on empty),
- `ac.dialog_signature("")` → `""`,
- `_ACTIVE_EXEC_RE.search("")` → no match,
- `ap.is_progressing(state, "")` → `False`.

So if `capture-pane` failed while `send-keys` still worked, the watchdog would paste a
continuation, and the autopilot would nominate a poke, onto a pane whose real contents —
a Russian or English permission dialog, foreign queued text, live work — were unknown.
The keystroke would answer or corrupt whatever was actually there.

Verified on pre-fix HEAD `8887460`: `decide` returned `deliver` and `evaluate` returned
`poke` for a pane with an empty tail, with no refusal recorded anywhere.

## The fix

Blindness signal: **nothing captured at all**. `pending` is read from the same styled
capture as the tail, so text on the input line proves the capture succeeded; only an
all-empty snapshot is the capture-failure signature. This narrowing matters — the first
implementation treated any empty tail as blind and regressed 23 established contracts
(notably the missed-Enter recovery this system exists for).

| File | Change |
|---|---|
| `core/agent_continuation_watchdog.py:390-395` | `blind = not _tail.strip() and not pending.strip()`, computed after the classification returns (which touch no pane) and checked before **every** path that reaches the keyboard → `skip / unobservable_pane`. |
| `core/commander_autopilot.py:161-168` | `evaluate` → `skip_unobservable_pane` before the poke-candidate branch. |
| `core/control_plane/actuator.py:204-210` | Comment only, recording the deliberately deferred layer (below). |

Ordering is deliberate: an unsafe pending text still returns `blocker`
(`unsafe_pending_text`) and an explicit `waiting_owner` snapshot still returns
`dialog_open` — neither touches a pane, and both carry more information for the ledger
than "unobservable" would.

## Deferred: the actuator-level guard (not applied)

The same refusal was implemented in `actuate` (3b3, `action_deferred_unobservable_pane`)
and **reverted**. In production an all-empty snapshot means capture failed, but **15
established actuator/bridge contracts model a CLEAN pane as `tail=""`** — the guard
turned every one into a refusal (`test_control_plane_actuator`,
`test_control_plane_p4prep`, `test_owner_os_adversarial` pending-guard tests). Closing it
at that layer requires a fixture-convention change (a clean pane must render something),
which is a separate scoped task and NOT an M2 fix. Current coverage: the watchdog refuses
before it ever calls the actuator, and the autopilot refuses before it nominates a poke —
so both production entry points are closed. A direct `actuate()` call from a future
caller on a blind pane is **not** guarded.

## Tests — `tests/test_unobservable_pane_m2.py`, 18 tests

Baseline proof in a `git worktree` at `8887460` with `tests/conftest.py` sys.path **and**
its `PYTHONPATH` default repointed, import origin asserted before running
(`IMPORT-FROM: …/wt-8887460/core/agent_continuation_watchdog.py`,
`…/core/commander_autopilot.py`) — the false-green trap avoided.

**11 FAIL on pre-fix `8887460`:**
`test_decide_refuses_proactive_delivery_on_a_blind_pane`,
`test_decide_treats_whitespace_only_tail_as_unobservable[""/"   "/"\n\n"/" \t \n "]`,
`test_blind_pane_hides_a_russian_dialog_nothing_is_pasted`,
`test_blind_pane_hides_an_english_numbered_dialog_no_paste_is_made`,
`test_run_once_never_types_on_an_unreadable_pane`,
`test_autopilot_never_pokes_an_unobservable_pane[""/"   "/"\n"]`.

**7 pass on both sides by design** (anti-overcorrection pins, not regression pins):
`test_autopilot_still_evaluates_a_readable_idle_pane`,
`test_visible_pane_still_submits_the_safe_pending_text`,
`test_visible_pane_still_delivers_proactively`,
`test_unsafe_pending_is_still_a_blocker_not_a_silent_skip`,
`test_pending_text_proves_the_pane_was_readable_and_still_submits`,
`test_waiting_owner_with_empty_tail_still_reports_dialog_open`,
`test_actuator_still_delivers_to_a_clean_visible_canary`.

RU/EN coverage: the hidden-dialog scenario is pinned in both languages
(«Точно удалить все данные? Продолжить? (да/нет)» and `Do you want to proceed? ❯ 1. Yes
2. No`), each asserting that the dialog IS detected when visible and that the blind pane
is refused when it is not.

**Two pre-existing fixtures adjusted** (they used `tail=""` as shorthand for a clean pane
and were not testing blindness; behaviour asserted is unchanged, only the fixture now
renders a readable pane): `test_agent_continuation_watchdog.py:120` proactive
opt-in/cap test, `test_commander_autopilot.py:115` no-footer skip test.

**Full suite: 1141 passed, 0 failed** (was 1123 at `f9c06ee`, 1123 at `8887460`).

## Limitations

- The live service still runs `45cfb37` code — this fix, like the `f9c06ee` guards, is
  **committed but not deployed**. Deploy remains an owner gate.
- Actuator-layer blind-pane refusal deferred (above): a direct `actuate()` caller is unguarded.
- The blindness signal is heuristic: a capture that succeeds but returns genuinely empty
  output (a pane cleared to a bare prompt with no styling) would be treated as blind →
  over-refusal, the safe direction.
- No live-pane verification — pane contact was forbidden; all evidence is synthetic tails
  plus the pre-fix behavioural probes.

## Checkpoint

- HEAD before: `8887460`. Suite before: 1123. Suite after: **1141 passed, 0 failed**.
- Live service untouched: PID 4063628, started 2026-08-04 00:29:37 CEST, autopilot dormant,
  canary allowlist `cp-canary:0.0` only.
- Owner-gated / open: deploy of `45cfb37` + `f9c06ee` + this fix; actuator-layer guard;
  M1 (dialog-shape denylist can miss third-party prompt phrasings) still open from the
  targeted review.
