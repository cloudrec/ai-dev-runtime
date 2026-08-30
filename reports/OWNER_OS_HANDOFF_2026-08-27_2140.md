# Owner OS handoff — 2026-08-27 21:40 UTC

Canonical branch `ai-runtime/220-windows-bridge` @ **b20c1f0**, local == remote,
0 unpushed. 29 unrelated dirty files preserved untouched throughout.

## Event 10037 — classified: NONE of the four watcher classes

Not FALSE-POSITIVE-RETURNED, not REAL-CRASH, not RECOVERY, not WAKE-FAILED.

| field | value |
| --- | --- |
| id / at | 10037, 21:35:01 |
| source / type | `stall_doctor` / `agent_waiting_input` (severity high, owner_action_required=1) |
| agent | `owner-os-opus-windows:0.0` (project ai-dev-runtime) |
| stored reason | `queued_line_not_submittable:would_answer_a_dialog` |
| digest | `47ddac360a86756f` (no excerpt field on this event type) |
| retraction overlay | not invalidated — expected: the overlay covers `agent_process_failed`, not waiting notices |

**It is the safety gate working.** The stall doctor found a queued line in that
pane and REFUSED to submit it because submitting would have answered a dialog —
fail-closed, surfaced to the owner instead of auto-answered. Its neighbours
(10032/10034/10035) show the same doctor submitting queued lines successfully on
this pane and on gaika-server, so the refusal is selective, not a breakage.

Wake handling was correct end to end: decided 21:35:02 -> route `owner-os` ->
delivered 21:35:31 `submitted_and_assistant_started_generating`.

**Condition has since self-resolved**: the pane is `working` with an empty input
line. No remediation performed, and none needed — this is not a defect in the
crash false-positive fix (that fix concerns crash classification only).

### Candidate finding (NOT remediated, needs a decision)

A `stall_doctor` waiting notice keeps `owner_action_required=1` after its
condition clears; only crash alerts get retired by `_reconcile_recovered_crash`.
Whether a resolved waiting notice should self-retire is a behaviour decision
about owner-facing semantics, not an obvious bug — deliberately left alone.

## State at handoff

* Wake loop proven end to end, causally ordered (ev 9997, ev 9999): real agent
  event -> correct project route -> persisted user turn -> assistant starts
  unprompted -> continuation carrying the wake's own event id reaches the agent
  -> agent starts (not queued).
* Delivery success now requires the ASSISTANT to start
  (`submitted_and_assistant_started_generating`); the old DOM-only criterion was
  a false positive the owner caught. Historic `user_turn_appeared` deliveries
  must be read as UNVERIFIED, not as failures.
* agent-watch crash false positive fixed and deployed (b20c1f0): harness blocks
  stripped whole, `killed` requires process context, crash matching reads a
  line-preserving view. 22 focused tests; no crash events since deploy.
* Windows bridge live: device `win-92840f98d82ad3fe`, workspace
  `gaika-basket-extension`.

## Owner gates — unchanged, nothing done on them

1. `TELEGRAM_CHAT_ID` invalid (`chat not found`); bot token is valid. Every
   Telegram notification dead-letters. Costs no wakes. Needs the owner to
   `/start` the bot or supply the correct chat id.
2. Widening managed-auto beyond `gaika-server` + `owner-os-opus-windows`.
3. Whether a resolved waiting notice should self-retire (above).
