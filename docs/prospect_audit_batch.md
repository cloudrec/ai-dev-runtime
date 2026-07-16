# Prospect Audit batch runner

`core/prospect_audit_batch.py` generates a production batch of prospect audits
from the confirmed read-only source (`company_websites WHERE status='active'`)
and hands the results to the email project as JSON records. It never sends
email, never submits a form, and never writes to the email database.

## Run a batch

```bash
python3 -m core.prospect_audit_batch \
  --source-db /opt/prospect-audit/data/prospects.db \
  --batch-size 25 \
  --batch-id batch-2026-07-16-001
```

The command prints a JSON summary and exits non-zero if any prospect failed.

## What it produces

For each prospect, under `var/prospect_audit/batches/<batch-id>/`:

* `short/<slug>-<token>.html` — the teaser report with the top findings and a
  link to the full report and the contact form.
* `full/<slug>-<token>.html` — every check, the Clients.Help widget snippet, the
  contact-form link and the permanent public URL.
* `outreach/<slug>-<token>.txt` — the text to paste when reaching out manually.

And under `var/prospect_audit/handoff/<batch-id>/`, one JSON record per prospect
for the /opt/email pipeline to consume. Every record carries
`delivery.send = false`: sending decisions stay with the email project.

The operational report lands in `reports/runtime/PROSPECT_AUDIT_BATCH_<date>.md`
and `.json` with the counts, per-prospect rows, failures and output paths.

## Public URLs

The URL is `<public-base>/<company-slug>-<token>` where the token is the first
16 hex characters of `sha256(domain|url_salt)`. This is deterministic (a rerun
of the same batch regenerates identical URLs) but unguessable without the salt.
Change `--url-salt` only when you intend to invalidate previously shared links.

## Safety rules the code enforces

* The source database is opened with `file:...?mode=ro`; any write attempt
  raises `sqlite3.OperationalError`.
* Output paths under `/opt/email` are refused with `BatchSafetyError`, so the
  email database cannot be reached from here.
* `send_emails=True` aborts the batch. The module imports no mail transport or
  HTTP client, and a test asserts that stays true.
* One malformed row is recorded in `failures` and does not abort the batch.
