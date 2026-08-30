# Owner OS event 7409 — diagnostic

1. Event id/type: 7409, `notification_dead_letter`.
2. Notification/job: id 1470, `agent_prompt_needs_response` (source `agent_watch`), event 7407.
3. Target/destination: `owner-os-server-alerts:0.0` (ai-dev-runtime pane) → telegram/owner_push.
4. Attempt/retry/error: 5 attempts, 22:39:26Z→22:42:08Z → dead_letter; `Bad Request: chat not found`.
5. Payment-runtime impact: no — unrelated target, no connection to the RU-PROD/Patroni failover.
6. State changes: none — no code/config/services/routing touched.
7. Delivered anyway: yes, via ChatGPT-wake fallback, real receipt (wake_delivery id 3081, 01:40:33Z).
