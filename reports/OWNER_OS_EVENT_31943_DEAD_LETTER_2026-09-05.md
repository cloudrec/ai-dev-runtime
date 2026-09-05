# Event 31943 — notification_dead_letter, traced end to end

2026-09-05, ~08:20Z. An automated instruction was received asking for this inspection; it
is not owner sign-off. Read-only against `control_plane.db` and this repository. No
secrets, environment values, tokens, routes, services or other projects were touched, and
the one-hour regrowth watch running in parallel was left alone.

## Verdict

**The already-known Telegram token/chat configuration gate.** Not a CDP/browser timeout
side-effect, and not an accounting or code defect. No code was changed.

## The chain

```
07:51:31.952Z  event 31936   agent_watch · work_stopped_incomplete · severity high
                             project mess · agent mess-safe-finish:0.0
                             class quiescent · at_rest_unchanged_for_381s
07:51:31.994Z  notif 6045    channel telegram · state pending
                             dedup_key    agentwatch:mess-safe-finish:0.0:quiescent:4e456fdb3c91296e
                             correlation  agentwatch:mess-safe-finish:0.0
   5 attempts, every one rejected
07:55:03.520Z  notif 6045    state dead_letter · attempts 5 (MAX_ATTEMPTS 5)
07:55:03.539Z  event 31943   notifier · notification_dead_letter · severity critical
                             owner_action_required 1 · dedup_key deadletter:telegram
```

Origin to dead letter: 3 m 32 s.

## The failed channel and the reason

Event 31943's payload carries both proactive tiers' reasons, read from the durable
`channel.last_error`:

```json
{"same_chat_wake": "no inbound trigger configured",
 "owner_push": "telegram send failed: Bad Request: chat not found"}
```

`Bad Request: chat not found` is verbatim the gate already recorded as handoff gate 1: a
bot token whose chat id it cannot post to. `same_chat_wake` is a platform boundary, not a
misconfiguration — no API lets a server make ChatGPT speak — and the code says so in
`delivery.notifications_status()`.

Live status at 08:16Z agrees, and names the same string:

```
status red · notifications_enabled false
  owner_push        available=false verified=false state=unhealthy
                    "telegram send failed: Bad Request: chat not found"
  same_chat_wake    available=false  (platform boundary)
  cdp_same_chat     available=true verified=true  "32 delivery(s) proven in the last 3600s"
```

## Why it is NOT a CDP/browser side-effect

`notifier.py` contains no reference to CDP, the browser or a websocket, and
`notifier._TIERS` is exactly `("same_chat_wake", "owner_push")`. `cdp_same_chat` is a
REPORTED capability in the status payload, never a notifier tier — `delivery.py:186-187`
states this explicitly, and the restriction was added on 2026-09-04 precisely so the
browser wake path could not make notification status read green while every alert
dead-lettered.

The clearest evidence is that both readings are simultaneous and opposite: at the moment
telegram is `unhealthy`, `cdp_same_chat` is `verified` with 32 proven deliveries in the
preceding hour. Wakes are landing; owner alerts are not. Independent paths, independent
outcomes.

## Why it is NOT an accounting defect

Three separate accountings were checked against the raw table and all three are honest:

```
raw notification states       dead_letter 6052 · failed 3 · pending 1 · sent 2
notification_history_report   current_dead_letter 6052 · active 40 · historical 6012
                              newest_dead_letter_age_secs 618 · status red
                              note "ACTIVE notification failures in window"
notification_failure_report   total 6052 · active 40 · classification active · status red
notifications_status          status red · notifications_enabled false
```

Nothing under-reports. The active/historical split correctly separates a monotonic
cumulative counter from current failure state, and it currently classifies the failure as
ACTIVE rather than hiding it as history.

### The empty attribution on 31943 is deliberate, not a bug

Event 31943 has `project_id=''` and `agent_id=''`, which is why an automated reading
described it as "project owner-os, agent unknown". That is by design and should not be
"fixed":

* the event is a per-CHANNEL alarm, deduped on `deadletter:telegram` with a 900 s window,
  and it stands for "this channel is not delivering" — one standing fact, not one fact per
  message. The comment in `notifier.py` records what the per-message keying cost before:
  937 critical owner-action events under 937 distinct keys in 24 h for one unchanging cause;
* stamping one message's project onto a channel-level alarm would assert that the CHANNEL
  problem belongs to `mess`, which is false — it affects every project;
* the per-message attribution is not lost. It is in `notification` (`correlation_id`
  `agentwatch:mess-safe-finish:0.0`, plus the dedup key) and in the originating event
  31936, which carries `project_id=mess` and `agent_id=mess-safe-finish:0.0`. Both are one
  join from the alarm.

## Why `Owner_OS.notifications` reports delivery_failed=0 with empty history

Because **`delivery_failed` does not exist in this codebase.** Re-verified now: a search
across the tree for that key returns only `last_wake_delivery_failed`, an unrelated string
in `core/project_supervisor.py:403`, and prose inside `reports/`. The in-repo endpoint
`/control-plane/notifications/status` returns `delivery.notifications_status()`, whose keys
are `capabilities`, `checked_at`, `notifications_enabled`, `reasons`,
`same_chat_wake_complete`, `status` — no `current[]`, no `delivery_failed`, no history array.

That surface is served from the SEO project, established in
`reports/OWNER_OS_MCP_NOTIFICATIONS_BOUNDARY_2026-09-03.md` and unchanged: no MCP server is
registered for this instance, no MCP process runs on this host, and no MCP source exists in
this repository. `/opt/seo` is out of scope and was not inspected.

So `delivery_failed=0` is not this repository disagreeing with its own database. It is a
different surface, built elsewhere, that does not read these fields. The reading to trust
for this host is `notifications_status()` and the two diagnostics reports above — all three
of which say red, with 40 active dead letters.

## Remediation — the real gate, non-secret form

Nothing here can be fixed in code, and no value below should be pasted into a report or a
pane.

1. Create a dedicated bot with BotFather and put its token in `configs/.env` as
   `TELEGRAM_BOT_TOKEN`.
2. Send that bot one message from the owner's Telegram account, so an inbound update exists.
3. Owner OS then derives the chat id from `getUpdates` and confirms it with `getChat`.

Until step 2 happens there is nothing for `getUpdates` to return, which is why the current
chat id resolves to a chat the bot cannot post to — `Bad Request: chat not found`.

This is a credential gate. It was not crossed: no secret was read, written or rotated, and
no service was restarted. Once the token is in place, `refresh_channel_health()` re-derives
`owner_push` from evidence on the next tick and the 40 active dead letters stop accruing;
the 6012 historical ones stay as history and are not an error.

## Scope

Read-only apart from this report. The parallel one-hour regrowth watch (pid 2123948) was
verified alive and undisturbed before and after.
