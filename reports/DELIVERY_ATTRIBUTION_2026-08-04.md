# DELIVERY ATTRIBUTION — who sent a `deliveries` row

**2026-08-04.** Implements the recommendation left open by
`reports/ACTUATOR_BLIND_PANE_AND_DELIVERY_ATTRIBUTION_2026-08-04.md`: record the caller
on every delivery. Scope strictly limited to that. No deploy, no service restart, no live
pane contact, no autopilot change, no allowlist/env/unit change, no destructive / payment
/ credential / publication action, nothing pushed.

## Problem

`deliveries` stored only WHAT was delivered — `idempotency_key, target, action, result,
created_at, created_ts`. The API knew the authenticated principal and the client address
and discarded both before the write. Attributing the 2026-08-03T22:29–22:37Z rows
therefore required correlating three systems by hand (access log → docker network →
caller source code). The answer was legitimate — the owner's ChatGPT-MCP commander
channel — but nothing in the runtime recorded it.

## Design decision: sidecar table, not new columns

The recommendation said "add an `actor`/`source` column". Implemented as a **separate
table** instead, because the column form is unsafe here:

The **currently running service** (one version behind by owner decision) writes with a
POSITIONAL statement — `INSERT OR REPLACE INTO deliveries VALUES (?,?,?,?,?,?)`
(`core/agent_control.py:616` at `45cfb37`). Adding two columns makes every one of its
writes fail with *"table deliveries has 8 columns but 6 values were supplied"*. The
migration runs on first open by whichever build touches the DB, so the column form would
break the owner's live command channel — either immediately if the new code opened the
live DB, or on any rollback to the running build.

A sidecar is compatible in both directions: old code never sees it, new code fills it,
and no delivery can fail because of attribution.

| File | Change |
|---|---|
| `core/agent_control.py:297-310` | `_migrate_delivery_attribution` — `CREATE TABLE IF NOT EXISTS delivery_attribution (idempotency_key PK, actor, source, recorded_at, recorded_ts)`, idempotent, called on every `_db()` open. `deliveries` schema untouched |
| `core/agent_control.py:640-666` | `_record_delivery(..., actor, source)` — delivery row written with NAMED columns; attribution written to the sidecar inside its own try/except that degrades to an audit line, so it can never fail a delivery |
| `core/agent_control.py:668-688` | `delivery_attribution(key)` — the "who sent this?" lookup |
| `core/agent_control.py:628-633` | TTL sweep prunes the sidecar on the same retention as the rows it describes |
| `core/agent_control.py` `agent_send` / `agent_answer` / `_deliver` | optional `actor` / `source` pass-through; every existing caller is unaffected (defaults `None`) |
| `api/v1.py:43-52` | `_auth` records which method it accepted (`hmac` / `bearer`) on `request.state` |
| `api/v1.py:55-95` | `caller_identity(request, declared)` → `(actor, source)`; never raises |
| `api/v1.py:290-303` | `/agents/send` and `/agents/answer` compute and pass it |

**What is recorded.** `actor` = `api:<auth-method>` plus an optional caller-declared name
from an `X-Runtime-Actor` header, e.g. `api:hmac/chatgpt-mcp`. `source` = client address
and port plus a truncated user-agent, e.g. `172.20.0.2:59342 ua=python-httpx/0.27`.

**Trust boundary, stated explicitly.** The auth-method prefix is proven; the declared name
is self-asserted and sanitised (`[^A-Za-z0-9 ._:@/+-]` stripped, 64 chars) purely so it
cannot inject into the record. **No safety gate reads either field** — this is
observability, not authorisation. An internal in-process caller records nothing rather
than a fabricated identity.

## Tests — `tests/test_delivery_attribution.py`, 17 tests

Baseline proof in a `git worktree` at `0839ff3` with `tests/conftest.py` sys.path **and**
PYTHONPATH repointed, import origin asserted (`pre-fix has delivery_attribution: False`).

**15 FAIL on pre-fix `0839ff3`.** **2 pass both sides by design**: legacy rows survive the
migration, and a pre-migration key stays visible to the dedupe path (that one must hold on
both sides — if it ever failed, migrating would silently re-deliver every message whose
key predates it).

Notable pins:
- `test_old_positional_insert_still_works_after_migration` — the rollback pin: the running
  build's 6-value positional INSERT still works on a migrated DB.
- `test_migration_adds_the_sidecar_and_leaves_deliveries_untouched` / `test_migration_is_idempotent`.
- `test_attribution_failure_never_fails_the_delivery` — with the sidecar deliberately
  broken (wrong shape, NOT NULL), the delivery is still recorded.
- `test_declared_actor_is_sanitised_and_bounded` — newline / quote / `;` / length injection.
- `test_caller_identity_never_raises_on_a_broken_request` — `None` request and a request
  whose `state`/`client` raise both degrade to `api:unknown` / `unknown`.
- `test_auth_records_the_method_it_accepted` — HMAC and bearer paths both set it.
- `test_the_investigated_row_shape_is_now_answerable` — the original
  `owneros-cancel-wrong-deploy-selection-…` question is one lookup.

**Full suite: 1200 passed, 0 failed** (1183 before). No existing test or fixture changed.

## Limitations

- Attribution starts now: the 2026-08-03 rows stay unattributed in the DB (their origin is
  documented in the investigation report, not backfilled — inventing rows would be worse
  than the gap).
- `actor`'s declared-name half is self-asserted; only the auth method and the client
  address are independently observed.
- Deliveries made by in-process callers (watchdog, autopilot, actuator) record no
  attribution — they have no external principal. Their audit trail is `cp_action` /
  `cw_step` / `autopilot_run`, which is what distinguished them during the investigation.
- **Not live.** The running service (PID 4063628, started 2026-08-04 00:29:37 CEST) still
  runs `45cfb37`; this commit, like the four before it, takes effect only on an
  owner-approved restart. The live `deliveries` table is still 6 columns and untouched.

## State

- HEAD before: `0839ff3`. Suite 1183 → **1200 passed, 0 failed**. Working tree clean.
- Live service unchanged; autopilot dormant; canary allowlist `cp-canary:0.0`.
