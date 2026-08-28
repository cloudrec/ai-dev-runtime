# Event 10801 — active work misclassified as waiting_input (gaika-server, 2026-08-28)

## The false wake

Owner OS emitted `agent_waiting_input` (`waiting_transitions`, `idle → waiting_input`)
for `gaika-server:0.0` at `2026-08-28T18:52:06Z`, waking the bound ChatGPT chat, while
the pane was provably still working at that moment:

- the live spinner showed `Wibbling… (thinking)` — an active-execution marker
- the conversation transcript file was actively being appended to
- GAIKA was mid-edit on its own tests (4 new, 528/528)

## Root cause

`core/agent_control.py::_STATE_ACTIVE_RUN_RE` — the sole path to a `working`
classification — requires a digit/duration inside the spinner's parenthesis
(`… (12s`, `(8s ·`) to recognize active execution. The gerund-spinner family
(`Pouncing…`/`Noodling…`/`Beboppin…`/`Wibbling…`/…) normally satisfies this via a
running timer, but this particular render carried a bare `(thinking)` suffix with
no duration at all. It matched none of the existing active-run patterns, so a
genuinely-active turn fell straight past step 1 (the only "definitely working"
check) into step 3 — the composer/pending-input heuristic — which misread the pane
and returned `waiting_input`.

More generally: nothing in the classification pipeline verified activity
*independently* of the pane-text regex before trusting the pending-input guess.
Every prior fix in this area (the recall-ghost check, the gaika-server
LOST_CONTINUATION rate guard) narrowed what the heuristic itself considered
"real," but none of them protected against the heuristic firing during genuine,
ongoing work whose spinner text simply didn't match a known pattern.

## Fix — two layers, `core/agent_control.py`

1. **Specific pattern gap closed.** `_STATE_ACTIVE_RUN_RE` gains `\(thinking\)` —
   any spinner ending in a bare `(thinking)` with no duration is now active-run
   evidence on its own.
2. **General backstop (the one the incident actually needs).**
   `conversation_recently_active(cwd)` — is the project's Claude transcript file's
   mtime within the last ~10s (`AGENT_CONVERSATION_RECENT_SECS`, env-tunable)? A
   transcript actively being written to is real work in progress, proven
   independently of what any single pane-text capture shows at that instant. Wired
   into both `agent_list()`'s per-agent loop and the single-target status path:
   pending-input detection is **skipped entirely** — not merely distrusted —
   whenever this is true, the same way it already skips while a live shell command
   is running. This protects against every future spinner-vocabulary variant this
   session hasn't seen yet, not just `(thinking)`.

## Tests

5 new regressions, `tests/test_agent_control.py`:
- `test_gerund_thinking_spinner_without_a_duration_is_working`
- `test_conversation_recently_active_true_for_a_fresh_transcript_write`
- `test_conversation_recently_active_false_for_a_stale_transcript`
- `test_conversation_recently_active_false_with_no_transcript`
- `test_recent_transcript_activity_overrides_a_pending_input_guess` — end-to-end:
  the pending-input path is monkeypatched to *raise* if called, proving it is
  skipped (not merely overridden) whenever recent transcript activity is present.

Run log:
```
118/118  test_agent_control.py + test_access_recovery.py
341/341  broader gate (test_agent_control, test_access_recovery,
         test_agent_orchestrator, test_context_budget, test_stall_doctor,
         test_agent_watch, test_agent_queued_prompt,
         test_queued_input_delivery_failure, test_queued_input_stall_incidents,
         test_wake_companion, test_closed_loop_wake)
2529/2530 full suite (1 pre-existing, unrelated failure —
         test_delivery_attribution.py::test_agent_send_threads_attribution_to_the_record,
         confirmed via git-stash earlier this session to fail identically without
         any of this session's changes)
```

## Commit & deploy

Commit `a992b3a` — *fix(agent-control): active work never classifies as
waiting_input (event 10801)* — `core/agent_control.py`,
`tests/test_agent_control.py`. Pushed to `ai-runtime/220-windows-bridge`; local
SHA == remote SHA, verified via fetch.

`owner-os-wake-companion` restarted `2026-08-28T21:13:03 CEST` to load the fix
(required — the companion is a long-lived process that only re-imports code on
restart). `gaika-server:0.0` was never touched: verification used a single
read-only `tmux capture-pane -p` (no keys sent, no interaction), and confirmed
the pane was still genuinely mid-turn (`✢ Choreographing…`) immediately after the
restart — the fix's own general backstop would apply to that shape too.
`ai-runtime.service` was not restarted — it does not import `core/agent_control.py`
directly, so no restart was needed there.

Preserved throughout: wake target/route bindings, `waiting_transitions` retirement
semantics, managed-auto scope, the stall-doctor pause/rate-guard from the prior
fix — none touched. No scheduling created.

## Status: closed

No remaining implementation, test, or deploy step. Nothing here depends on the
offline Windows host or Telegram — both stay as separate, already-tracked owner
gates, unrelated to this incident.
