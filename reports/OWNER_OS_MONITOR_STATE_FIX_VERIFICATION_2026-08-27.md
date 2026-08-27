# Post-deploy verification — monitor-session state fix (`cedd060`)

Deployed 2026-08-27 22:09:48 UTC by restarting `ai-runtime.service` and
`owner-os-wake-companion.service`. Both active. Branch
`ai-runtime/220-windows-bridge` @ `cedd060`, local == remote, 0 unpushed.
29 unrelated dirty files preserved untouched.

## The fix works, proven on live data and not only in tests

Event **10053** (22:10:40) is the confirmation:

```
from_state: "shell_running"  ->  to_state: "waiting_input"
evidence:   ... ❯ Keep monitoring and confirm no false wa…
            ⏵⏵ auto mode on · 2 monitors · ← 3 agents
```

Two things are true in that one event:

1. **The at-rest state is now `shell_running`, not `idle`.** Every earlier
   own-pane transition this session read `from_state: idle` or `working`. This is
   the first to rest in `shell_running` — the classifier now sees the live footer
   counter `· 2 monitors ·`.
2. **A genuine waiting_input still fires.** The composer held a real queued owner
   line, so the transition was emitted exactly as it should be. The fix did not
   mask a real prompt.

`agent_status('owner-os-opus-windows')` returns `shell_running` at rest; before
the fix the same pane returned `idle`.

## No false wakes

Own-pane `agent_waiting_input` events, 21:00 UTC onward:

| window | count |
| --- | --- |
| before deploy | 9 |
| after deploy | 1 (10053, genuine — real queued owner line) |

The single post-deploy event is correct, not false. **Caveat on the ratio:** the
post-deploy window is minutes, not hours, so this is directional evidence, not a
proven rate. The structural claim — `idle` no longer reachable for a pane with a
live monitors counter, so it is no longer a poke/continuation candidate — is what
is actually established, and that follows from the classifier change plus 10053.

Event 10053 delivered 22:11:02, `submitted_and_assistant_started_generating`,
route `owner-os`. Wake loop intact.

## No disturbance to anything unrelated

* **Other panes**: all 11 non-monitor agents still classify `idle`. The signal did
  not leak. `owner-os-opus-windows` is the only pane whose state changed, and it
  is the only pane running monitors.
* **Routes**: `wake_route` = 9, `wake_target` = 1, bound conversation unchanged.
* **Stall doctor**: zero actions on any target since deploy. Nothing was actuated.
* **Telegram**: unchanged. The only dead-letter since deploy is 10054
  (notification 1938, `waiting:gaika-server:0.0:...`), channel `telegram`, same
  known-bad-chat-id signature. No new channel, no behaviour change. Owner gate 1
  untouched.
* **Watchdog scope**: `CONTINUATION_WATCHDOG_SESSIONS=owner-os-opus-windows,gaika-server`
  — not widened.
* **Supervisor dormancy**: unchanged; nothing was enabled.

## Test record

`tests/test_agent_control.py` — 85 passed (5 new regression tests).
Full suite — 2502 passed, 1 failed:
`test_delivery_attribution::test_agent_send_threads_attribution_to_the_record`.
That failure is **pre-existing**, reproduced on clean HEAD with the fix stashed.
Unrelated to this change and deliberately not fixed here.

## Owner gates — still open, still untouched

1. `TELEGRAM_CHAT_ID` invalid; every Telegram notification dead-letters.
   1930+ open `owner_action_required=1` dead-letter events, the largest class in
   the owner queue. Needs the owner to `/start` the bot or supply the chat id.
2. Widening managed-auto beyond `gaika-server` + `owner-os-opus-windows`.
3. Whether a resolved `waiting_transitions` notice should self-retire.
