# Stall Doctor — LOST_CONTINUATION false-submit loop (gaika-server, 2026-08-28)

## The incident

`gaika-server:0.0` was intentionally idle/parked — no pending owner prompt, no
blocker, no active job, `pending=null` in every consumer that should have mattered.
Owner OS kept waking the bound ChatGPT chat anyway (events 10674, 10676, 10680,
10682, 10684 — `agent_waiting_input` fired on every tick the composer's dim
"suggested next input" happened to redraw differently: "check status", "check
status tomorrow", "next safe roadmap item…").

Live verification found something worse than a false wake: **`stall_doctor` was
actively auto-submitting Claude Code's own self-generated composer suggestion as
if it were a real queued owner instruction**, in a self-feeding loop —

```
14:15  "stay idle"                    -> auto-submitted
14:22  "wake me if owner clears a…"   -> auto-submitted
14:27  "ok stopping here"             -> auto-submitted
```

Each auto-submission fed the agent a fabricated instruction, which it answered,
producing a new self-generated suggestion, which got auto-submitted next.

## Root cause

`core/agent_control.py`'s `_pane_pending_input` (feeding `classify_state`, feeding
`agent_orchestrator`'s sweep, feeding `waiting_transitions`) never distinguished
Claude Code's own dim composer redraw from real staged input — its docstring
promised ghost-filtering that the code never actually implemented. Fixed in commit
`20b363d` by reusing the existing `_is_recall_ghost`/`last_submitted_text`
transcript check — but that check only proves ONE kind of dim text (a genuine
recall-echo of the last SUBMITTED message). It cannot prove or disprove Claude
Code's own AI-generated "suggested next step," because that text was, by
definition, never submitted by anyone before `stall_doctor` submits it.

**Confirmed: no durable mechanism in this codebase can prove text was staged by
Owner OS itself.** `agent_send`/`agent_answer` always paste+Enter atomically (no
deferred-submit path exists in production); the one primitive that *could* stage
text without submitting it (`DirectPaneController.replace_pending(submit=False)`,
`core/direct_pane_control.py:237-273`) has no production caller and no durable
audit sink wired in. Building that infrastructure is a real feature, not a narrow
fix, and was explicitly out of scope here.

## What was implemented instead

**1. Scoped, reversible pause** (`core/stall_doctor.py`) — `pause_target`,
`resume_target`, `is_paused`, `paused_targets`, backed by a new durable
`stall_doctor_pause` table. `scan()` skips a paused target entirely (observation
continues, only actuation stops) before doing any classification work. Applied
immediately to `gaika-server:0.0` while the fix below was built and tested; lifted
once the tests below passed (see §Resolution).

**2. Cross-episode submit-rate guard** — the real, permanent fix. Since a true
origin-proof isn't achievable today, the guard targets the actual failure shape
instead: `may_submit_queued`'s existing content-safety gate proves text is
*safe to type*, never that it *came from the owner*, and `MAX_ACTIONS_PER_EPISODE`
(the existing per-episode loop guard) never sees a self-feeding loop because each
new suggestion has a different digest and therefore starts a brand-new episode
with a zeroed action counter.

`LOST_CONTINUATION_MAX_SUBMITS_PER_WINDOW` (default 3) /
`LOST_CONTINUATION_SUBMIT_WINDOW_SECS` (default 3600) count actual
`submit_queued` **deliveries** for a target from the durable `stall_doctor_action`
log, across episodes/digests, within a rolling window. Once a target crosses the
rate, `decide()` escalates (`agent_waiting_input`, wakes the owner) instead of
auto-submitting — the same fail-safe posture `may_submit_queued` already uses for
unevaluable/forbidden content. A single legitimate queued instruction — the
ordinary shape for every other project — is completely unaffected; the guard only
engages on a genuine repeat pattern within the hour, which no plausible
human-paced instruction stream produces.

Both changes are env-tunable (`STALL_DOCTOR_LC_MAX_SUBMITS`,
`STALL_DOCTOR_LC_SUBMIT_WINDOW_SECS`) without a code change.

## Tests

`tests/test_stall_doctor.py`, all new, all passing:
- `test_pause_target_stops_actuation_for_that_target_only` — the paused target
  gets zero actuation; a second, unpaused target's normal auto-submit is untouched.
- `test_paused_target_is_observed_not_silently_dropped`
- `test_resume_target_restores_normal_actuation`
- `test_resume_of_a_never_paused_target_reports_false`
- `test_paused_targets_lists_current_pauses`
- `test_rate_limit_escalates_after_repeated_different_digest_submits` — the exact
  gaika-server shape: three different self-generated suggestions submitted within
  ~15 minutes escalate on the fourth.
- `test_rate_limit_does_not_affect_a_single_legitimate_submission`
- `test_decide_boundary_for_recent_lc_submits`

31/31 `test_stall_doctor.py`; 263/263 broader gate
(`test_stall_doctor`, `test_agent_control`, `test_access_recovery`,
`test_wake_companion`, `test_closed_loop_wake`, `test_agent_watch`,
`test_queued_input_delivery_failure`, `test_queued_input_stall_incidents`);
full suite 2524 passed / 1 failed (the same pre-existing, unrelated
`test_delivery_attribution.py` failure, confirmed via git-stash earlier this
session to fail identically without any of today's changes).

## Resolution

`gaika-server:0.0`'s pause was lifted (`resume_target`) after the rate-limit guard
above was committed, deployed, and its regression tests confirmed passing — the
permanent, general protection (works for every target, not just this one) is now
what's carrying the safety guarantee, not the temporary pause.

## Still open

A true origin-proof (durably knowing WHO typed WHAT into a composer) does not
exist anywhere in this codebase today. If this class of bug recurs on a different
project, or Claude Code's suggestion-redraw behavior changes shape, the rate-limit
is a backstop, not a cure — the underlying ambiguity (dim text with no matching
last-submitted message could be real staged input OR a CLI-generated suggestion)
is unresolved and, per the research done for this incident, would need new
production wiring (a durable staging path + audit sink) to resolve properly. Not
built here — out of the narrow, safe scope authorized for this fix.
