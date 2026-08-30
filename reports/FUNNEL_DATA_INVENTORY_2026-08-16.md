# Funnel Data Inventory — Task 200 Phase 1 (Read-Only Recon)

Date: 2026-08-16
Scope: `seo-postgres-1` (`traffic_os` db), `/opt/seo` tree, `seo-worker-1`/`seo-backend-1` container logs and env (names only). Read-only throughout — no writes, no outreach, no external calls.

## 1. Data Map

| Location | Rows | Date range | Contents |
|---|---|---|---|
| `client_leads` (Postgres) | 24 | 2026-05-29 → 2026-06-23 | **100% auto-seeded demo data.** `routes_clients.py` calls `seed_sample_leads()` on every empty-table hit of `/leads` or `/pipeline`; the same 8 fictional leads (Sarah Chen/CloudScale, Marcus Johnson/RetailBoost, etc., hardcoded in `_SAMPLE_LEADS`) were inserted 3x across 3 sessions. `last_contact` is NULL on every row. |
| `client_lead_activities` | 0 | — | Empty. This is the table `/api/clients/outreach` writes to (`activity_type="email"`) — zero invocations ever, real or stub. |
| `revenue_leads` | 11 | 2026-05-30 → 2026-05-31 (one day) | 100% QA/test artifacts: `sample@test.invalid`, `qa@test.com`, titles like `[QA_TEST] Full Service QA Lead`; `sample_test_data=true` on most rows; `source='google'` (inbound web-form attribution, not outbound). |
| `revenue_lead_activities` | 3 | 2026-05-30 | All `activity_type='note'`, tied to the QA leads above. |
| `growth_network_connections` | 1 | 2026-06-16 (single event) | Backlink/partnership-outreach feature (separate from sales outreach). `status='pending'`, `contact_email` blank. |
| `growth_network_connection_activities` | 0 | — | Empty — no reply/response ever recorded for the one connection. |
| `campaigns` | 9 | 2026-07-12 → 07-13 | Internal content/marketing campaign tracker, no email-send linkage. |
| `omni_campaigns` | 5 | 2026-07-03 → 07-12 | Cross-channel content distribution (video/social), not cold-email. |
| `trial_notification_events` | 0 | — | Transactional email log (has `sent_at`/`delivered`/`error` columns) — empty, confirms transactional email path has never fired either. |
| `agent_logs`, `seo_agent_logs` | 0 matches | — | Zero rows mention "outreach", "cold email", or "smtp". |
| `owner_experiments` | 0 | — | Empty. |
| `owner_opportunities` | 11 | 2026-07-12 → 08-13 | Unrelated growth-idea/changelog feed (e.g. "Bundle AI audit as a paid lead magnet", version bump entries); nothing about a cold-outreach campaign or its results. |

**Code**: `/opt/seo/backend/routes_clients.py` — `POST /api/clients/outreach` is explicitly documented in its own docstring as an **"outreach stub (logs activity)."** It looks up the lead, writes one `ClientLeadActivity` row, and returns. It never calls `email_service.send_email`, `smtplib`, or any provider. `/opt/seo/backend/core/email_service.py` is a real, provider-agnostic sender (SMTP/Postmark/SendGrid) used elsewhere in the app (trial notifications), but is never wired into the outreach flow.

**Env/config**: `seo-backend-1` container env, `/opt/seo/.env`, `.env.example`, `.env.production.example`, `backend/.env.example` — none define `SMTP_HOST`, `SMTP_FROM`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `EMAIL_PROVIDER`, `POSTMARK_API_KEY`, or `SENDGRID_API_KEY`. Only `ADMIN_EMAIL` is present (a receive address for admin alerts, not a sending identity). Per `email_service.get_email_provider_status()`'s own logic, this means the app's email provider is currently `"not_configured"`.

**Other observation (out of task scope, noted only)**: `docker ps` shows separate `prospect_backend` / `prospect_db` containers running on this host, unrelated to `seo-postgres-1`/`/opt/seo`. Not investigated per scope limits, but flagged as a candidate location for the real outreach system in a later phase.

## 2. Sent-Message Evidence

**No.** There is no table, log, or file anywhere in `seo-postgres-1` or `/opt/seo` containing actual sent cold-outreach message bodies tied to real recipients. The only "sent" artifact is the empty `client_lead_activities` table (0 rows) that the outreach-stub endpoint would have written to — meaning even the in-app placeholder was never exercised, let alone a real SMTP dispatch. The app ships 5 canned templates (`tpl_001`–`tpl_005` in `routes_clients.py`, e.g. subject `"Quick SEO wins for {{company}} — free audit inside"`) — these are boilerplate defaults, not evidence of what the owner actually sent. No real samples exist to quote.

## 3. Delivery-Signal Evidence

**None.** No SMTP/ESP provider is configured (see above) — `email_service.py`'s status check would currently report `not_configured`. No SPF/DKIM/DMARC references found anywhere in `/opt/seo/deploy` or `/opt/seo/docs`. `trial_notification_events` (the one table with `sent_at`/`delivered`/`error` columns) is empty. `analytics_daily_rollups.bounces` / `analytics_sessions.is_bounce` exist but describe web-traffic bounce rate, not email bounces — unrelated. No open/click tracking exists for any outbound email.

## 4. Gaps — What Diagnosis Cannot Answer From This Data

- **What was sent**: no real subject lines, copy, offer, or personalization recoverable — nothing exists in-system.
- **Who was targeted**: only fake/seed leads and QA test rows exist; no real prospect list, firmographics, or list-source quality data.
- **Delivery outcome**: zero delivery telemetry (bounced vs. delivered vs. spam-foldered vs. delivered-but-ignored) — the app-side sending pipeline has never even been configured.
- **Sending domain authentication/reputation**: no SPF/DKIM/DMARC config found; unclear whether this app's domain was even the one used.
- **Whether the campaign ran through this app at all**: strong evidence it did not — outreach endpoint is a documented stub, 0 activity rows, no provider configured. The real campaign most likely ran outside `/opt/seo` entirely (manual email client, a different tool, or possibly the separate `prospect_backend`/`prospect_db` system on this host).
- **Reply detection**: no schema anywhere has a "replied_at" field or inbound-email-parsing table, so even if sends happened elsewhere, there is no structured place a reply would be recorded in this system.

## 5. Prioritized Plan for Diagnosis Phase

1. **Locate where the campaign actually ran** (blocks everything else). Ask the owner directly, and/or scope a phase-2 recon into `prospect_backend`/`prospect_db` if the owner confirms that's the real outreach tool.
2. **Once located**, pull real sent-message samples and the recipient list from that source to assess copy/offer/targeting quality.
3. **Check that source's SMTP/ESP auth** (SPF/DKIM/DMARC, sender reputation) first — a *zero*-reply result (vs. merely low replies) usually points at deliverability (never landed) before copy/offer (landed but unpersuasive).
4. **Check the owner's actual sending inbox** for bounce-backs/auto-replies (owner-accessible only; not automatable from this recon).
5. **Only after deliverability is ruled in/out**, evaluate targeting and copy/offer using whatever real list and message samples are recovered in step 2.
