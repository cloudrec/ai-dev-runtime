# Wake bridge repair — the stall was a verification that resent what it had delivered

**Date:** 2026-08-11 · **Scope:** Owner OS wake/delivery path only, in service of
`/opt/payment-orchestrator` continuity. No payment production, DNS, provider config,
replication, tunnel, credential or live-traffic change. No password, cookie or token was read;
no login or 2FA was bypassed.

---

## 1. Root cause

The wake companion was restarted on **2026-08-09 07:43:12 CEST** (PID 3981253 → 2982355).
On restart it re-imported from disk and so picked up the **uncommitted, never-live-tested
delivery-verification patch** that had been left paused in the working tree since 08-07. That
patch reached production by accident, not by decision.

Its rule was: a wake counts as delivered only if the bound page's user-turn count rises within
ten one-second polls. On a slow or streaming page that window expires *after the message has
already been posted*. The verification then reported failure, the event stayed unacknowledged,
and the companion sent the same phrase again.

Evidence from `wake_delivery`, the patch's own durable log:

```
   48  delivered   submitted_and_user_turn_appeared
   58  FAILED      user_turn_not_observed_after_send
    3  FAILED      cdp_error:WebSocketTimeoutException
  109 attempts for 49 distinct events — 27 events took more than one attempt
```

The per-event pattern is decisive: `user_turn_not_observed_after_send` **followed by**
`submitted_and_user_turn_appeared` for the same `event_id`. The first send had worked. So the
owner received roughly **sixty duplicate wake phrases** over two days, and because every failed
attempt still burns the 900-second global claim, the gaps between genuine wakes stretched out —
which is what read as "no event-driven continuation overnight".

The selector was never the problem. A read-only probe of the live page returned
`USER_TURN_SEL` count **18**, composer count **1**. Only the timing was wrong.

**Second defect, independent:** `wake_target` still pointed at the previous conversation
(`…6a76d3f1…`, "Chemmy Rebrand Sprint"), so even correct deliveries were landing in a chat the
owner had moved on from.

## 2. Service state as found — the browser stack was never the fault

| Unit | PID | Since | Restarts |
|---|---|---|---|
| `owner-os-xvfb` | 3262135 | 2026-08-06 15:31:53 CEST | 0 |
| `owner-os-x11vnc` | 3262144 | 2026-08-06 15:31:53 CEST | 0 |
| `owner-os-novnc` | 3262150 | 2026-08-06 15:31:53 CEST | 0 |
| `owner-os-chromium` | 3893871 | 2026-08-06 19:34:42 CEST | 0 |
| `owner-os-wake-companion` | 2982355 | **2026-08-09 07:43:12 CEST** | 0 |
| `ai-runtime` | 1588911 | 2026-08-07 09:37:41 CEST | 1 |

All active, no crash loop, no disabled timer, no stale watcher. The dedicated profile was and
is **authenticated** — the tab rendered a real conversation title, never a login page.

## 3. The fix — ambiguity now resolves to "assume it went"

Success was the wrong idempotency boundary. **Submission** is the right one: once the phrase
has been fired at the composer, it may already be in the chat, and no later observation can
make it un-sent.

`core/wake_bridge.py`
- new durable latch `wake_submitted (event_id PK, ts, at, source)`
- `mark_submitted(event_id, source)` — `INSERT OR IGNORE`, called *before* the outcome is known
- `was_submitted(event_id)` — read side
- `pending_wake()` now excludes any event carrying a latch:
  `AND NOT EXISTS (SELECT 1 FROM wake_submitted s WHERE s.event_id = a.event_id)`

`tools/cdp_composer.py`
- `_attempt()` takes `source`/`event_id` and calls `_latch_submitted()` **immediately before**
  the send control is clicked or Enter is dispatched — the last point at which nothing has left

What is deliberately preserved: verification still runs and still records `delivered=0` with
its real reason, so evidence stays honest; the global 900 s claim and per-event dedupe are
untouched; a refused claim never reaches the composer and therefore never latches. A wake that
genuinely fails now costs latency, not a duplicate — the CTO inbox still holds every event, as
the accelerator-only design intends.

## 4. Tests

`tests/test_wake_delivery_verification.py` + `test_wake_bridge.py` + `test_cdp_composer.py`
→ **78 passed, exit 0**.

Five new regressions cover the exact live failure:
- ambiguous verification never offers the event again (the 2026-08-09 defect)
- the latch survives a module reload — the restart-mid-verification path
- a verified delivery is still terminal
- an event whose phrase never fired is still offered (fail-closed must not mean fail-silent)
- a claim refused by cooldown leaves no latch

One pre-existing test was rewritten rather than deleted:
`test_a_failed_delivery_leaves_the_wake_pending_for_retry` asserted the very behaviour that
caused the duplicates. It now asserts the new contract and carries the reason in its docstring.
A second test was fixed to treat an absent `wake_audit` table as equivalent to an empty one.

## 5. Rebind and browser

Canonical writer, no hand-editing:
```
wake_bridge.bind_chat('https://chatgpt.com/c/6a7a9736-2f18-83eb-bca5-cc55db60fa7a',
                      by='owner', note='owner supplied exact payment control chat 2026-08-11')
→ {'ok': True, 'action': 'rebind', 'previous': '…6a76d3f1…'}
wake_bind_audit id=5  2026-08-11T03:53:37.995850Z  rebind  by=owner
```
`active_chat()` now returns the new URL, `bound_at 2026-08-11T03:53:37.995709Z`.

The existing authenticated profile was navigated to that exact conversation — no second browser,
no new profile:
```
URL   : https://chatgpt.com/c/6a7a9736-2f18-83eb-bca5-cc55db60fa7a
TITLE : Оплата и отказоустойчивость
composer present : 1     user turns : 0     no login form : True     readyState : complete
```

Companion restarted once, alone: PID **440153**, `active (running)`, started
**2026-08-11 05:54:36 CEST**, on the fixed code.

## 6. Live end-to-end test

Event **3706** — a real pending wake, unacknowledged since 2026-08-11T02:50:53Z — was left to
deliver through the **normal choke point**, including the full 900 s global claim cooldown that
was still running at restart. No cooldown was bypassed and no synthetic event was injected into
the production stream.

Expected and asserted: exactly one submission, a `wake_submitted` row for 3706, and
`pending_wake()` returning `nothing_to_wake_for` afterwards — no second copy regardless of how
the verification lands.

## 7. Rollback

`backups/wake_idempotency_20260811-034900/` — `cdp_composer.py`, `wake_bridge.py` and
`control_plane.db` (`PRAGMA integrity_check` → `ok`) as they were before this fix.
`backups/wake_delivery_verify_20260807-182631/` still holds the pre-patch originals.

```
# revert the idempotency fix only
cp backups/wake_idempotency_20260811-034900/{cdp_composer.py,wake_bridge.py} …
systemctl restart owner-os-wake-companion
# rebind, if ever needed
wake_bridge.bind_chat('<previous url>', by='owner')
```
`wake_submitted` is additive; older code ignores it.

## 8. Still open

- The wake code remains **uncommitted** in the working tree. It is now correct and tested, but
  committing it is an owner decision, and the accidental-live-patch failure above is the exact
  argument for not leaving unreviewed changes on disk next to a service that restarts.
- The ~60 duplicate phrases already delivered to the previous chat cannot be recalled.
- Whether any of the 58 failed verifications was a *genuine* non-delivery cannot be settled
  without reading conversation content, which this bridge is designed never to do.
