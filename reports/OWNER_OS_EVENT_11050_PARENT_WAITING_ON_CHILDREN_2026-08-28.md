# Event 11050 — parent waiting on live background children woken as blocked (gaika-server, 2026-08-28)

## The false wake

Owner OS emitted `agent_waiting_input` (`working → waiting_input`) for
`gaika-server:0.0` at `2026-08-28T22:10:00Z`. The event's own stored evidence
proved the pane was genuinely mid-flight:

```
✻ Waiting for 2 background
  agents to finish
  ✔ Update installed · Re…
────────────────────────────
❯ continue waiting, check b…
────────────────────────────
  [CAVEMAN]
  ⏵⏵ auto mode on      · …

  ● main
  ◯ general-purpose 1m 53s ·
  ◯ general-purpose 1m 40s ·
```

Two live `general-purpose` forks with running timers, under a `main` parent
that was correctly waiting on them — distinct from every prior incident this
session (10801/10857), which were about spinner/thinking text, not
parent/child fork coordination.

## Root cause — a known pattern, silently broken by terminal wrap

`core/agent_control.py::_STATE_ACTIVE_RUN_RE` already had a pattern for exactly
this shape, added 2026-08-03 for the mess-qa-automation incident:
`waiting for \d+ background agents?\b`, using literal single spaces between
each word. At this pane's terminal width, Claude Code's own UI wrapped the
phrase across two lines — `"Waiting for 2 background\n  agents to finish"` —
and the literal-space requirement silently stopped matching. Reproduced
directly against the event's own stored payload, **before** touching any
code, confirming a real regex miss rather than a hypothesis:

```python
>>> _STATE_ACTIVE_RUN_RE.search(live_status_region(tail))
False   # pre-fix
```

With no active-run match, `classify_state` fell through to the
composer/pending-input step, which read the plausible-looking
`"continue waiting, check b…"` line as a genuine unsubmitted instruction —
`waiting_input`, even though the agent was correctly waiting on its own live
children the whole time.

## Fix

`core/agent_control.py` — the fragment now uses `\s+` between every word
(`waiting\s+for\s+\d+\s+background\s+agents?\b`) instead of literal spaces,
tolerating a wrap at any point in the phrase, not only the exact break this
one incident happened to hit.

## Tests

3 new regressions, `tests/test_agent_control.py`:
- `test_parent_waiting_on_wrapped_background_agents_text_is_working` — the
  exact wrapped shape.
- `test_parent_waiting_on_single_line_background_agent_text_is_still_working`
  — the original 2026-08-03 shape, confirmed unchanged.
- `test_gaika_server_agent_list_reports_working_for_parent_with_live_children`
  — end-to-end via `agent_list()`, with `_pane_shell_running`,
  `_pane_pending_input`, and `conversation_recently_active` all monkeypatched
  to *raise* if called — proving the fix resolves at the active-run step
  itself (the highest-confidence signal), not by falling back to a later one.

```
124/124  test_agent_control.py (was 121/121)
292/292  + test_access_recovery, test_context_budget,
         test_continuation_governor, test_owner_os_adversarial
496/496  broader wake/orchestrator/stall-doctor gate
2535/2536 full suite (1 pre-existing, unrelated failure —
         test_delivery_attribution.py, confirmed via git-stash earlier this
         session to fail identically without any of this session's changes)
```

## Commit & deploy

Commit `becf0b6` — *fix(agent-control): wrapped "background agents" text
still classifies working* — `core/agent_control.py`,
`tests/test_agent_control.py`. Local at time of writing; push status recorded
separately once authorized.

`owner-os-wake-companion` restarted `2026-08-29T00:32:15 CEST` to load the fix
(required — long-lived process, only re-imports on restart). No direct read of
gaika-server's live pane was performed during this fix's investigation,
verification, or deploy — the reproduction used the event's own already-stored
payload text exclusively, and post-restart health was confirmed via the
companion's own service logs (clean startup, no errors), never by capturing
gaika-server's pane while its forks might still be running.

Preserved throughout: wake target/route bindings, `waiting_transitions`
retirement semantics, managed-auto scope, Telegram/config/secrets — all
untouched. No scheduling created.

## Status: closed

No remaining implementation, test, or deploy step for this incident.
