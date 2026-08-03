# Arbitrage2 owner-gate email — source trace (READ-ONLY)

**Date:** 2026-08-03 · **Mode:** read-only investigation. No email sent, no notification
settings / credentials / services / production behavior changed. Secrets located but never
shown (env inspected by NAME only).

**Subjects/bodies traced:** `Arbitrage2 завершил план упёрся в owner gate` /
`Есть итоговое существенное обновление...`

## Verdict

These emails are **NOT sent by any server-side Owner OS component.** They are composed and
delivered **externally by a ChatGPT Task / platform email delivery** that reads Owner OS
events over the runtime API and emails the owner. The subject/body are AI-phrased (natural
Russian, not a code template); truncation is a ChatGPT-summary / platform email limit, not a
server truncation.

## Evidence

### 1. No local email sender produced these strings
- **Exact-string search** for `существенное обновление`, `упёрся в owner gate`,
  `завершил план` across `/opt` and `/root` (excluding venv/git/node_modules): **no match**
  in any code, template, or locale. The wording exists nowhere in the codebase → not a
  server template.

### 2. Server-side email channel is NOT configured
- `/opt/seo/backend/services/notifications.py::_send` supports channels
  `local | webhook | telegram | email`. The **email branch returns `not_configured`
  ("SMTP not configured")** — the SEO Owner OS never sends email; it delivers to Telegram
  (when configured) or logs.
- `agent_notifier.py` (the commander-event drainer) delivers via `notifications._send`; with
  email not_configured it does **not** email. `daily_brief.send_if_due` likewise routes
  through `_send` (Telegram/logged), not SMTP.

### 3. No local mail transport / outbound mail
- `postfix`, `exim4`, `nullmailer` all **inactive**; no `sendmail`/`postfix` binary path
  active. No `/var/log/mail*` entries for `arbitrage2` / `owner gate` / `существен`.
- **No SMTP/mail credentials** anywhere relevant: `ai-runtime.service` env exposes
  `OPENAI_API_KEY` only (used by the planner, not email) and **no** `SMTP_*/MAIL_*/EMAIL_*/
  SENDGRID_*/MAILGUN_*`. SEO backend env exposes only `ADMIN_EMAIL` (a recipient address
  config value, not an SMTP sender). So no server process can send SMTP mail.

### 4. `/opt/email` is unrelated (B2B outreach, not owner alerts)
- `/opt/email` is the B2B affiliate **outreach** engine (`partnerOutreach`,
  `manualOutreach`, `tenants`, `collector`). It emails *partners*, not the owner about
  arbitrage2; it contains no owner-gate / arbitrage owner-notification code.

### 5. The CONTENT maps exactly to Owner OS events exposed by the runtime API
The email wording corresponds to real Owner OS events an external reader consumes:
- **"упёрся в owner gate"** ↔ control-plane owner-gate events: `owner_gate_opened` (#17,
  #40), `canary_continuation_gated` (#41, severity=high, owner_action_required=1),
  `decision_provenance_unverified` (#18, critical, owner_action_required=1); plus the
  arbitrage2 `unverified_owner_decision` gate.
- **"итоговое существенное обновление"** ↔ high-significance completion/update events for
  `arbitrage2-opus:0.0` in `commander_events` (agent_control.db).
- These are served read-only, owner-auth, by the runtime API:
  `GET /api/v1/agents/commander/events`, `GET /api/v1/control-plane/cto/brief` — the exact
  surface a ChatGPT CTO connector reads.

## Answers to the specific questions

- **Sender component:** external **ChatGPT Task / OpenAI platform email delivery** (not
  Owner OS, not agent_notifier, not `/opt/email`). No server-side SMTP is involved.
- **Trigger event:** an Owner OS **owner-gate / high-significance event** for `arbitrage2`
  (owner_gate_opened / canary_continuation_gated / decision_provenance_unverified /
  completion), surfaced via the CTO inbox + commander_events API. ChatGPT reads it and emails
  a summary.
- **Destination channel:** the owner's email inbox, via the ChatGPT/OpenAI platform's email
  delivery — NOT the server outbox. (`ADMIN_EMAIL` is the configured owner address.)
- **Dedup / retry:** server-side dedup exists on the DATA (commander_events: `event_id` +
  semantic fingerprint in `commander_delivered`, pre-delivery revalidation; control-plane
  events: `dedup_key`). But the **email** dedup/retry is on the **ChatGPT/platform side and
  is opaque to Owner OS** — the server neither sends nor tracks these emails. (This is also
  why the server's own delivery posture is RED / dead-lettering: the server-controlled
  owner-push channel is not configured, so server notifications are separate from these
  externally-sent emails.)
- **Why the body may be truncated:** the body is a **ChatGPT-generated summary** of the
  events, bounded by ChatGPT's output/summary length or the platform email formatting — a
  composer-side truncation, not a server field limit.

## What could NOT be observed (external, out of scope for read-only local inspection)

- The ChatGPT Task schedule, prompt, and email-delivery internals live on the OpenAI
  platform, not on this host — not inspectable here. The conclusion rests on: (a) no local
  sender/credentials/MTA, (b) strings absent from code, (c) content matching API-exposed
  Owner OS events, (d) AI-phrased natural language.

## Confidence

**High** that the email is externally composed/sent by ChatGPT (not server-side): four
independent negatives (no template, no SMTP config, no MTA, `/opt/email` unrelated) plus a
positive content-to-API-event match. The exact ChatGPT Task configuration is external and
was not directly inspected.

## Actions taken

None beyond read-only inspection and writing this report. No email sent; no settings,
credentials, services, or production behavior changed.
