# Wake send correctness: D1/D2 from the acc-20261118 acceptance (2026-11-18)

Trigger: an automated instruction was received (Owner OS API). It is not owner approval.

Scope:
- DEV only, with synthetic mocked browser fixtures.
- No live browser or chat was inspected, no PROD change, no push, no new agent.
- No access, secret or payment agent was touched.
- The acc-20261118 evidence is intact: directory, repo, DB rows.

## Candidate
| | SHA |
|---|---|
| Live runtime | 0a0c456 |
| **Candidate** | **2883d2b** (code `f0619bc`, plus a test-only follow-up), branch `owneros/autonomy-loop-20261117` |

Diff `0a0c456..2883d2b`, saved as `reports/loop/send_intent_2026-11-18/runtime.diff` (6 files, +945 −11):
- `core/wake_bridge.py`;
- `core/closed_loop_wake.py`;
- `tools/cdp_composer.py`;
- `tools/wake_companion.py`;
- the new `tests/test_wake_send_intent.py`;
- `tests/test_composer_cdp_timeout_posthoc.py`.

## Defects
- **D1, blind resend.**
  - `wake_submitted` latched only once the composer was seen cleared.
  - A CDP timeout, or a process death, after the click and before that point left no record, so the event was offered again.
  - Event 88629: attempt 1 timed out after dispatch at 07:15:15Z, and attempt 2 re-sent it at 07:21:30Z.
  - An unreadable composer (`None`) was also read as "provably not sent".
- **D2, no late receipt.**
  - A send that landed while the page was silent stayed "not delivered" forever (88652, 88629). Nothing ever looked again.

## Fix
**Durable intent.**
- `wake_send_intent` gets a row, committed before `Input.insertText`.
- If the row cannot be written, nothing is typed: `send_intent_unavailable`.

**Three outcomes per attempt:**

| Outcome | When | Latch | Re-offered |
|---|---|---|---|
| `delivered` | the assistant started (the existing proof) | yes | no |
| `not_sent` | **proven**: nothing dispatched, or the page *answered* that our phrase is still in the composer and in no user turn | no | yes, through every filter |
| `indeterminate` | dispatched, no proof either way (timeout, silent page, `None` answers) | yes | **never blindly** |

Transitions are one-way and audited in `wake_send_intent_audit`:

| From | Allowed moves |
|---|---|
| intent | not_sent, indeterminate, landed, delivered |
| indeterminate | not_sent (proof only), landed, delivered |
| landed | delivered |

**Crash or restart.**
- An open `intent` blocks any new send of the event (`begin_send`, and the `pending_wake` filter).
- After `WAKE_INTENT_STALE_SECS` (300 s) it becomes `indeterminate` and is latched.
- The companion then registers its unverified closed-loop watch, so re-wake and escalation still apply.

**Duplicate event.** `begin_send` decides under `BEGIN IMMEDIATE`. A second attempt is refused before typing, whether it is concurrent, after a crash, or already latched.

**Reconciliation** (companion, every tick):
- **Read-only:** nothing is typed, clicked or navigated.
- **Bounded:**
  - first look ≥ 60 s after the send;
  - ≥ 120 s between looks;
  - at most 20 looks and 3 events per tick;
  - only inside the wake age window.
- **Evidence:** three booleans about this event's own marker `[Owner OS wake] event=<id> `. No other text is read or returned.

| Evidence | Verdict |
|---|---|
| marker in a user turn, assistant turn after it | `delivered`: delivery row `reconciled_assistant_answered`, acknowledged, watch `delivery_verified=1` (late receipt) |
| marker in a user turn only | `landed`: still latched, never re-sent |
| marker in the composer, in no user turn | proven `not_sent`: the latch is lifted (audited) and the event is offered once more |
| no tab, silent page, unreadable, marker nowhere | unchanged: stays `indeterminate`, watched, and abandoned visibly after `MAX_WAKE_AGE_SECS` (`submitted_delivery_unproven`) |

**Unchanged:**
- dedup, cooldowns and claims;
- payment and offer filters (a re-offer goes through `pending_wake` again);
- permissions;
- the closed-loop re-wake and escalation;
- the abandonment record;
- the result dicts callers match on.

## Tests
- **New file `tests/test_wake_send_intent.py`, 29 tests.** Synthetic pages, a per-test DB. Cases:
  - 88629 replay;
  - click timeout;
  - silent and unreadable pages;
  - proven not-sent and the retry;
  - `None` is not proof;
  - crash then restart;
  - stale conversion;
  - race;
  - latch;
  - DB write failure;
  - one-way moves;
  - late receipt;
  - landed;
  - reconcile not-sent and re-offer;
  - four no-evidence pages;
  - unreadable evidence;
  - probe privacy;
  - bounds;
  - abandonment;
  - the died attempt's watch.
- **On base 0a0c456: 28 fail, 1 passes.** The one is the unchanged result-dict shape.
  - The 88629 replay fails on the behaviour itself: `was_submitted(88629)` is False, so the event could be offered again.
- **Changed tests in `test_composer_cdp_timeout_posthoc.py`:** two pinned the D1 behaviour ("no evidence → not latched, retryable"). They now assert `indeterminate` and latched. The file also gets a per-test DB.
- **Mutants, all 17 killed** (scratch worktree):
  - no intent;
  - dispatched counted as not_sent;
  - indeterminate without latch;
  - pending ignores intent (run by hand: the scripted pattern missed);
  - begin allows any;
  - not-sent ignores the turn;
  - `None` as proof;
  - a turn counted as delivery;
  - retract from any state;
  - no stale conversion;
  - no look cap;
  - no watch for a died attempt;
  - no post-hoc not-sent;
  - non-bool evidence accepted;
  - not_sent after the composer was seen cleared;
  - no fail-closed on an intent write error;
  - no acknowledgement on a late receipt.
- **Related suites** (composer, bridge, companion, delivery, closed loop, rotation, session watch, e2e): 597 passed.
- **Full suite at 2883d2b:** 5522 passed, 4 skipped, EXIT 0 (`reports/loop/send_intent_2026-11-18/results.txt`).

## Remaining risks
1. A send that is clicked and then really lost, with the composer cleared and no turn, stays `indeterminate`: no resend, by design. It surfaces through the closed-loop watch (one re-wake, then escalation) and the abandonment record. That costs latency, not a duplicate.
2. If a later wake to the same chat replaces our stranded phrase in the composer, which the draft guard allows for our own phrase, the not-sent proof is gone and the event stays `indeterminate`.
3. Reconciliation needs the conversation's tab. With no tab, nothing is decided.
4. The cause of the frequent post-send CDP timeouts (D2's trigger) is still unmeasured. This fix makes their outcome correct, not rarer.
5. The marker check reads whether user-turn text contains a string this module wrote, one bit per question. That stays inside the module's existing rule ("only bits about strings it already holds").

## Not covered (D3, from the acceptance)
Reject, rework and `add_tasks` remain **untested live**. The acceptance report is accepted for the two successful tasks only, and transport is **not** claimed fully accepted.

## Release, when authorised (non-payment Owner OS)
`TARGET_SHA=2883d2b deploy/adaptive_offer/install.sh --check`, then `--apply`.
- Schema: two new tables, created on first use.
- The companion restarts with the deploy.
- Rollback: through the backup the installer prints.

## Released 2026-11-18 08:09:02Z (owner's direct word "деплой 2883d2b")
- `install.sh --check` clean (payment panes never offered), then `--apply`: live 0a0c456 → 2883d2b.
- Rollback: `deploy/adaptive_offer/rollback.sh /root/ai-dev-runtime/backups/adaptive-offer-20261118T080902Z`.
- Units active (ai-runtime, wake-companion, fullcycle-intake, fullcycle-worker). Tables `wake_send_intent` and `wake_send_intent_audit` were created on the first tick. 0 companion errors; `loop_watch.check`: 0 problems.
