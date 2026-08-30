# M1 — SHAPE-INDEPENDENT DIALOG DETECTION

**2026-08-04.** Closes the remaining MEDIUM finding **M1** from
`reports/FABLE_TARGETED_REVIEW_F9C06EE_2026-08-04.md`. Internal work only: no live pane
contact, no env/unit change, no service restart or deploy, actuation scope unchanged
(`cp-canary:0.0`), payment / arbitrage2-opus / mess-qa-automation untouched, no
destructive / live / payment / credential / publication action, nothing pushed. Autopilot
remains dormant.

## The defect

`_DIALOG_RE` (`core/agent_control.py:713`) is a denylist of KNOWN dialog phrasings. Claude
Code's own shapes are covered; an unseen wording is not. The review's live probe

```
Allow this tool to run?
> approve / deny
```

returned `looks_like_dialog() is False` on pre-fix `9fbb7f4` (re-verified in the baseline
worktree below), so `classify_state` read the pane as `idle` and the watchdog would have
pasted a continuation onto it — and that paste's Enter answers the permission prompt.
A third-party CLI, or any localisation not enumerated in the pattern, evaded the gate.

## The fix

A second detector matching STRUCTURE rather than wording, in
`core/agent_control.py:753-822`. A pane awaiting a human answer looks the same in any
language: a short question at the tip, and/or a small set of mutually exclusive choices
right after it.

| Element | Rule |
|---|---|
| `_dialog_scan_lines` | per-line normalisation (ANSI, box frames, NBSP) that PRESERVES line structure — `_dialog_scan_text` collapses it, which structural matching needs |
| question | line ending in `?`/`？`, ≤ 200 chars, within the last 12 non-empty lines |
| choices | slash pair (`approve / deny`, `разрешить / запретить`), or ≥ 2 short option lines (`❯ > » ▸ * • - –` or `1.` / `2)`), ≤ 80 chars each, within 4 lines of the question |
| permission intent | `allow / permit / grant / authorize / approve / deny / trust / confirm / разреш* / запрет* / довер* / подтверд*` — enough with a single option line, or with a slash pair and no question mark at all |

`dialog_signature` now returns the denylist match first and falls back to the structural
signature; `looks_like_dialog` inherits both. `classify_state` (`core/agent_control.py:893`)
was switched from calling `_DIALOG_RE` directly to calling `dialog_signature`, so the
structural detector reaches the state classifier too — without that, detection existed but
the pane still classified `idle`.

Both consumers inherit the fix with no change of their own: the watchdog's fail-closed
`pane_shows_dialog` gate and the autopilot's `skip_dialog_open` both route through
`looks_like_dialog`.

Bounds that keep it fail-closed without stalling agents: the question-length cap (a
narrative paragraph ending in `?` is not a prompt), the option-line length cap, the
12-line window from the pane tip, and unchanged precedence — an active-execution marker
still wins, so a live turn that prints a question stays `working`.

## Tests — `tests/test_dialog_structural_m1.py`, 30 tests

Baseline proof in a `git worktree` at `9fbb7f4` with `tests/conftest.py` sys.path **and**
its `PYTHONPATH` default repointed, import origin asserted before running
(`IMPORT-FROM: …/wt-9fbb7f4/core/agent_control.py`) — the false-green trap avoided. The
pre-fix probe printed `looks_like_dialog: False`, reproducing the finding exactly.

**21 FAIL on pre-fix `9fbb7f4`**, including: the review's live probe; five unseen English
shapes (numbered approve/deny, `❯ yes / no`, bullet keep/replace, authorize allow/deny);
three Russian shapes that match NO pattern in `_DIALOG_RE` (asserted in the test itself);
styled + box-framed variants; the no-question permission choice; and every watchdog and
autopilot refusal built on them.

**9 pass on both sides by design** (anti-overcorrection pins, not regression pins):
ordinary prose with an embedded question, a clean idle pane, a task footer, bullet lists,
report-writing output, empty tail, `test_a_working_pane_that_asks_something_is_still_working`,
`test_clean_idle_pane_still_reaches_delivery`, `test_long_prose_ending_in_a_question_is_not_a_prompt`.

**Full suite: 1171 passed, 0 failed** (1141 at `9fbb7f4`). No existing test or fixture
needed changing — the structural detector did not disturb any established contract.

## Limitations

- Still heuristic, now on two independent axes: a prompt with no question mark, no option
  markers and no permission vocabulary (for example a bare `>` awaiting free text) is not
  detected. Backstops remain: `waiting_owner` state, the pending-input guard, the dwell
  window, verified delivery, and the M2 unobservable-pane guard.
- Deliberate over-refusal: agent output that ends in a short question followed by two
  bullets reads as a dialog and delays a poke until the pane moves on. Fail-closed
  direction, and the anti-overcorrection tests bound how far it goes.
- Purely graphical dialogs (no text) remain undetectable by any text-based method.
- No live-pane verification — pane contact was forbidden; all evidence is synthetic tails
  plus the pre-fix behavioural probe.

## Checkpoint

- HEAD before: `9fbb7f4`. Suite before: 1141 → **1171 passed, 0 failed**.
- Live service untouched: PID 4063628, started 2026-08-04 00:29:37 CEST, running `45cfb37`
  code, autopilot dormant, canary allowlist `cp-canary:0.0` only.
- Both review findings are now closed: **M1 here, M2 in
  `reports/COMMANDER_WATCHER_M2_UNOBSERVABLE_PANE_2026-08-04.md`.**
- Still owner-gated / open: deploy of `45cfb37` + `f9c06ee` + `9fbb7f4` + this commit (the
  live watcher runs none of these guards); the deferred actuator-layer blind-pane guard;
  the unattributed `deliveries` writer from 2026-08-03T22:29–22:37Z.
