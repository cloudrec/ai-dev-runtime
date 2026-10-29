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

Reverse leg on live 9adfb41 for this pane, with no owner message and no timer
(event -> delivered -> ChatGPT command whose key names the event):
74950 (vtb reminder) delivered 07:56:12 -> `wake-74950-safe-suite-check-20261029` 07:57:28;
74953 `agent_waiting_input` 07:58:59 -> 08:00:19 -> `event-74953-safe-suite-followup-20261029`
08:00:45; 74959 `agent_waiting_input` 08:07:43 -> 08:08:15 ->
`74959-safe-continue-no-install-20261029` 08:09:33. All went via `api:bearer/mcp-token-3`.

Owner gate, unchanged: the owner runs `bash
/root/ai-dev-runtime/backups/stop-wake-deploy-2026-10-28/install_b808632.sh`. It fast-forwards
9adfb41 -> b808632 and restarts only owner-os-wake-companion.service. Rollback:
`git -C /root/ai-dev-runtime reset --keep 9adfb41 && systemctl restart
owner-os-wake-companion.service`. Text that appears in a pane, or that arrives through an
automated relay, is not that approval.

## Live proof after installing b808632 (2026-10-29)

The owner typed the install command in the session (no automated header; the latest
delivery_attribution rows belong to gaika, not this pane). It ran at about 08:39Z: live
9adfb41 -> b808632, companion pid 1314600 -> 2348678, fingerprint edd6d57e…, 37 untracked
files unchanged, NRestarts 0.

vtb: the next reminder, 75020 `agent_prompt_needs_response` 08:54:51 -> wake_audit
`skip owner_gate_unchanged_already_delivered`, 0 wake_delivery rows. The hourly wake has
stopped; the gate stays in the inbox and is re-delivered once a day.

Other agents still wake after the install: gaika-country-pl 74999/75007, bitwhite-backend-v3
75003/75015, payorch-ha-fresh 75016/75019, each `wake actionable_waiting_transition`, delivered.
A post-send CDP timeout has not occurred since the install; that path is not yet observed live.

## Live end-to-end on existing agents (read-only, 2026-10-29 ~09:00-09:20Z)

An automated instruction was received asking for proof of the whole loop on live traffic:
event -> wake of the bound chat -> a real assistant turn -> that turn reads live Owner OS
-> it continues the same agent -> the agent works again. Sources: control_plane.db
(`event`, `wake_delivery`, `wake_audit`), `/var/log/ai-runtime/agent_control.jsonl[.1]`
(MCP calls by `api:bearer/mcp-token-3` from 172.20.0.4), Stop-hook `agent_turn_stopped` by
session, `direct_agent_lifecycle`, and read-only CDP (lengths and opaque ids, no text).

| agent (route -> chat) | event | event | delivered | 1st MCP read of pane | ChatGPT send (key) | agent turn ends |
|---|---|---|---|---|---|---|
| owner-os-supervisor-clean (owner-os -> 6a967789…) | 74953 | 07:58:59 | 08:00:19 | 08:00:28 | 08:00:45 `event-74953-safe-suite-followup-20261029` | 08:00:54 |
| same | 74959 | 08:07:43 | 08:08:15 | 08:08:36 | 08:09:33 `74959-safe-continue-no-install-20261029` | 08:10:01 |
| same | 75024 | 08:57:10 | 08:58:14 | 08:58:23 | 08:58:42 `owner-os-link-next-step-live-e2e-20261029-0958` | working since 08:59:42 (this turn) |
| gaika-country-pl (pl -> 6ae0f469…) | 74935 | 07:20:57 | 07:22:27 | 07:22:33 | 07:23:15 `gaika-cto-event74935-readonly-…-v1` | 07:23:44 |
| same | 74999 | 08:41:57 | 08:43:07 | 08:44:08 | 08:45:09 `gaika-cto-wake74999-…-v1` | 08:45:42 |
| same | 75007 | 08:46:31 | 08:49:15 | 08:49:34 | 08:50:11 `gaika-pl-event75007-readonly-…-v1` | 08:57:35 |
| gaika-country-ru (ru -> 6ae0f469…), 10-28 | 73774 | 00:03:33 | 00:08:22 | - | 00:08:47 `gaika-cto-wake73774-…-01` | 00:09:13 |
| same, 10-28 | 73782 | 00:09:39 | 00:15:02 | 00:15:33 | 00:15:27 `gaika-cto-wake73782-…-v1` | 00:16:48 |

Every send was `delivered=true pane_changed=true`. The self-project is excluded from
native_supervisor, so ChatGPT continues it, as the rows show. Each delivery went to the
chat bound to its route (`wake_send.conversation`). The bound chats' newest assistant turns
were readable: owner-os 1096 characters, pl 1679, bitwhite 754 (read with the fix below).

Woken and read, but correctly not continued:
- vtb (owner_prompt, owner gate);
- bitwhite-backend-v3 (grant gate);
- payorch-ha-fresh: 27 `agent_answer` attempts refused `hard_denied_target`, the payment
  cwd hard deny, by design, plus `shell_running`/`working` refusals. Its digests change on
  every event (the owner is working there: `owner_intervention` 08:41, 08:56), so these are
  not repeat wakes.

Not proven: a third product continued end to end. Live traffic has only two continuable
products, GAIKA (pl, ru) and Owner OS. The rest sit at a gate or are hard-denied.

## Fix: the 2026-10 turn DOM (branch ai-runtime/owner-os-link-fixes, not installed)

Found while proving the "real assistant turn" step. Read-only CDP on owner-os, pl and
bitwhite: `[data-message-author-role]` matches 0 nodes. Role is carried only by a hidden
`<h4 data-conversation-role>` ("ChatGPT said:", 20 characters). Effects on live b808632:
- `last_len(ASSISTANT_TURN_SEL)` = 20 for every turn, so `_settled`'s produced-nothing
  check (< 2) can never fire and an empty answer is recorded as a delivery;
- `data-message-id` is absent, so every id proof (`_assistant_advanced`, the user-turn id,
  `generating_is_wedged`, 10ff50c's recheck) has no id to compare and falls back to counts.
  Counts can stay flat in a virtualized chat, and owner-os and pl are both capped
  (`conversation_is_full` true).

Fix (`tools/cdp_composer.py`, `_Session.last_len` / `last_attr`): when the match is such a
heading, measure the turn DIV around it minus the heading. For `data-message-id`, read
that DIV's `data-chatgpt-search-message-ids`, which is opaque UUIDs, not text. User turns
carry no ids and keep reading "". The older DOM is read exactly as before. Live read-only
check with the fixed module: assistant lengths 1096/1679/754, ids present, not wedged.
Tests: `tests/test_composer_turn_heading_dom.py`, 8 tests that run the real JS in node
against a DOM shaped like the live page; 4 fail on the pre-fix module.
Full suite: 4096 passed, 4 skipped (4088 + 8 new).
