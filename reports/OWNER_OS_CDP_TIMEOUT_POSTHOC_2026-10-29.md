# CDP timeout after send: wakes that landed but were recorded as lost (2026-10-29)

An automated instruction was received (Owner OS API relay, not owner-typed) asking for
read-only forensics of the reverse leg (Claude stop -> Owner OS -> ChatGPT wake -> ACK ->
ChatGPT command) and of the `cdp_error:WebSocketTimeoutException` deliveries on route
`pl`, with at most one narrow fix in an isolated worktree. Nothing here was deployed.

## Reverse leg, live (3e5f79e), owner-os pane `owner-os-supervisor-clean:0.0`

Commands whose idempotency key names the wake event (strongest attribution), from
`event` / `wake_delivery` (control_plane.db) and `delivery_attribution` (agent_control.db):

| event | type | event ts (UTC) | delivered (ACK) | ChatGPT command | lag |
|---|---|---|---|---|---|
| 74537 | work_stopped_incomplete | 17:20:47 | 17:23:30 ok | `74537-safe-overflow-detection-20261028` | 29 s |
| 74558 | work_stopped_incomplete | 17:44:33 | 17:45:59 ok | `74558-safe-preinstall-20261028` | 43 s |
| 74566 | work_stopped_incomplete | 17:56:04 | 17:57:26 ok | `event-74566-finish-safe-report-20261028` | 67 s |
| 74581 | work_stopped_incomplete | 18:15:19 | 18:16:41 ok | `wake-74581-safe-continuation-20261028` | 66 s |
| 74596 | work_stopped_incomplete | 18:38:18 | 18:39:37 ok | `74596-continue-safe-bounded-20261028` | 22 s |

All via `api:bearer/mcp-token-3`. No owner message and no timer in these chains: the
trigger is `agent_watch` (pane at rest `QUIESCENT_SECS`=300 s), the route is `owner-os`.

Not a wake: the 06:12:27 command (`owneros-event-driven-bidirectional-proof-20261029-v1`)
came 80 s after this pane's Stop (event 74897, `agent_turn_stopped`, routine by design —
no wake_audit row) and with no delivery to the owner-os chat since 05:54:51 (74889, vtb).
It belongs to ChatGPT's turn already in progress, not to an event-driven wake.

## Route `pl` WebSocketTimeout cases

| event | sent | timeout | latched before error | later | ChatGPT acted |
|---|---|---|---|---|---|
| 74724 | 23:15:25 | 23:16:07 | yes 23:15:32 | abandoned `submitted_delivery_unproven` 02:15 | unnamed command at +21 s |
| 74760 | 00:49:30 | 00:49:51 | **no** | re-offered 00:55 (back-pressure), **sent again** 01:00:52 | 3 commands naming 74760, from +102 s |
| 74803 | 03:05:57 | 03:06:18 | no | re-sent 03:11:21, delivered | no command seen |
| 74872 | 05:42:10 | 05:42:45 | yes 05:42:27 | watched unverified, resolved progress_observed | named, +61 s |
| 74896 | 06:09:09 | 06:09:45 | yes 06:09:29 | watched unverified | named, +62 s |

No wake was lost. The defect is the verdict: the renderer stops answering CDP while
ChatGPT starts its answer, the 15 s socket timeout fires, and `_attempt`'s catch-all
records `cdp_error` without looking again. Effects: no ACK for delivered wakes, a 3-hour
abandonment record for one, and a duplicate delivery (74760) when the error preceded the
latch. No tab recovery was involved or needed — the same tab delivered the next wakes.

## Fix (branch `ai-runtime/cdp-timeout-posthoc`, on top of aca2a37 / candidate 9adfb41)

`tools/cdp_composer.py`: `dispatched` is set the instant the send is pressed. An
exception after that runs `_verify_after_cdp_error`: a fresh session, bounded by
`WAKE_POSTHOC_VERIFY_SECS` (20 s), compares against the pre-send baselines. A new user
turn (count or id) latches; the assistant starting (same `_assistant_advanced` rule) goes
through `_settled` and returns `submitted_and_assistant_started_after_cdp_error:<Exc>`.
No evidence, or the page still hung: the old `cdp_error:<Exc>` verdict and latch, unchanged.
An exception before the send never opens a second session.

Tests: `tests/test_composer_cdp_timeout_posthoc.py`, 9 tests; 5 fail on pre-fix code, the
4 unchanged-behaviour guards pass on both.

## Event 74914: periodic wakes for an unchanged owner gate

An automated instruction was received asking why vtb:0.0, parked on a genuine owner gate,
keeps waking ChatGPT. Read-only on a copy of control_plane.db.

vtb:0.0: `agent_watch_state` cls `owner_prompt`, digest `a3f420d6b8b74117`, unchanged since
2026-10-19 21:47:33 (668 emissions). Each hour `agent_watch` (REMINDER_SECS=3600) emits a
new `agent_prompt_needs_response` with the same digest; it is a new event id, so
`should_wake`'s per-event dedupe never matches and it wakes the owner-os chat
(`actionable_waiting_transition`). The closed-loop watchdog then re-wakes and escalates it:

| reminder | delivered | re-wake | escalation |
|---|---|---|---|
| 74802 | 02:53:36 | 74804 | 74811 |
| 74822 | 03:54:19 | 74827 | 74836 |
| 74846 | 04:54:38 | 74848 | 74855 |
| 74889 | 05:54:51 | 74902 | 74914 |

9adfb41 removes the re-wake/escalation pair; the hourly reminder wake itself remained.

Fix (`core/wake_bridge.py`, `unchanged_owner_gate_already_delivered`, checked in
`should_wake` after the recurring-cause rule): skip with
`owner_gate_unchanged_already_delivered` when the event carries the gate class and digest,
the pane still shows that class and digest, and a wake for the same target/type/class/digest
was delivered at or after `digest_since`. Re-delivered once a day (RE_ALERT_SECS, the owner's
2026-09-08 rule for unchanged red postures). Any screen change re-arms. Fails open. The
event stays in the inbox and owner status; nothing in agent_watch changes.

On the DB copy: before, 74802/74822/74846/74889 each `wake`; after, each skipped, citing the
previous delivered reminder (74778, 74802, 74822, 74846). 7-day sweep with current state:
only vtb:0.0 (29) and bitwhite-backend-v3:0.0 (15, parked on its grant gate) match; all other
agents 0. Steady state for vtb: 1 wake/day instead of ~3/hour.

Tests: `tests/test_wake_unchanged_owner_gate.py`, 11; 2 fail without the fix. Full suite
4088 passed, 4 skipped.

## Live proof after installing 9adfb41 (2026-10-29)

The owner typed "устанавливай сам" in the session (no automated header, no matching
delivery_attribution row; last relay 06:30:43Z). `install_9adfb41.sh` ran at ~06:44Z: live
3e5f79e -> 9adfb41, companion pid 3909219 -> 1314600, fingerprint 53d05ff4…, untracked
files verified unchanged. Rollback: `git -C /root/ai-dev-runtime reset --keep 3e5f79e &&
systemctl restart owner-os-wake-companion.service`.

Reverse leg, no owner message, no timer (this pane):
74921 `agent_waiting_input` (blocker) 06:45:37 -> delivered 06:46:54
`submitted_and_assistant_started_generating+conversation_full_notice` (owner-os chat) ->
ChatGPT command `event-74921-continue-live-vtb-watch-20261029` 06:47:19 (25 s).

vtb repeat suppression: 74924 `agent_prompt_needs_response` 06:53:50 (digest
a3f420d6b8b74117) -> delivered 06:55:05 -> watch deregistered 06:55:59
`prompt_already_escalated_unchanged`, rewoken=0, escalated=0. Before (74889): re-wake
74902 at +18 min and escalation 74914 at +34 min. The reminder wake itself remains until
f146d9d is installed.

## Pre-install check of b808632 (read-only, 2026-10-29 ~08:10Z)

An automated instruction was received asking to finish the read-only validation of b808632.

Full suite at b808632 (worktree): 4088 passed, 4 skipped, EXIT=0 (527.85 s), the same as
the 4088 reported for f146d9d. b808632 changes only this report.

Live (9adfb41) is still the expected pre-f146d9d behaviour: the next vtb reminder, 74950
`agent_prompt_needs_response` 07:54:43 (digest a3f420d6b8b74117), was woken
(`actionable_waiting_transition`, delivered 07:56:12), then its watch resolved at 07:58:00
`prompt_already_escalated_unchanged`, rewoken=0, escalated=0. That was the second
consecutive hour with no re-wake or escalation, so no new code mutation is needed.

Replay of f146d9d's `should_wake` on a copy of control_plane.db, with each event's own
audit, delivery and watch rows removed: 74924 -> skip `owner_gate_unchanged_already_delivered`
(first 74889); 74950 -> the same skip (first 74924). Not installed. Installing remains owner-run:
`backups/stop-wake-deploy-2026-10-28/install_b808632.sh`.
