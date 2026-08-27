# Wake bridge: bounded fast retry for transient composer-lookup failures (event 10063)

**Date:** 2026-08-27/28 · **Scope:** Owner OS wake/delivery retry timing only. No
Chromium tab was driven or mutated as remediation or as a test of this fix — every
scenario below was exercised entirely through `wake_bridge`'s own audit tables
(`wake_audit`, `wake_delivery`, `wake_submitted`, `wake_expire_audit`,
`agent_alert_invalid`). No payment production, DNS, credential, or live-traffic
change.

## 1. Defect

Event **10063** (`gaika-server`, `agent_waiting_input`, route `gaika-extension`):

```
wake_audit    96540  decided 'wake'          2026-08-27T22:19:42Z
wake_send             claimed_actionable      2026-08-27T22:19:58Z
wake_delivery  3760  delivered=0 reason=composer_ambiguous_or_absent:0  22:20:30Z
wake_send             claimed_actionable      2026-08-27T22:25:50Z
wake_delivery  3762  delivered=1 reason=submitted_and_assistant_started_generating  22:26:03Z
```

The composer read **0** matches at 22:20:30 (page still rendering) and **1** a few
seconds after the next retry. That is not the wedged-page/dead-chat shape event 4214's
300s `RETRY_BACKOFF_SECS` floor exists for — but every failure reason shared that one
floor, so a live `agent_waiting_input` wake sat benched for **5m33s** before the
standard backoff cycle happened to land on a moment the composer was mounted. The
event self-recovered; the delay was not acceptable for an actionable wake.

## 2. Fix

`core/wake_bridge.py` — `pending_wake` and `pipeline_health` now compute retry
eligibility **per event** instead of against one fixed cutoff:

- `TRANSIENT_FAILURE_PREFIXES = ("composer_ambiguous_or_absent",)` — the only reason
  class treated as transient. Matched by prefix because `cdp_composer.py` appends the
  observed composer count (`:0`, `:2`, …).
- `TRANSIENT_RETRY_BACKOFF_SECS` (default **30s**, env
  `WAKE_BRIDGE_TRANSIENT_RETRY_BACKOFF_SECS`) — the fast lane's bench window.
- `TRANSIENT_RETRY_MAX_ATTEMPTS` (default **6**, env
  `WAKE_BRIDGE_TRANSIENT_RETRY_MAX_ATTEMPTS`) — after this many CONSECUTIVE transient
  failures for one event, it stands down onto the original `RETRY_BACKOFF_SECS` (300s)
  floor. `_consecutive_transient_failures` breaks the streak on any success or any
  *different* failure reason, so this is never a general failure counter.
- `expire_stale`'s `MAX_WAKE_AGE_SECS` ceiling and the `agent_alert_invalid`
  invalid-overlay check are unchanged and remain the final, unconditional stop —
  the fast lane cannot turn into an unbounded hot-loop even if a composer never
  resolves.

Everything else is untouched: `wake_target`/route resolution
(`wake_routes.resolve`, re-run per selection from the same project+agent inputs),
the submission latch (`mark_submitted`/`was_submitted`, fail-closed idempotency —
`composer_ambiguous_or_absent` fires *before* any keystroke, so it was never latched
and retrying it was always safe), `coalesce_generic_backlog`,
`_supersede_stale_actionables`, and the actionable/generic cooldown floors. Selection
is still bounded (`_CANDIDATE_SCAN_LIMIT = 200` oldest-eligible candidates scanned
per tick, actionable-first then oldest — the pre-existing order).

`pipeline_health` now derives its `pending`/`benched_after_failure` counts from the
same per-event eligibility helper the selector uses, so health reporting cannot
disagree with what the selector is actually doing.

`tools/wake_companion.py` — when a tick finds nothing pending because the head of
line is benched, it now logs which lane, the attempt number, and seconds to next
retry:
```
event 10063 benched (transient composer backoff, attempt 1, next retry in 24s)
```
Eventual success is already logged (`delivered wake for event …`); expiry/clearing is
recorded durably in `wake_expire_audit.reason` (`marked_invalid`,
`stale_past_max_age`, `event_older_than_max_age`) exactly as before.

## 3. Files changed

- `core/wake_bridge.py` — constants, `_is_transient_failure`,
  `_consecutive_transient_failures`, `_event_retry_backoff_secs`, rewired
  `pending_wake` selection loop, rewired `pipeline_health` eligibility.
- `tools/wake_companion.py` — benched-tick logging.
- `tests/test_wake_composer_transient_retry.py` — new, four regressions (below).

## 4. Tests

New file, exact event-10063 class:

- `test_ambiguous_composer_then_available_is_exactly_one_delivery` — attempt 1
  ambiguous, retried inside the fast lane once available → exactly one
  `delivered=1` row, `wake_delivery` history is `[0, 1]`.
- `test_repeated_ambiguity_falls_back_to_standard_backoff_not_a_hot_loop` — 7
  consecutive ambiguous failures; the 7th pushes the streak past
  `TRANSIENT_RETRY_MAX_ATTEMPTS`; the event is then benched on the **standard** 300s
  floor (not the 30s fast lane) but is still retried and offered again — bounded, not
  a dead letter.
- `test_condition_clearing_retracts_the_wake_before_the_fast_lane_fires_again` — an
  `agent_alert_invalid` row lands while the event is benched inside the fast-lane
  window; `pending_wake` retires it (`wake_expire_audit.reason=marked_invalid`)
  instead of ever offering or submitting it.
- `test_a_non_transient_failure_still_uses_the_original_floor` — `renderer_unresponsive`
  (event 4214's shape) is unaffected: still benched at fast-lane+1s, still retried only
  at the standard 300s floor.

Run log:

```
tests/test_wake_bridge.py ...................................... (40)
tests/test_wake_routes.py ........................... (27)
tests/test_wake_pipeline_health.py .................................. (34)
tests/test_wake_companion.py ........... (11)
tests/test_cdp_composer.py ........................ (24)
tests/test_wake_delivery_verification.py ................................. (33)
tests/test_wake_route_registry_and_dead_expiry.py ...................... (22) .....(5)
tests/test_api_wake_routes.py ........ (8)
tests/test_wake_actionable_transitions.py ............. (13)
tests/test_closed_loop_wake.py ...................... (22)
tests/test_wake_assistant_proof.py ....... (7)
tests/test_wake_composer_transient_retry.py .... (4)
248 passed in 91.27s
```

Full repo suite: **2506 passed, 1 failed** in 639s
(`tests/test_delivery_attribution.py::test_agent_send_threads_attribution_to_the_record`).
Confirmed pre-existing and unrelated: the same test fails identically with this
commit's changes stashed out (`git stash push` on just the three changed files, rerun,
`git stash pop`) — an HMAC-attribution kwargs assertion in the agent-send path,
nothing to do with the wake bridge.

## 5. Deploy

Committed `02c0702` — *fix(wake): bounded fast retry for transient composer-lookup
failures* — `core/wake_bridge.py`, `tools/wake_companion.py`,
`tests/test_wake_composer_transient_retry.py`. Nothing pushed.

Pre-change backup: `backups/wake_composer_transient_retry_20260827-224851/`
(`wake_bridge.py`, `wake_companion.py` from `HEAD`, plus `control_plane.db`,
`PRAGMA integrity_check` → `ok`).

Both processes that import `core/wake_bridge.py` were restarted (module is imported
once at process start and never re-imported per tick, so both need it):

```
systemctl restart owner-os-wake-companion
  → active (running), PID 1447942, since 2026-08-28 00:49:28 CEST
  → wake_worker row re-registered: started_at 2026-08-27T22:49:28Z, matching PID
  → no error lines in journal

systemctl restart ai-runtime
  → active (running), PID 1449926, since 2026-08-28 00:49:55 CEST
  → GET /api/v1/health → {"status":"ok","provider_available":true,...}
  → no error lines in journal
```

Unrelated working-tree entries deliberately excluded from the commit and left exactly
as found: `reports/OWNER_OS_WAKE_BRIDGE_REPAIR_2026-08-11.md`,
`reports/phase3_postfix_soak.jsonl` (both pre-existing modifications), and the
untracked `reports/OWNER_OS_EVENT_*`, `OWNEROS_EVENT_7409_DIAG.md`,
`FUNNEL_DATA_INVENTORY_2026-08-16.md`, `OWNER_OS_TELEGRAM_RECHECK_EVENT_7558_2026-08-22.md`,
`OWNER_OS_WAKE_REBIND_STATUS_2026-08-08.md` files.

## 6. Rollback

```
cp backups/wake_composer_transient_retry_20260827-224851/{wake_bridge.py,wake_companion.py} \
   core/wake_bridge.py tools/wake_companion.py   # adjust destinations per file
systemctl restart owner-os-wake-companion
systemctl restart ai-runtime
```
The new columns/tables introduced are none — this change is purely retry-timing
logic over existing tables, so a revert needs no data migration.

## 7. Still open

- No live end-to-end proof against a real composer-ambiguous event was collected
  post-deploy, deliberately: doing so would require either waiting for another
  organic transient failure or synthetically driving/mutating a Chromium tab to
  provoke one, and the latter is exactly the remediation style this task ruled out.
  The fix is proven at the unit level (§4) against the exact recorded shape of event
  10063's own audit rows.
- `TRANSIENT_RETRY_BACKOFF_SECS=30` and `TRANSIENT_RETRY_MAX_ATTEMPTS=6` are
  reasoned defaults (bounded to ~3 minutes of fast retries before standing down), not
  measured against a distribution of how long a ChatGPT tab typically takes to finish
  rendering after navigation. Both are env-overridable without a code change if the
  real-world window turns out to need tuning.
