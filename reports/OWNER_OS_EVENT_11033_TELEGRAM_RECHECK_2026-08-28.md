# Event 11033 — Telegram `getUpdates` recheck (read-only)

- **New unambiguous `/start` update:** No.
- **Candidate chat id:** None.
- **Result:** `getUpdates` against the already-configured bot token returned
  `HTTP 409 Conflict` — another active poller/webhook already holds the
  long-poll slot for this bot, so no updates could be read. Inconclusive, not
  resolved.
- **Config change made:** No.
- **Scheduling/polling created:** No.
- **Notification sent:** No.
- **Drafted external bug report sent:** No.

## Follow-up: diagnosed the 409 source (read-only, no changes)

- **Local processes checked:** no local process in this repo calls `getUpdates`
  (confirmed by source search). Two unrelated Telegram bot services exist on
  this host (`beautybot-telegram-bot.service`, `forms-telegram-bot.service`);
  neither is this bot's own consumer path (`owner-os-fleet-health.service`,
  the service that hosts this bot's send logic, is `active`; the other two
  bots are separate projects, unaffected/untouched).
- **`getWebhookInfo` (sanitized):**
  - `webhook_set`: **True**
  - `pending_update_count`: 0
  - `has_custom_certificate`: False
  - `last_error_date_present`: False
  - `last_error_message_present`: False
  - `max_connections`: 40
- **Root cause of the 409:** structural, not a competing process. Telegram
  makes `getUpdates` and an active webhook mutually exclusive — with a webhook
  set, `getUpdates` always returns 409 regardless of what else is running. This
  fully explains the earlier conflict on its own.
- **No service stopped/restarted/reconfigured. No updates consumed.**

Telegram remains a gated, unresolved owner blocker.
