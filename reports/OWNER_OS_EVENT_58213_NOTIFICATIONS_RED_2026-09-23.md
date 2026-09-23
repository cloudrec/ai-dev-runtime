# Event 58213 — `notifications_red`, 2026-09-23 10:29:08Z

An automated instruction was received via the Owner OS API (not owner sign-off) asking
which channel is red, the exact non-secret reason, the retry/dead-letter state, and
whether the accounting hides or misclassifies the condition.

## What was red, and why

One channel: **`owner_push` (Telegram)**. Non-secret reason, verbatim from the capability
row: `telegram send failed: Remote end closed connection without response` — a TCP reset
from the Telegram API, recorded at 10:28:36Z.

| Tier | State at 10:29:08Z | Note |
|---|---|---|
| `owner_push` | unhealthy | the transport error above |
| `same_chat_wake` | unavailable | no server→ChatGPT inbound trigger exists on this platform (structural, not a fault) |
| `cdp_same_chat` | available, 19 deliveries proven in the last 3600 s | a WAKE path, not a notifier tier — it does not lift the red |
| `scheduled_chatgpt` | unavailable | hourly fallback, never the pinger |
| `cto_inbox` | available | durable pull |

`notifications_status()` is red when no proactive NOTIFIER tier is healthy, so the verdict
was honest: with `owner_push` down and `same_chat_wake` structurally absent, no proactive
notifier existed for those minutes.

## Credential/config or code? — code, on two counts

**Not credentials.** The 2026-09-06..11 dead-letter era (`Bad Request: chat not found`,
2 650 dead letters, ending 2026-09-11 02:33) was the chat-id gate. It is over: zero
dead letters since, 119 sent today, 195 yesterday, receipts `telegram:2514…2519`
contiguous. Receipt **2518 landed at 10:29:09Z — one second AFTER the red** — and the
channel row was `healthy` again at 10:31:50Z. Live state at the time of writing: **green**,
`owner_push` healthy, last receipt `telegram:2519`. Nothing here needs a credential,
route or config change, so no gate is crossed.

**Defect 1 — a red that clears is never recorded as cleared.** `notifications_red` is
critical + `owner_action_required=1`; nothing ever emitted the all-clear. **936** reds in
the log, **0** recovery events, **0** with `resolves` — while the tmux plane has carried
exactly this doctrine (`agent_control_plane_recovered`, info, routine, never a wake) since
2026-08-30. 58213 is the pattern in miniature: alarm at 10:29:08, condition gone by
10:31:50, alarm still open.

**Defect 2 — found while writing the all-clear.** With a single fixed dedup key
(`notifications_red`, 1 h window), red → green → red inside the hour emitted NOTHING the
second time. Once a recovery marked the first alarm resolved, a genuinely LIVE red would
have had no open alarm at all — the fix for defect 1 would have created an under-report.

**Defect 3 — evidence attributed to the wrong state.** For an unhealthy `owner_push`,
`proven_at` and `evidence` were filled from the last SUCCESSFUL send: 58213 published
`state: unhealthy`, `proven_this_runtime: true`, `proven_at: 10:28:36`, `evidence:
telegram:2517` — a receipt that had succeeded — beside a `detail` describing the failure.
A reader cannot tell which send the proof belongs to.

Nothing was hiding a failure in the other direction: no false green. The evidence-scoped
design (`_owner_push_state`) held — a proven failure survived the next probe and only a
proven send restored green.

## Fix — commit `409c59a`, local only

* `notifications_recovered` (info, `owner_action_required=0`, `push=False`, `resolves`
  the open red) emitted when a green posture answers an unanswered red; once per red,
  never on a green that was always green. Added to `wake_bridge.ROUTINE_EVENT_TYPES`, so
  it can never wake anyone — it would travel to a channel that was just down.
* `notifications_red`'s dedup key now carries an outage episode that advances only when a
  recovery is recorded: repeats inside one outage still collapse to one alarm an hour; a
  new outage is a new alarm.
* `detect_capabilities`: `proven_at` / `evidence` describe the CURRENT state (null when
  it is not healthy); the history moves to `last_success_at` / `last_success_proof`, and
  `last_error` carries the reason.

11 regression tests. No schema change, no credential, route, service or push.

## Remediation that is NOT ours to do

* `same_chat_wake` stays unavailable until an inbound trigger URL exists
  (`CONTROL_PLANE_SAMECHAT_WAKE_URL`) — a platform/owner decision, unchanged by this fix.
* The 2 650 dead-lettered notifications from the chat-id era are not re-sent. Re-queuing
  weeks-old alerts would be a spam event in its own right; they remain in the log as
  history.

## Objective status

The zero-ping/autonomy objective stays OPEN. The notifier posture is green at this moment
and the transport blip self-corrected, but `owner_push` is a single transport-dependent
tier, `same_chat_wake` does not exist on this platform, and the three fixes above are
LOCAL — the daemon still runs the pre-fix code. Nothing here is proven live until the
companion/daemon gates below are cleared by the owner.

## Remaining gates (privileged — not taken)

1. `systemctl restart owner-os-wake-companion.service`
2. re-scope the live tmux server 3739055 (`busctl … StartTransientUnit … PIDs au 1 3739055`)
3. `systemctl restart ai-runtime.service` — only after 2; deploys this fix and the others
4. `git push`
