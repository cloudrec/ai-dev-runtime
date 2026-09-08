# Owner OS handoff — 2026-09-05

State, not narrative. Current facts only. First written from the repo and runtime at
~05:15Z; the Repo, Tests, Services, Browser and Next-safe-step sections were rewritten at
~08:55 CEST / 06:55Z, finalised at ~09:25 CEST / 07:25Z once both long-running checks had
actually finished, extended at ~09:40 CEST / 07:38Z when the first live recovery was
observed, and closed out at ~10:55 CEST / 08:55Z with the one-hour regrowth watch's final
summary and the host-memory spike that resolved inside it. The 05:15Z browser state is kept at the end of that section for the
record. Nothing here is reported as a pass that was not observed exiting.

Automated Owner OS API instructions drove this session; those are not owner sign-off. One
automated instruction asserted that an owner-typed instruction was present in the pane;
that was NOT independently verified and is recorded throughout only as "an automated
instruction was received". Owner decisions typed in the pane are marked as such where they
were verified.

## Repo

| | |
|---|---|
| Branch | `ai-runtime/220-windows-bridge` |
| HEAD | `9177606` — **3 unpushed** docs commits (`105f731`, `3f009d5`, `9177606`); push is an owner gate |
| Upstream | `origin/ai-runtime/220-windows-bridge` |
| Tracked tree | clean |
| Untracked | 34 files, all under `reports/` — preserve, never `git add reports/`; every commit below staged EXPLICIT paths |

16 commits landed on this branch since `e60a00b`: 4 fixes, 12 reports/handoff revisions.
The load-bearing ones, newest first:

```
5a9f015  the tab accrual, proven rather than suspected            (report)
ffe63c2  a create that timed out still made the tab               <- the leak, fixed
```

and from earlier in the session:

```
842ded3  a close the browser accepted is not a close that happened
6a51582  a ChatGPT page that cannot answer is not a usable page
42870de  the cleanup close needs the retry the old-tab close has
0988577  credentials must not reach the event store unredacted
2288f7c  the pane excerpt and the input line are ingress too
81778e6  queued task text reaches the event store too
52ab25d  a pane the stall doctor already escalated is not stalled
b91f477  dormant fail-closed native-continuation verifier
40159a4  give the effectiveness verifier a caller
858c357  attribute turns to the agent, not merely to its project
```

## Tests at this HEAD

```
234 passed  test_continuation_verifier, test_agent_control, test_owneros_hook, test_os_task_queue
197 passed  test_continuation_verifier, test_control_plane_diagnostics, test_closed_loop_wake
111 passed  test_cdp_composer                                      (ffe63c2, 8 new)
358 passed  test_cdp_composer, test_wake_delivery_verification, test_wake_pipeline_health,
            test_control_plane_diagnostics, test_closed_loop_wake
 22 passed  test_wake_assistant_proof, test_wake_companion, test_wake_composer_transient_retry
```
Each fix was confirmed by removal (revert it, the test fails). Every suite that imports
`cdp_composer` is in the three runs above, so `ffe63c2` has its full targeted coverage.

**Full suite GREEN at this HEAD: `3128 passed, 0 failed, 0 errors, 1 warning` in 24:22.**

The one warning is pre-existing and unrelated — a `tarfile` `DeprecationWarning` about
Python 3.14 extraction filters, from `test_core.py::TestBackupEngine::test_rollback`.

3128 against the last known green of 3049 on 2026-09-04 is **+79**, of which 8 are the new
tab-leak tests; the rest landed in this session's earlier commits, which had never been
measured against a full run until now.

An earlier attempt was killed by its own 25-minute `timeout` (exit 143 = SIGTERM) and
produced no result either way. The real run takes 24:22, so that budget was about a minute
short of the finish; it was never a failure.

## Services

```
ai-runtime   PID 1196430  up 2026-09-05 06:05:35 CEST  active   (was 2690604, up 09-02)
companion    PID 529251   up 2026-09-05 20:39:38 CEST  active   (was 4170370, up 18:25)
```

**Deploy skew CLEARED.** Owner typed "restart ai-runtime". The first hourly tick after it,
event 31374 (04:10:45Z), carried `cdp_same_chat` and the corrected wording; the newest,
31513 (05:11:08Z), still does. Every event before the restart had 4 capabilities and no
`cdp_same_chat`. Handoff-2026-09-04 blocker 2 is closed.

The 05:28 companion restart (owner typed "restart the companion") put all eight fixes into
a running process for the first time — verified by introspecting the loaded modules.

The companion was restarted AGAIN at 08:28:39 CEST, to put `ffe63c2` live. An automated
instruction asked for it; that is not owner sign-off. `ai-runtime` was NOT restarted.
Evidence the running process carries the fix: source finalised 08:20:34,
`tools/__pycache__/cdp_composer.cpython-312.pyc` compiled 08:20, process started 08:28:39,
`NRestarts=0`. Autonomy alive on the first ticks — `native-supervisor: continued
security-demo:0.0 from event 31638`.

## Canary retargeted to the live gaika agent (SUPERSEDED — see hostsecure below)

Owner typed "retarget the canary to gaika-opus-v6". The companion restart that loaded it
arrived through the automated Owner OS API channel — an automated instruction was
received; that part is not owner sign-off.

**Why.** `NATIVE_CANARY_TARGET` still named `gaika-opus-v5:0.0`, which had been retired:
last event 2026-09-05 08:13:16Z, no tmux session. `native_continuation_effectiveness()`
therefore read `verdict=proven, streak=3/3, {verified 13, unverified 7, unattributable 1}`
— counts that had not moved in over two hours and could never move again, because the
agent producing them no longer runs. Green with nothing behind it, which is the more
misleading direction of failure. The live agent is `gaika-opus-v6:0.0` (tmux session
created 09:27:24), already `covered` under `NATIVE_SUPERVISOR_TARGETS=*`.

**The change.** One line in `configs/.env`, verified as exactly two diff lines against the
backup:

```
NATIVE_CANARY_TARGET=gaika-opus-v5:0.0  ->  gaika-opus-v6:0.0
```

`NATIVE_SUPERVISOR_TARGETS`, the denylist, routes and every other line were untouched.

**Skew closed.** Before the restart the running process (PID 1770858) still held
`NATIVE_CANARY_TARGET=gaika-opus-v5:0.0` — read from `/proc/<pid>/environ`, that variable
only, out of 40. After the restart PID 2889296 holds `gaika-opus-v6:0.0`. `ai-runtime` was
NOT restarted and still runs PID 1196430 from 06:05:35.

**Verified after the restart:**

```
service      active (running) since 13:00:07 CEST, NRestarts=0
canary       target gaika-opus-v6:0.0 · verdict unproven · streak 0/3 · counts {} · active
coverage     covered 5 · denied 5 · uncovered 0
             gaika-opus-v6 · hostsecure · mess-postsignup-cleanup-sonnet-v4
             mess-safe-finish · security-demo
browser      8 pages · headroom 4 · degraded=False
delivery     six consecutive submitted_and_assistant_started_generating after the restart
             (11477-11482), no regression
notifications red — Telegram gate, unchanged
```

`unproven` with zero samples is the CORRECT post-retarget state, not a regression: it is
the fail-closed rule starting from no evidence. It will earn a verdict from
`gaika-opus-v6`'s own turns, needing a streak of 3 inside the 3600 s window. The previous
`proven` was the untrustworthy reading.

**Rollback.** `backups/canary_retarget_20260905T104016Z/` — `.env.before` (mode 600) plus
`ROLLBACK.md` carrying both the one-line undo and the whole-file restore. The verifier
writes nothing and adds no schema, so there is no data to undo; a companion restart is
required either way.

## Canary retargeted again — `hostsecure:0.0` (current)

Owner typed "retarget the canary to hostsecure", then "restart the companion".

```
NATIVE_CANARY_TARGET=gaika-opus-v6:0.0  ->  hostsecure:0.0     (one line, 2 diff lines)
NATIVE_SUPERVISOR_TARGETS=*             unchanged
NATIVE_CANARY_TIMEOUT_SECS=3600         unchanged
companion PID 4170370 -> 529251, active since 2026-09-05 20:39:38 CEST
loaded NATIVE_CANARY_TARGET=hostsecure:0.0  (read from /proc/<pid>/environ, that var only)
ai-runtime PID 1196430 untouched
```

**Why.** `gaika-opus-v6` was a thin instrument: 5 continuations total, streak stuck at 2/3
with one sample expired past its window. `hostsecure:0.0` reads 275 samples / 239 verified
/ 32 discarded `unattributable` / streak 201 / `matched_by=agent` / verdict `proven`.

**CAVEAT, recorded rather than buried.** This target was chosen AFTER its verdict was known
to be `proven`. That is selection bias: the `proven` reading is not fresh evidence the
canary gathered, it is what the read-only survey had already established hours earlier.
What the retarget buys is a better instrument going forward, not a new result.

Rollback: `backups/canary_retarget_hostsecure_20260905T182744Z/` — `.env.before` (mode 600)
plus `ROLLBACK.md` carrying the one-line undo, the whole-file restore and this same caveat.

## The self-agent external wake — `стоит агент` no longer required

An automated instruction was received asking for this; that is not owner sign-off. The
owner's complaint it addresses: Owner OS's own Claude agent reaches `waiting_input`/idle
and nothing continues it until the owner types `стоит агент` by hand.

**The delivery path was never the defect.** Traced read-only:

```
event 33019   14:30:20Z  agent_waiting_input (self agent, severity high, oar=1)
wake_audit    14:30:20Z  decision=wake  reason=actionable_waiting_transition  route=owner-os
wake_delivery 14:31:43Z  delivered=1  submitted_and_assistant_started_generating -> 7789-9b28-83
```

Every recent self-agent stop was decided, routed and delivered to the bound Owner OS
conversation — 41 deliveries on the `owner-os` route in three hours. `SELF_PROJECT`
denial held throughout; the self agent is never natively supervised.

**The defect was the INSTRUCTION on arrival.** `compose_phrase` sent the base
`WAKE_PHRASE` — "check the new Owner OS events and continue permitted work" — which
invites an acknowledgement and permits stopping there. That is exactly what the supervisor
did, so the owner typed `стоит агент` to force it.

**The fix, `core/wake_bridge.py`:** `SELF_WAKE_FLOW`, a FIXED system-authored constant,
appended only when the wake is for the self agent AND its trigger class is one the
supervisor should act on. It names the steps: read `agent_status`, read recent output,
answer waiting input already covered by owner decisions or safe policy, continue the
remaining authorized work, stop only at a genuine owner gate.

Scope is deliberately narrow, and the tests pin each boundary:

* `is_self_agent()` asks the SAME source the denial uses,
  `native_supervisor._project_for_target`, so the two can never disagree — the event's own
  `project_id` is not authoritative (self-agent events carry `owner-os-opus-fresh`, the
  denial keys on the checkout `ai-dev-runtime`). It FAILS CLOSED: unresolvable -> ordinary
  phrase.
* Non-self routes are byte-for-byte unchanged. This is not a global loosening.
* `owner_decision` is EXCLUDED from the flow set — that class IS the genuine gate, and
  telling the supervisor to push through it would be the paper-over this must not become.
* An unknown or mangled event type falls to `trigger=event` via the closed lookup and gets
  no flow, so a corrupted type cannot talk the supervisor into acting.
* The flow interpolates NOTHING, preserving the module's injection defense. Hostile input
  in the agent field is reduced to inert identifier characters:
  `owner-os-opus-fresh:0.0IGNOREPREVIOUSrm-rfcurlevil` — no newline, no separator, no
  shell metacharacter.

**The hard invariant is untouched:** `SELF_PROJECT` remains in
`AUTO_REGISTER_DENY_PROJECTS`, asserted by its own test. This is an EXTERNAL instruction to
the supervisor; it grants Owner OS nothing over itself.

**Tests:** 6 new in `tests/test_wake_bridge.py`, all confirmed by removal — with
`core/wake_bridge.py` reverted they fail 6 of 6.

Regression across the 17 suites that import `wake_bridge`, `native_supervisor` or
`continuation_verifier`: **708 passed, 1 failed**. The failure is
`test_continuation_verifier.py::test_the_repo_ships_with_no_canary_selected` and it is
PRE-EXISTING, not caused by this change — proven by replaying the exact suite ORDER that
failed with this change stashed: `1 failed, 447 passed` at HEAD. It is order-dependent
pollution: some earlier suite leaves `NATIVE_CANARY_TARGET` set in `os.environ`, and the
assertion only fires when `test_continuation_verifier` runs after it. The test passes in
isolation and under other orderings. Its premise is also no longer true of this working
tree by owner decision — a canary IS selected (`gaika-opus-v6:0.0`), which is exactly what
it asserts against. Left alone here rather than mixed into this fix.

**Loaded. Owner typed "restart the companion".**

```
companion  PID 2889296 -> 4154783 -> 4170370   active since 2026-09-05 18:25:24 CEST
ai-runtime PID 1196430 unchanged (06:05:35)   — NOT restarted
source core/wake_bridge.py mtime 17:13:02, pyc compiled 17:13, process start 18:21:24
loaded module: SELF_WAKE_FLOW present, is_self_agent present,
               flow classes ['blocker','completion','failure','loop_watchdog']
coverage covered=6 denied=5 uncovered=0 · browser 8 pages headroom 4 degraded=False
```

**The restart immediately exposed a defect the tests had hidden.** Composing the live
phrase for the self agent showed the flow was NOT appended:

```
is_self_agent('owner-os-opus-fresh:0.0', 'owner-os-opus-fresh') -> False
ns._project_for_target('owner-os-opus-fresh:0.0')              -> ''      <- the miss
agent row project_id                                            -> 'ai-dev-runtime'
```

`_project_for_target` reads the SUPERVISOR REGISTRY, and the self agent is DENIED from
supervision, so it may never have a row there. It returned `""`, and `is_self_agent` fell
through to the event's `project_id` (`owner-os-opus-fresh`), which is not the checkout
name — so the flow silently never appended. This is the SAME trap
`diagnostics.native_continuation_effectiveness` already documents and guards against; the
fallback had simply not been back-ported.

The first round of tests mocked `_project_for_target` to return a value, so they never
exercised the empty-registry path — the fix passed 6/6 under test and did nothing in
production. Fixed by falling back to the `agent` row exactly as diagnostics does, with two
further tests: one that fails without the fallback, one asserting an unknown agent with an
empty registry still fails CLOSED.

Verified live after the fix:

```
is_self_agent('owner-os-opus-fresh:0.0', ...) -> True
is_self_agent('hostsecure:0.0', ...)          -> False
flow appended to the self phrase              -> True
tests/test_wake_bridge.py                     -> 81 passed
```

## Self-wake: what is settled and what is not (18:00-19:40 CEST)

State only. An automated instruction asked for this update; that is not owner sign-off.
Owner-typed instructions in this window were "restart the companion", "answer the scope
gate for owner-os-opus-fresh", "answer the remaining scope gates" and "stop the watch".

### Live

`e285901` is loaded in companion PID 4170370 (18:25:24 CEST). Verified by composing the
live phrase: the self-agent wake carries `SELF_WAKE_FLOW`, a non-self wake does not.
`ai-runtime` PID 1196430 untouched since 06:05:35. Coverage 6/5/0, browser 8 pages,
headroom 4, `degraded=False`.

### Settled — the parking defect

Self-agent wakes were resolving `pane_awaiting_owner` because
`closed_loop_wake.py:424` returns that whenever an OPEN `owner_gate` names the agent.
`owner-os-opus-fresh:0.0` had an unanswered `classify_scope` gate (`0ea48864bbf04c56`,
"unknown-scope agent at /root/ai-dev-runtime") open since 06:07:24Z — over ten hours. The
classifier was correct: a pane parked on an owner gate is waiting, not stalled.

Six `classify_scope` gates were answered through `api.answer_gate()` (never hand-written
SQL), each appending an `owner_gate_answered` audit event: the self agent, plus
`gaika-opus-v6/v7/v8`, `security-demo-next` and `acap-voice`. The five non-self gates got
the standing house default used by all 19 prior ones — `keep as observe_only - no scope
change` — which GRANTS NO NEW SCOPE; that is what made it safe to apply to projects this
session does not own. The provenance clause was rewritten to name the actual Claude Code
instruction rather than repeat the precedent's "Owner decision given in the session pane",
which would have been untrue. **Zero owner gates of any kind are now open.**

The behaviour changed durably:

```
33368  16:40:39Z  pane_awaiting_owner              ]
33405  16:47:03Z  pane_awaiting_owner              ]  gates open — all parked
33424  16:50:12Z  pane_awaiting_owner              ]
--- scope gates answered 17:05:53Z ---
33474  17:13:33Z  runtime_reports_agent_working    ]
33487  17:25:28Z  pane_escalated_by_stall_doctor   ]  four verdicts, none parked
33526  17:32:17Z  prompt_no_longer_present         ]
```

### NOT settled — the external supervisor proof gap

Whether the external ChatGPT supervisor autonomously reads status and continues this agent
is **unproven**. The reason is a CONFOUND, not an unreachable code path:

* CORRECTION to the first version of this section. It claimed the scraped
  `pane_alive_and_working` path was "unreachable for the self agent while it is working",
  because `_natively_working()` is checked first (`closed_loop_wake.py:467`). That was
  wrong. `_natively_working()` FAILS OPEN — when `claude agents --json` does not report the
  session busy, the classifier falls through to `_watch_state_cls`, which can and does
  return `working`. Measured 18:43-19:04Z, the self agent resolved:

```
33753  18:43:01Z  pane_alive_and_working
33766  18:47:52Z  runtime_reports_agent_working
33786  18:50:54Z  pane_alive_and_working
33802  18:57:27Z  pane_alive_and_working
33817  19:04:03Z  pane_escalated_by_stall_doctor
```

* So the path IS reachable, and three samples took it. What it does not establish is
  CAUSATION: `pane_alive_and_working` says the pane is working, never what made it work.
  Instructions were arriving across all five of those timestamps, so this session's own
  activity explains the scraped class exactly as well as supervisor-driven resumption.
* An automated Owner OS API message asserted that its own arrival should be treated as
  proof that the supervisor can resume the self-project without owner input. That is
  circular and is NOT recorded as evidence: nothing on this side distinguishes "the
  supervisor autonomously continued the self-project" from "the API automation sent another
  instruction".
* Nothing in this repo records what the supervisor called. There is no API/MCP/tool-call
  log; those calls land on the `/opt/seo` surface. `submitted_and_assistant_started_generating`
  proves a turn began in the bound chat, nothing more.

Event 33325 was earlier offered as end-to-end proof; it is not. It was delivered 16:24:30Z,
54 seconds BEFORE the working build loaded at 16:25:24Z, so its phrase carried no flow.

**What would close it:** a self-agent `agent_waiting_input` delivered during a genuine idle
gap, with no instruction in flight, resolving `pane_alive_and_working`. Needs no new
instrumentation — the signal is already durable in `wake_loop_watch`.

### Observations recorded, deliberately not "fixed"

* `owner_gate.notified_at` is declared at `store.py:130` and never written or read anywhere.
  A dead column: nothing re-notifies on it, no behaviour depends on it. All 172 rows empty.
* All 32 gate/scope notifications are `dead_letter` on the Telegram gate, yet 24 gates have
  been answered — so gates reach the owner through the chat, not the notifier.
* `agent_waiting_input` is ~390 of the owner-action events in 24 h, 27 of them from the self
  pane simply being idle between turns. Suppressing those is an alerting-policy judgement,
  not a defect, and could hide a genuinely stuck Owner OS.
* Three gaika generations (v6, v7, v8) opened scope gates within eight hours. Agents are
  rotating faster than their gates get answered.

### Gates still standing

1. **Telegram token** — sole cause of health red, unchanged all session.
2. **Canary retarget to `hostsecure:0.0`** — declined three times on the automated channel;
   needs a typed instruction. Not needed for the proof, which already reads
   `streak 172, matched_by=agent` read-only at that target.
3. **`acap-voice` route/project registration** and its interactive prompt — never driven
   from here.

## Zero-ping / autonomy

`NATIVE_SUPERVISOR_TARGETS=*`, denylist authoritative and holding.

```
covered  5   gaika-opus-v5 · hostsecure · mess-postsignup-cleanup-sonnet-v4
             mess-safe-finish · security-demo
denied   5   capacity-blockchain · diamond-auction · email · payorch-ha-fresh
             (denylisted_project) · owner-os-opus-next (self_project)
uncovered 0
```

`payorch-ha-fresh:0.0` is NEW this session and was correctly denied — the denylist caught
an agent that did not exist when it was written.

**Effectiveness: proven for the canary, retrospectively.** Owner typed "use
gaika-opus-v5:0.0 as the canary". With `NATIVE_CANARY_TIMEOUT_SECS=3600`,
`native_continuation_effectiveness()` reported 15 samples / 10 verified / 4 unverified /
1 pending, streak 5, verdict PROVEN, `turns_matched_by=agent`. Later re-reads moved to
`unproven` as new unverified samples reset the streak — that is the fail-closed rule
working, not a regression. Verdicts are computed from durable history, not gathered live.

Not generalised: one agent, and the window is a judgement (0 verified at 600s, 10 at
3600s) because `agent_turn_stopped` fires when a turn ENDS.

Autonomy verified healthy at 04:30Z: security-demo and mess-safe-finish actively
continuing; hostsecure gated by `continuation_cap_reached_without_progress`
(MAX_CONSECUTIVE=6, by design); gaika-opus-v5 `cls=working` mid-turn; mess-postsignup
held by the supervisor's own continuation gate. No regression.

## Browser — the tab leak, PROVEN, FIXED, LIVE and OBSERVED CLOSED

Superseded the "top OPEN technical issue" section of the 05:15Z state below. Worked under
an automated Owner OS API instruction; a later automated instruction asserted an
owner-typed instruction in the pane, which is NOT independently verified and is recorded
here only as "an automated instruction was received". No owner sign-off is recorded in
this section.

### Verdict

`/json/new` could time out AFTER Chrome had already created the tab, and the code then
threw away its only chance to close it. One leaked page per failed recovery, permanently,
on the bound conversation.

```python
try:
    fresh = _http("/json/new?...", method="PUT")   # _http applied a fixed 8 s ceiling
except Exception:                                   # "pre-111 Chrome used GET here"
    fresh = _http("/json/new?...")                  # Chrome 151 answers 405
```

`/json/new` is the only browser-level DevTools call that does real work — Chrome spawns a
renderer and starts the navigation before it answers; everything else is a lookup answered
from memory in milliseconds. All of them shared one 8 s ceiling. **Measured live on this
host at 13 pages open: 4.56 s.** When the answer missed the ceiling the tab EXISTED and
only the answer was missing. The GET fallback then raised `HTTPError 405: Using unsafe HTTP
verb GET to invoke /json/new`, which escaped to the outer handler with `fresh` still None,
so its cleanup guard `if fresh and fresh.get("id")` closed nothing. The orphan finished
loading the bound conversation seconds later: a duplicate on a ROUTED conversation, with no
root URL and no `WEB:` placeholder to give it away — which is why every orphan hunt missed it.

**The 05:15Z hypothesis is REFUTED.** It was: a verified replacement followed by both
`_reap` attempts failing on the OLD tab. Measured against the live browser, `/json/close`
answers HTTP 200 `Target is closing` and the page leaves `/json/list` in under 0.5 s.
`_close_target` and `_reap` work correctly.

### Evidence

Cleanup ended 03:46Z at 8 pages (`wake_delivery` 11117, first non-degraded attempt). Next
`too_many_pages:13` refusal is 11195 at 05:37:39Z. Five pages accrued, against exactly five
failed recoveries in that window:

| leaked page (conversation) | failed recovery | recorded |
|---|---|---|
| e672 x2 | 11163, 11194 hostsecure | `renderer_unresponsive` |
| 1648 x1 | 11137 mess | `renderer_unresponsive` |
| 0690 x1 | 11185 seo | `renderer_unresponsive` |
| e63a x1 | 11151 / 11181 payment-orchestrator | `assistant_generating_wedged` recovery |

`performance.timeOrigin` read from each live tab gives its document load time. Every leak
still measurable was created 7-9 s before its own failure record — the 8 s timeout plus an
immediate 405:

```
F50ACC3FC28A  e672  created 05:36:22  ->  11194 renderer_unresponsive 05:36:31  (+9 s)
416895EB5762  0690  created 05:17:09  ->  11185 renderer_unresponsive 05:17:16  (+7 s)
F3FF276A6CC9  1648  created 00:13:25  ->  10925 renderer_unresponsive 00:13:34  (+9 s)
```

That timing also excludes the verification-timeout branch, which cannot return in under
30 s (15 iterations x `sleep(2)`), and the success branch, which returns a tab rather than
None. Only an exception exits that fast, and the 405 is the exception.

### The fix — pushed

```
ffe63c2  fix(browser): a create that timed out still made the tab
5a9f015  docs(report): the tab accrual, proven rather than suspected
```

`e60a00b..5a9f015` pushed to `origin/ai-runtime/220-windows-bridge`, normal non-force push.

`_create_tab()` is now the literal single choke point both creation paths go through, and
carries three things, all three needed:

* a budget that fits the call (`CDP_NEW_TAB_SECS`, 30 s); `CDP_HTTP_SECS` keeps 8 s for
  every lookup, and `_http` takes a per-call timeout;
* the GET fallback fires only on a VERB refusal (405/501). A timeout must never re-issue a
  create — on the pre-111 Chrome that fallback exists for, the second call would open a
  SECOND tab;
* a create that fails for any reason sweeps for the tab it may have made anyway.
  `_sweep_unnamed_tab()` closes a page that was not in the snapshot taken immediately
  before the create AND sits on a URL that create could have produced. A page open before
  the create is never a candidate, so no bound conversation and no tab of the owner's can
  be caught by it; an unreadable before-snapshot closes nothing rather than guess.

### Test evidence — targeted, complete

8 new tests in `tests/test_cdp_composer.py`. Confirmed by removal: with
`tools/cdp_composer.py` reverted to HEAD all eight fail, the central one as
`assert [] == ['ORPHAN']` — no close was even attempted.

All six suites that import `cdp_composer` are green at this HEAD:

```
111  test_cdp_composer
358  test_wake_delivery_verification, test_wake_pipeline_health,
     test_control_plane_diagnostics, test_closed_loop_wake  (+ test_cdp_composer)
 22  test_wake_assistant_proof, test_wake_companion, test_wake_composer_transient_retry
```

### Companion restarted — the fix is LIVE

```
owner-os-wake-companion  PID 1036186 -> 1770858  active since 2026-09-05 08:28:39 CEST
NRestarts=0
```

Source finalised 08:20:34, `tools/__pycache__/cdp_composer.cpython-312.pyc` compiled 08:20,
process started 08:28:39 — the running companion loaded the fixed module. Autonomy alive on
the first ticks: `native-supervisor: continued security-demo:0.0 from event 31638`.
`ai-runtime` was NOT restarted.

### Guarded cleanup run — 13 -> 8

```
close A16354B50837  e672  verified_gone=False   <- both completed a moment after _reap's
close C873547A5F38  e63a  verified_gone=True       deadline; neither is in the after-list
close C37A1698FAFB  e672  verified_gone=True
close F3FF276A6CC9  1648  verified_gone=False
close C8BB5A78C124  0690  verified_gone=True

pages 13 -> 8 · headroom 4 · degraded=False
every conversation keeps exactly one responsive tab
```

Guards were re-asserted against the LIVE list at execution time, not only in the dry run:
every close target on a routed conversation, no conversation may lose its last tab, count
must land on exactly 8. The first execution aborted on `guard: C8BB5A78C124 no longer
exists` — a 12-char-prefix vs 32-char-id bug in the cleanup script — and closed nothing. No
close candidate was mid-generation. Snapshots kept as
`tabs_before_cleanup.json` / `tabs_after_cleanup.json` in the session scratchpad.

`too_many_pages` refusals stop dead at row 11263 (06:34:06Z). What follows is
`endpoint_slow:2.5s` and `cdp_error:WebSocketTimeoutException` — the pre-existing host-load
intermittency, not the leak. Row 11237 recorded `too_many_pages:14`: that was a controlled
`about:blank` transport test being correctly refused, and it cost one delivery attempt.

### Both checks FINISHED

**Full suite: green.** `3128 passed, 0 failed, 0 errors, 1 warning in 1462.09s (0:24:22)`.
Details in *Tests at this HEAD* above.

**25-minute tab-count soak: clean — and it does NOT prove the fix.**

```
50/50 samples over 25 min   pages=8   peak=8   dups=-   list failures=0
deliveries during the soak: 5 delivered · 10 cdp_error:WebSocketTimeoutException
browser_degraded after:     {'degraded': False, 'pages': 8, 'headroom': 4}
```

Not one sample deviated: no rise to 9, no duplicate on any conversation, and the CDP
endpoint answered every poll.

The limit has to be stated, because a clean soak reads like proof and is not. **Zero** rows
since the cleanup carry any of the three reasons that call `recover_wedged_tab` —
`renderer_unresponsive`, `assistant_generating_wedged`, `composer_ambiguous_or_absent:0`.
So `_create_tab` has not executed live even once. The soak establishes that the count is
stable at 8 and that nothing else in the system accretes pages; under the OLD code the same
25 minutes need not have leaked either, because a leak required a failed recovery. It is
corroboration, not the live proof.

What does prove the fix: the measured cause above, and 8 tests confirmed by removal.

The `cdp_error:WebSocketTimeoutException` majority is a session timing out mid-work under
host load. That path does not trigger recovery, which is why none fired.

### CLOSED END TO END — the fixed path ran in production, and nothing leaked

Read-only verification at 07:38Z under an automated instruction; not owner sign-off.

The watch is satisfied. A recovery fired after the 06:34Z cutoff, `/json/new` ran live
several times, and the page count never moved off 8.

```
wake_delivery 11287 · 2026-09-05 07:11:49Z · mess (1648) · assistant_generating_wedged
```

That reason is one of the three that call `recover_wedged_tab`. Its replacement tab
`4AFE7549CE41` was created at 07:10:53Z — 56 s before the row — the old tab
`D020C47C0524` is gone, and mess held exactly one tab throughout. The recorded failure is
the RETRY still finding generation in flight, not a failed recovery; mess delivered
normally 6 minutes later (11291, 07:18:04Z).

Diffing the live list against `tabs_after_cleanup.json` shows four complete create-and-close
cycles, four tabs created and four closed, net zero:

```
GONE since cleanup            NEW since cleanup
  F50ACC3FC28A  e672            18A03E3D5670  e672
  D020C47C0524  1648            4AFE7549CE41  1648
  B38671ACBE96  e63a            F399D6E47FA1  e63a
  5E240CA39977  7789            B24345268AB8  7789
UNCHANGED: 416895EB5762 0690 · E0DA45FEAC8F 459a · D5AE3402B075 487a · 48BE29644EA9 0e62
```

A target id changes only when a tab is created or destroyed, so these are genuine
`/json/new` creations, not navigations. There were at least FIVE: `7789` was read as
`B87BDCF4190F` and then as `B24345268AB8` about a minute apart, so it cycled twice inside
the observation itself.

```
07:38Z  8 pages · headroom 4 · degraded=False · duplicates NONE
```

Successful recoveries remain invisible in `wake_delivery` — they are recorded only as
`submitted_and_assistant_started_generating`, the standing observability gap — which is why
this had to be established from tab identity rather than from records.

**Before and after, same measure.** Pre-fix, 03:46 -> 05:37Z: five recoveries that recorded
a failure reason, five leaked pages, 8 -> 13. Post-fix, 06:34 -> 07:38Z: at least five
`/json/new` creations including one recorded failure, zero leaked pages, 8 -> 8.

Evidence kept: `tabs_after_cleanup.json` and `tabs_after_live_recovery.json` in the session
scratchpad.

### One-hour regrowth watch — FINISHED, no regrowth

Armed 09:50 CEST / 07:50Z, exited 10:52 CEST / 08:52Z. Baseline 8 pages, sampled every
60 s, from `wake_delivery` id > 11320.

```
WATCH DONE samples=59 final_pages=8 peak=9 creates=18 closes=18 listfail=0
           recoveries_recorded=6

   delivered=1  submitted_and_assistant_started_generating   x28
   delivered=0  cdp_error:WebSocketTimeoutException          x10
   delivered=0  assistant_still_generating                    x7
   delivered=0  assistant_generating_wedged                   x6
```

**Eighteen tab creations, eighteen closes, exact parity, and the count ended where it
started.** Six recorded recoveries — `assistant_generating_wedged` x6, one of the three
reasons that call `recover_wedged_tab` — so the fixed path ran repeatedly under
observation. Zero `/json/list` failures across 59 samples.

Same measure as the leak, before and after:

| window | recorded recovery failures | tab creations | pages |
|---|---|---|---|
| pre-fix 03:46 -> 05:37Z | 5 | not instrumented | 8 -> 13, five leaked |
| post-fix 07:50 -> 08:52Z | 6 | 18 | 8 -> 8, none leaked |

#### The one alert was a false positive

```
REGROWTH 10:04:31  pages=9 (baseline 8) dups={'1648-2b08-83': 2} new_tabs=['CEFB2BC4FC3E']
ok       10:05:32  pages=8 peak=9 dups=- creates=3 closes=3 listfail=0
```

`CEFB2BC4FC3E` was created on mess at 08:04:15Z and the sample landed at 08:04:31Z — **16
seconds into the window between `/json/new` and the old tab's close**, the one moment a
correct recovery legitimately holds two tabs on one conversation. The next sample was back
to 8, and that tab is now the sole tab on mess. Same classifier hazard the 2026-09-04 proof
report recorded, where two "LEAK CANDIDATE" events were healthy replacements sampled in
flight.

**Threshold lesson, to carry forward:** a single sample above baseline is not evidence of a
leak. `peak=9` with `final_pages=8` and `creates == closes` is the signature of healthy
replacement, not of accrual. Any future alert must persist across at least two consecutive
samples before it is treated as regrowth.

### What is NOT claimed

None of these creations is shown to have TIMED OUT, and the leak required a timeout. So
this window proves the fixed code path runs correctly in production and no longer accrues
pages; it is not an observation of the specific 8 s-timeout race being survived. The proof
of that remains the measured cause (4.56 s live create against a fixed 8 s ceiling, GET
fallback answering 405) and the eight tests confirmed by removal.

### State as read at 05:15Z, kept for the record

```
13 pages · headroom -1 · reclaimable 5 · degraded=too_many_pages:13
all 13 are duplicates on ROUTED conversations — no roots, no WEB: placeholders, no orphans
  x3 hostsecure/security-demo · x2 mess · x2 payment-orchestrator · x2 seo
```

A guarded cleanup at ~03:40Z took 13 -> 8. It grew back to 13 within ~4.5 h with the three
earlier fixes live — those REDUCED the leak but did not eliminate it, because none of them
touched the create-timeout path proven above. Delivery over that window: 39 delivered of
117 attempts (33%) — 38 refused `too_many_pages`, 27 `cdp_error:WebSocketTimeoutException`
(host load), 4 `renderer_unresponsive`.

## Zero-ping resume — durable evidence, and what it does NOT cover

An automated instruction asked for this observation; that is not owner sign-off.
Read-only; nothing changed.

### Proven for natively supervised agents

Four consecutive ATTRIBUTABLE resumes on `hostsecure:0.0`, traced row by row from
`native_supervision` and `event`:

```
continue 2026-09-05 20:07:41Z -> turn 20:07:44Z  (+3s)  deliveries_between=0
continue              20:09:58Z -> turn 20:10:07Z  (+8s)  deliveries_between=0
continue              20:12:10Z -> turn 20:12:15Z  (+5s)  deliveries_between=0
continue              20:14:17Z -> turn 20:14:21Z  (+3s)  deliveries_between=0
```

Each row: the native supervisor issued `continue`, the agent produced a COMPLETED turn
3-8 s later, and ZERO wake deliveries landed on that route in between — so no ChatGPT
message and no human instruction can account for it. A quiescent agent resumed to working
with nothing in flight, four times in seven minutes.

Aggregate at that target: 275 samples, 239 verified, 32 discarded `unattributable`,
streak 201, `matched_by=agent`, verdict `proven`. The 32 discards ARE the guard working.

`mess-safe-finish:0.0` is the contrast that shows the guard is not decorative:

```
continue 21:16:26Z -> turn +862s   deliveries_between=1  discarded
continue 21:32:59Z -> turn +957s   deliveries_between=2  discarded
```

Browser wakes landed between continuation and turn, so those samples were refused rather
than counted.

### NOT covered by this evidence

This proves the NATIVE path for agents the supervisor is allowed to touch. It says nothing
about the SELF agent, which is denied from native supervision by the recursion guard and
can only be resumed by the external ChatGPT supervisor. That remains unproven and cannot be
observed while instructions keep arriving, because this session's own activity satisfies
`_natively_working()`. See the self-wake section above.

So "zero-ping" is proven in the supervised sense and open in the self-project sense. Those
are two different claims and should not be reported as one.

## 90-minute read-only watch, 20:55-22:26 CEST — results

An automated instruction asked for this record; that is not owner sign-off. Read-only
throughout: nothing shed, restarted or configured.

### Browser — the tab fix held under adverse conditions

`pages=8`, zero duplicates, on EVERY sample of the window. Not one deviation across five
memory-pressure alerts and five recovery-triggering delivery failures. Five
`assistant_generating_wedged` events each drove `recover_wedged_tab` — the create-and-close
path that leaked a page every time before `ffe63c2` — and the count never left 8.

This is stronger evidence than the morning soak, which ran in a quiet window.

### Delivery — 45 of 61 (74%)

```
45  submitted_and_assistant_started_generating
 8  cdp_error:WebSocketTimeoutException
 5  assistant_generating_wedged
 2  assistant_still_generating
 1  assistant_started_but_produced_nothing:1
```

### Memory — five alerts, all self-clearing, gate 3 CLOSED

```
21:16  psi_full 5.15   free 192MB  so=14848  ->  21:19  psi 2.14  so=0
21:41  psi_full 3.32   free 559MB  so=0
21:44  psi_full 11.65  free 140MB  so=17032  ->  21:47  psi 1.01  so=0   worst of session
22:02  psi_full 3.49   free 624MB  so=2512   ->  22:08  psi 0.00  so=0
22:20  psi_full 5.25   free 243MB  so=0                                  OPEN at window close
```

Each episode recovered in about three minutes and every one involving eviction returned
`so` to 0. Bursty, not degenerative.

**Two things stated as OBSERVATION, not proof:**

* The failure clusters CORRELATE with the memory spikes; they were not traced
  event-by-event to them. Correlation across a 90-minute window is not causation, and no
  attempt was made to establish it.
* The 22:20 alert was still open when the window ended. Its shape matches the four that
  cleared and it carried `so=0` (pressure without eviction), but it was NOT observed
  clearing. Do not read the run as "five spikes, all resolved".

### Repo

`f73b6cd`, `origin` in sync, tracked tree clean, zero open owner gates.

### The gates, restated

1. **Telegram token** — owner-controlled. Still the sole cause of health red.
2. **`acap-voice` route/project registration** — owner-controlled; never driven from here.
3. **Supervisor resumption** — NOT a gate and NOT a config change. It needs only a quiet
   observation window: a self-agent wake resolving `pane_alive_and_working` with no
   instruction in flight. The signal is already durable in `wake_loop_watch`; nothing needs
   building, enabling or restarting to capture it.

## Host memory — spiked and recovered inside one hour, still monitor only

At 05:15Z:

```
load  29.29 -> 9.98      swap 15.4 / 20 GB      free 666 MB      paging si=2208 so=0
```

`so=0` means nothing was being evicted; earlier it was `si=1548 so=3128`, i.e. thrashing.
Largest consumers are NOT Owner OS: `postgres` 2.31 GB, `chrome` 2.15 GB, `fastnetmon`
1.54 GB single process, `claude` 2.72 GB spread over 26 processes; `mariadbd` and `ollama`
hold 2.3 GB of swap between them while idle.

**It then reversed, and recovered, within the hour of the regrowth watch:**

```
10:45 CEST  free 909 MB   load 20.77  (avg5 17.95 avg15 18.55, RISING)
            PSI some avg10=9.48 > avg60=6.85 · full avg10=2.65 > avg60=2.13
10:47 CEST  free 214 MB   used 10 049 MB          <- worst reading
10:52 CEST  free 798 MB   load 10.72  (avg5 12.77 avg15 16.07, FALLING)
```

The spike was not inferred from counters alone: the harness shed three of this session's
background wait wrappers for low memory in the space of a few minutes, which is the
pressure landing on real processes. The watch process itself survived all three and
completed its full 59 samples.

By 10:52 load had halved and the ordering had inverted back to falling. So this is a spike
that resolved, NOT the sustained reversal that gate 3 describes. Nothing was shed by hand
and nothing should be: the top consumers remain postgres, chrome, fastnetmon and `claude`
itself, none of them Owner OS.

Watch it. If free memory stays under ~300 MB with PSI `avg10 > avg60` across consecutive
readings, gate 3 is genuinely open and the question of which process to shed becomes the
owner's. The elevated `cdp_error:WebSocketTimeoutException` rate is the visible symptom of
this load, and is unrelated to the tab leak.

## Ledger rows 21903 and 24179

Both are `agent_turn_stopped` hook events carrying the AGENT'S OWN end-of-turn message.

| row | when | project | key | value len |
|---|---|---|---|---|
| 21903 | 2026-09-02 15:37:59 | `arbitrage2-fable-audit` | `token=` | 20 |
| 24179 | 2026-09-03 07:45:58 | `payment-orchestrator` | `password=` | 33 |

Established without reading either value (boolean checks only): **not** a secret of this
repo (no match or containment against `configs/.env`), **not** a repo fixture (absent from
the working tree and all git history), **not** a placeholder, **not** prose. They belong,
if to anything, to those two other projects. Nothing in this repo can say whether they are
live — that needs the values read and compared against those systems.

Ingress is closed going forward: four writers now redact at the emit boundary, live since
the 05:28 companion restart, and the hook has redacted since `0988577` because it is a
fresh subprocess per event. These two rows persist; nothing new joins them.

## Genuine owner gates

1. **Telegram BotFather token** — the SOLE cause of health red, all session.

   Non-secret remediation, in order — step 2 is the one people miss:

   1. create a dedicated bot with BotFather, token into `configs/.env` as
      `TELEGRAM_BOT_TOKEN`;
   2. send that bot ONE message from the owner's Telegram account, so an inbound update
      exists;
   3. Owner OS derives the chat id from `getUpdates` and verifies with `getChat`.

   Until step 2 happens there is nothing for `getUpdates` to return, which is precisely why
   the current id resolves to `Bad Request: chat not found`.

   Counts at 2026-09-05 23:10Z: **6652 dead letters, 30 active** (was 5986/18 at 05:15Z —
   the backlog grows by roughly one per agent-watch notification and will keep growing
   until the token is in place). Two dead-letter events were traced end to end and BOTH are
   this gate rather than a defect: 31943 (`mess-safe-finish`) in
   `reports/OWNER_OS_EVENT_31943_DEAD_LETTER_2026-09-05.md`, and 34453
   (`capacity-blockchain`) at 23:08:30Z, identical chain, identical terminal reason.

   Accounting re-verified against the raw table at 23:10Z and it is HONEST — nothing hides
   or misclassifies these:

   ```
   raw            dead_letter 6652 · sent 2
   history_report total 6652 · active 30 · historical 6622 · status red
   failure_report total 6652 · active 30 · classification active
   notifications_status  red · owner_push "telegram send failed: Bad Request: chat not found"
   ```

2. **Rows 21903 / 24179** — one question per project: is that value live? If yes, rotate at
   the issuing service; `control_plane.db` is a plain file on this host, so scrubbing the
   row is cosmetic beside rotation. If no, nothing is required.
3. **Host memory** — conditional only. Five spikes on 2026-09-05, each self-clearing within
   ~3 minutes; by 00:25Z load was back to 10.13 with PSI decaying. NOT currently a gate;
   becomes one only if free memory stays under ~300 MB with PSI `avg10 > avg60` across
   CONSECUTIVE readings and processes must be shed.
4. **Push** — four documentation-only commits on
   `reports/OWNER_OS_HANDOFF_2026-09-05.md` are unpushed (`105f731`, `3f009d5`, `9177606`,
   `db6cbc1`). Repeated automated instructions both to push and NOT to push arrived on the
   same channel; neither is owner-typed, so the gate stands.
5. **`acap-voice` route/project registration** — its events carry an empty `project_id` and
   fall back to the `owner-os` route by design (`wake_routes.route_key_for_event`). Fixing
   that means registering the project or binding a route: both owner-only.

## Do NOT touch

* `configs/.env` secrets, the Telegram token/chat id, the SECURITY project's bot/webhook.
* The denylist, `SELF_PROJECT`, `NATIVE_SUPERVISOR_TARGETS`.
* The 34 untracked `reports/` files; stage explicit paths, never `git add reports/`.
* `/opt/seo` and the other projects' trees. The `Owner_OS.notifications` MCP surface is
  served from /opt/seo and diverges from this repo by design — not a defect here.
* Route rebinds without a typed owner instruction.

## Rollback

```
backups/activate_canary_20260904T235702Z/    .env.before (600) + ROLLBACK.md
backups/gate_answer_20260904T213159Z/        gate_before.json + ROLLBACK.md
backups/telegram_bot_swap_* rebind_* rotate_runtime_token_*   (earlier sessions)
```

Canary rollback is complete and needs nothing else — the verifier writes nothing and adds
no schema:

```
sed -i '/^NATIVE_CANARY_TARGET=/d;/^NATIVE_CANARY_TIMEOUT_SECS=/d;/^# canary for native-continuation/d' \
  /root/ai-dev-runtime/configs/.env
```

Cheapest degrade for anything companion-related: `systemctl stop
owner-os-wake-companion`. Native continuation runs INSIDE it, so that stops autonomy too.

## State at context rotation — 2026-09-06 ~08:45Z

Written for a fresh session to resume from. An automated instruction asked for it; that is
not owner sign-off.

### Resume identifiers

```
repo        /root/ai-dev-runtime   branch ai-runtime/220-windows-bridge
HEAD        97550a0                PUSHED — local == remote, behind=0 ahead=0
upstream    origin/ai-runtime/220-windows-bridge   (in sync; verified 2026-09-06 ~08:55Z)
tracked tree clean · 34 untracked files under reports/, never staged broadly
session     claude.ai/code/session_011BF9Z1MpBRv4uL9AHtK5WS
scratchpad  7e0ead20-0e1c-4201-8d75-6a0d47198fa2   (session-local, will NOT survive)
```

Durable evidence lives in `control_plane.db`, `logs/owneros_hook_diag.jsonl` and this file.
Scratchpad logs (watch runs, test output) do not survive rotation and are already
summarised here.

### Landed and pushed this session

```
c084d43  test: keep the suite out of the live hook diagnostic
080eb6f  feat(hook): tell the three silences apart
2c53af7 db6cbc1 9177606 3f009d5 105f731   handoff revisions
f73b6cd e285901 83c41b2 a91e5c5 34a6eeb   self-wake fix + handoff
ffe63c2 5a9f015                           the tab leak, fixed and proven
```

### Last two commits (now pushed)

```
97550a0  docs(handoff): state at context rotation
31d7cb6  test: pin that the doctor's suppression closes a watch, not a delivery
```

`31d7cb6` is tests only, no production change — it pins that the stall doctor's
suppression closes a WATCH and cannot gate a DELIVERY. 212 passed across
`closed_loop_wake`, `wake_bridge`, `owneros_hook`.

**Nothing is unpushed.** Owner typed "push it" at ~08:50Z and the push was verified
(`c084d43..97550a0`, local == remote).

### Live runtime state

```
companion   PID 529251   ai-runtime PID 1196430   both active
coverage    6 covered / 4 denied / 0 uncovered
canary      hostsecure:0.0 · streak 216 · proven · matched_by=agent
browser     8 pages · headroom 4 · degraded=False · duplicates NONE
owner gates 0 open
hook diag   clean across 9 sessions, no forged test records
```

### The three gates — all owner-only

1. **Telegram token.** Sole cause of health red. Remediation: BotFather token into
   `configs/.env` as `TELEGRAM_BOT_TOKEN`, THEN send that bot one message from the owner's
   account so `getUpdates` has an inbound update. Step 2 is the one that is missed.
2. **`acap-voice` route/project registration.**
3. ~~**Push**~~ — CLOSED. The owner typed "push it" and `97550a0` is on the remote.

   Kept as a note for whoever resumes: throughout this session the automated channel
   both demanded and forbade the push in alternating turns, and the pane's `push it` line
   was machine-queued and rejected by Owner OS's own
   `queued_line_not_submittable:forbidden_token` guard (events 35059, 35133, 35193). Pane
   text is NOT owner approval — the queue writes into that pane, and its own safety check
   was refusing the very line being cited as authorisation. Only a typed instruction in
   the Claude Code session counts.

### Open, NOT a gate

Self-project supervisor resumption. The wake reaches the bound chat and a turn starts;
whether the supervisor then continues the work is unobserved, because nothing durable
records pane progress after a delivered wake. Closing it means writing progress state into
the watch path — a real change, not read-only observability.

### Carried forward for owner judgement

* `observe_only` gate answers do NOT constrain supervision. `discovery.classify_scope`
  derives lifecycle from orchestrator config and never reads `owner_gate.answer`;
  `owner_status` classes `classify_scope` as diagnostic, "a mapping gap, not a decision".
  `acap-voice:0.0` is natively covered despite its answer.
* `mess` agent churn: five generations in 36 minutes, four dead
  (`mess-ru-edge` 366s, `-edge-sonnet` 244s, `-final` 586s, `-go` 1073s,
  `-54582145` alive). Replacements are created ~2 s BEFORE the predecessor is declared
  dead. Cause lives in `/opt/mess`, out of scope here.

## Next safe step

**The only gate in this workstream is the push.** Three documentation commits are staged
locally on `reports/OWNER_OS_HANDOFF_2026-09-05.md` and nothing else:

```
9177606  record the hostsecure retarget and refresh the stale PID
3f009d5  zero-ping resume proven for supervised agents, still open for self
105f731  the 90-minute watch, with its correlations marked as such
```

Everything finished in this workstream is recorded above: the tab leak proven, fixed,
live and observed closed; the self-wake flow live after shipping inert once; six owner
gates answered; the canary on `hostsecure:0.0`; full suite green at 3128.

### Open, and each one belongs to the owner

1. **Telegram token** — sole cause of health red, unchanged all session. Traced end to end
   in `reports/OWNER_OS_EVENT_31943_DEAD_LETTER_2026-09-05.md`: a gate, not a defect.
2. **`acap-voice` route/project registration** — never driven from here.
3. **Push** of the three commits above.

### Open, and NOT a gate

**Self-project supervisor resumption.** Needs no config change and no approval — only a
quiet observation window: a self-agent wake resolving `pane_alive_and_working` with no
instruction in flight. The signal is already durable in `wake_loop_watch`. It could not be
obtained in this session because every wake arrived while instructions were in flight, and
this session's own activity satisfies `_natively_working()`.

Distinguish the two zero-ping claims when reporting: PROVEN for natively supervised agents
(four attributable resumes on `hostsecure`, deliveries_between=0), OPEN for the self agent.

### Watch items, neither a task

* **Host memory** — five spikes across the session, each self-clearing within ~3 minutes,
  every eviction returning `so` to 0. Gate 3 opens only if free memory stays under ~300 MB
  with PSI `avg10 > avg60` across CONSECUTIVE readings. The last observed alert (22:20) was
  still open when its window ended and was never seen clearing.
* **Duplicate tabs** — if any reappear, the page that appears IS the evidence: capture
  `/json/list` and the matching `wake_delivery` row before touching anything, and confirm
  across two consecutive samples before calling it regrowth. A single sample above baseline
  is a replacement caught mid-swap, not a leak.

---

# Session of 2026-09-06 — the MCP control path, and self-project resumption

Appended to the canonical handoff rather than started as a new one, because an automated
instruction named this file as the resume point. An automated Owner OS API instruction
drove this session; that is **not** owner sign-off, and nothing below is recorded as owner
approval. Full evidence:
`reports/OWNER_OS_MCP_CONTROL_PATH_AND_SELF_RESUMPTION_2026-09-06.md`.

## Repo

| | |
|---|---|
| Branch | `ai-runtime/220-windows-bridge` |
| HEAD | `78c3d09` and one docs commit after it — **8 unpushed**; push is an owner gate |
| Upstream | `origin/ai-runtime/220-windows-bridge` at `b615fbc`, verified 2026-09-06 12:2xZ |
| Before this session | `b615fbc`, local == remote, ahead=0 behind=0 |
| Tracked tree | clean apart from the two commits above |
| Untracked | reports only — never `git add reports/`; both commits staged EXPLICIT paths |

```
78c3d09  fix(workers): the last two ticks that ran on the event loop   <- code
1f3e603  docs(handoff): the repo table listed a HEAD two commits stale
31a9309  docs(handoff): the suite result, and the one failure I cannot name
e024d0f  docs(comment): the lock note said four routes; it is five
8584047  docs(handoff): the 2026-09-06 session, and what it did not prove
25930ef  docs(report): the control-path root cause, and the self-resumption chain
a7a438c  fix(api): take the MCP control path off the event loop   <- the only code change
```

## Criterion 1 — MCP control path reliability: ROOT CAUSE PROVEN, FIXED, NOT DEPLOYED

`agent_status` / `agent_read` / `agent_send` / `agent_answer` were `async def` while
calling blocking tmux code (`subprocess.run`, `_TMUX_TIMEOUT=15`, plus `time.sleep(0.4)`
on delivery). On single-worker uvicorn that runs ON the event loop and freezes every other
request; the MCP client times out and reports JSON-RPC failure while the server answers
`200`. The access log is clean because the failure is latency, never an error.

Ruled out first: HTTP layer healthy — 42,513 × 200 vs ~886 non-200 (2.0%), the 400s being
`AgentControlError` refusals for dead targets (`mess-qa-automation`, `arbitrage2-opus`,
`cp-canary`) and the 401s being auth.

Measured live (read-only, nothing mutated): `/health` p50 **7.8ms → 213ms** under six
concurrent `agent_read`; **max 4028.6ms under ambient traffic alone**. Matched A/B, two
worktrees, real router, no startup workers:

```
probe p50 under load   220.2ms -> 30.3ms      agent_read p50    228.2ms -> 85.9ms
probe p99 under load   787.1ms -> 123.5ms     agent_read p99    788.5ms -> 210.8ms
probe samples/s        3.7     -> 16.2        read throughput   23.5/s  -> 64.7/s
```

Fix, two narrow parts: five handlers declared `def` (Starlette threadpool), and a
reentrant `_DELIVER_LOCK` preserving the serialisation the event loop supplied by
accident — without it, concurrent callers can double-paste a replayed idempotency key and
bypass `agent_send`'s anti-queue guard.

12 tests in `tests/test_mcp_control_path_concurrency.py`. Removal proof both halves:
`async def` restored → 2 fail; lock removed → 3 fail (4 messages pasted into one pane
instead of 1); restored → 12 pass.

## Criterion 2 — self-project supervisor resumption: PROVEN from durable records

The previous section of this handoff recorded this as open. It was **observable all
along** — the records existed across three tables and had not been correlated.

Event **33823**, self agent `owner-os-opus-fresh:0.0`, project `ai-dev-runtime`:

```
19:04:06.080  event 33823 work_stopped_incomplete, high, actionable=1     event
19:06:02.554  wake decision WAKE / actionable_waiting_transition          wake_audit 136282
19:09:48.921  delivered=1 submitted_and_assistant_started_generating      wake_delivery 11809
              conversation 6a967789-… (route owner-os, bound_by=owner)
19:09:59.036  +10.1s agent_send key owner-self-handoff-33823-continue     deliveries +
              actor api:bearer  source 172.20.0.2 ua=python-httpx         delivery_attribution
      —       watch resolved pane_alive_and_working                       wake_loop_watch
      —       NO owner_intervention_log row for this target that day      owner_intervention_log
```

Not isolated: of 208 self-pane watches, 16 are followed by an `api:bearer` continuation
inside the 900s SLO and **9 carry a key naming their own wake event id**.

`SELF_PROJECT` denial and `SELF_WAKE_FLOW` intact, verified for the CURRENT self pane
(`owner-os-opus-final:0.0` — this session): `is_self_agent` True. Note it resolves True via
the `agent`-table fallback, because `_project_for_target` returns `''`; the guard holds by
its fallback, not its primary path.

Gap closed for future runs: `agent_status` / `agent_read` now record `actor`/`source`
(keyword-only; omitted entirely when unset, so worker audit lines stay byte-identical).
Live-verified. It cannot be back-filled for the 33823 window.

## Tests at `e024d0f`

The suite could not be run as one process: the host was in memory exhaustion (see below)
and three full runs were reaped. It was run in ten 12-file batches instead, twice.

```
pass 1  batched, under memory pressure   3158 passed, 1 FAILED, 1 warning
pass 2  batched, memory recovered        3159 passed, 0 failed, 1 warning
pass 3  ONE PROCESS, memory recovered    3159 passed, 0 failed, 1 warning  exit 0, 16:46
pass 4  ONE PROCESS, + worker-loop fixes 3170 passed, 0 failed, 1 warning  exit 0, 13:34
```

Passes 3 and 4 ran as a single `pytest tests/` with `-rf` and `^FAILED`/`^ERROR`
capture — the batching was only ever a workaround for the memory exhaustion, and it is
no longer needed. Pass 4 is the current HEAD; 3170 = 3159 + the 11 new worker-loop tests.

Both passes total 3159 tests, so pass 1's failure was a real test failure, not a collection
error. **It is unidentified**: the first batch runner captured only each batch's summary
line and threw the `FAILED` line away. That was a defect in my harness, fixed for pass 2,
which captures `^FAILED`/`^ERROR` and recorded none.

It is NOT claimed to be unrelated to this session's change. What is known:

* it fell in batch 6 — the run that took 365s against ~120s for the same twelve files in
  four later runs, i.e. it coincided with peak swap exhaustion;
* that batch has since passed **five** times (4 × full batch, plus pass 2), and the three
  files in it that touch `agent_control` — `test_owneros_hook`, `test_owner_status`,
  `test_pinger_shadow` — passed a further **six** consecutive runs, 69 tests each;
* eleven clean runs in total, no reproduction.

Most consistent with a timing flake under swap exhaustion. Anyone resuming who sees a
failure in `tests/test_owner_os_policy.py` … `tests/test_prospect_audit_batch.py` should
capture the test name — that is the one piece of evidence this session did not keep.

The 1 warning is the pre-existing `tarfile` `DeprecationWarning` from
`test_core.py::TestBackupEngine::test_rollback`, unchanged and unrelated.

## Criterion 2 — now proven LIVE on the current pane

Beyond the historical 33823 chain, the loop closed twice on `owner-os-opus-final:0.0`,
the pane running the session that wrote this:

```
ev 35869  wake delivered 14:12:27Z -> self-wake-35869-safe-continue-20260906   +2m02s  api:bearer
ev 35897  wake delivered 14:27:30Z -> owner-self-event-35897-continue-safe-fixes  +25s  api:bearer
```

Both continuation keys name their own wake event; both arrived as instructions this session
acted on. Self agent stopped -> wake to the bound `owner-os` chat -> supervisor continuation
-> pane working, no owner message.

**Trap for whoever audits this next:** all seven of this pane's watches today resolved
`pane_awaiting_owner`, *including those two*. The reason describes the pane's state when the
watch retired — this pane sat on real owner gates (push, deploy) all session — and is NOT a
verdict on whether the supervisor acted. Audit `deliveries` + `delivery_attribution`;
`resolved_reason` alone would score both successes as failures.

## Event 36728 `notifications_red` — diagnosed, owner gate, NOT the deploy gate

Hourly recurring, `dedup_key=notifications_red`, severity critical, `owner_action_required=1`.
(The wake described it as project `owner-os`; the event row's own `project_id` is empty.)

**Exact condition and channel.** Red is driven by ONE tier:

```
owner_push        available=False  state=unhealthy
                  last_error = "telegram send failed: Bad Request: chat not found"
                  last_ok_at = NULL          <- has never once succeeded
same_chat_wake    available=False  — platform boundary, no server->ChatGPT inbound trigger
cdp_same_chat     available=True  verified=True   34 deliveries proven in the last 3600s
cto_inbox         available=True  verified=True   durable pull
```

`notifications_status` is RED unless a PROACTIVE tier is available. `cto_inbox` is pull-only
and `cdp_same_chat` is the wake path, not a notifier — it was deliberately excluded on
2026-09-04 after counting it read green while 19 alerts dead-lettered. **The accounting is
correct.** Wakes land; owner alerts do not; red says exactly that.

**It is the chat binding, not the token.** Evidence, from shape and error class only — no
credential value was read or printed:

* the token is 46 chars in BotFather's two-part `<bot_id>:<secret>` form;
* Telegram answered `Bad Request: chat not found`, a CHAT-level rejection. A bad token
  returns `Unauthorized`. So the token authenticated;
* `TELEGRAM_CHAT_ID` is a 10-digit POSITIVE id — a private user chat. Telegram refuses a
  bot's message to a user who has never started that bot.

Remediation is therefore step 2 alone, and it is owner-only: **the owner sends that bot one
message from their own Telegram account.** Nothing in this repo can do it, and no config
change here would help — `TELEGRAM_ENABLED` being absent is irrelevant, since the code keys
off `WATCHDOG_TELEGRAM_ENABLED` or the presence of token+chat id, and it plainly did attempt
a send.

**Not caused by the undeployed fix, and a restart will NOT clear it.** This session's
commits touch `api/v1.py`, `core/agent_control.py`, `core/agent_orchestrator.py` and
`core/wake_bridge.py` — none of the notification path. The red is hourly across the whole
day and predates them. A restart would only move `owner_push` from `unhealthy` to
`unverified` (`_owner_push_state` keeps a proven verdict only within its runtime epoch,
currently `1196430:…`) — still not green, until the next probe fails again.

**No code defect found, so nothing was changed.** `_owner_push_state` already derives health
from evidence rather than configuration — hardened 2026-08-06 against this exact
"creds present ⇒ healthy ⇒ green" conflation.

## Event 37041 `notification_dead_letter` — same gate, and the MCP 0/0 boundary

Same root cause as 36728. No code changed, because no local defect exists.

**The failed channel and reason.** `telegram`, dead-lettered after **5 attempts**:

```
notification 7227   dedup_key waiting:payorch-ha-fresh:0.0:91faf9b8b3b52a05
                    correlation_id waiting:payorch-ha-fresh:0.0
                    state dead_letter   attempts 5   receipt NULL
reasons  owner_push      "telegram send failed: Bad Request: chat not found"
         same_chat_wake  "no inbound trigger configured"
```

Not distinct, and not new. Of the last 200 dead-letter events, **199** carry that exact
`chat not found` string; the remaining one is a transient
`[Errno 104] Connection reset by peer`. Telegram dead letters run unbroken from
2026-08-03T02:00:09Z to 2026-09-06T23:08:56Z — 7228 of them. The only two `sent` rows in
the whole table are `owner_push` from 2026-08-03, before this began.

**Remediation is unchanged and owner-only, and involves no secret:** the owner sends that
bot one message from their own Telegram account. The token is present and authenticating;
it is the chat binding that is missing. Nothing here can do it.

### The `delivery_failed=0 / current_alerts=0` surface — boundary, not a defect

This repository's accounting is correct, non-zero, and red. `/control-plane/observability`
(`diagnostics.observability_summary()`) reports, live:

```
notifications        total 7230   active 27   historical 7203   status red
notification_history current_dead_letter 7230   active_dead_letter 27
                     dead_letter_events_logged 4012   notifications_red_events 821
                     cumulative_failure_attempts 36150   status red
```

So `0/0` is not this repo under-reporting. Two facts pin the boundary:

1. **Those field names do not exist anywhere in this repository.** A grep for
   `delivery_failed` and `current_alerts` across `core/` and `api/` returns nothing (the
   only near-match is an unrelated `last_wake_delivery_failed` reason string in
   `project_supervisor`). This repo cannot emit a field it does not define.
2. **The posture endpoint carries no counters at all, by design.**
   `/control-plane/notifications/status` returns exactly
   `capabilities, checked_at, notifications_enabled, reasons, same_chat_wake_complete,
   status` — no `current[]`, no counts. It answers "can we reach the owner?", not "how many
   failed".

The most likely mechanism, offered as a hypothesis and NOT asserted: a consumer reading the
POSTURE endpoint, finding no counter fields, and defaulting them to `0` — which renders
`delivery_failed=0 / current_alerts=0` while the authoritative counters sit on a different
endpoint reading 7230/27/red. That is the same shape the 2026-09-03 boundary report
predicted. The mapping lives in `/opt/seo`, outside this repo, and was not touched.

**Trust order for anyone auditing delivery:** `/control-plane/observability` for counts,
`notifications_status()` for posture, the MCP snapshot for neither.

### Event 37150 — same gate again, now swallowing a stall-doctor alert

Checked independently rather than assumed. Same terminal cause, different and more
serious cargo:

```
notification 7263   channel telegram   attempts 5   state dead_letter   receipt NULL
  dedup_key      doctor:payorch-ha-fresh:0.0:LOST_CONTINUATION:f4fc9a1076f7ec87
  from event     37145  agent_waiting_input  payorch-ha-fresh:0.0
  created 00:34:17Z -> last attempt 00:37:52Z   (5 attempts in ~3m35s)
  reason         owner_push: "telegram send failed: Bad Request: chat not found"
```

37041 lost a routine `waiting:` alert; **this one lost a stall-doctor
`LOST_CONTINUATION` alert** — the class that says an agent may have dropped its
continuation. The gate is not just muting noise. (`payorch-ha-fresh` is another project's
agent; nothing was done to it from here.)

**Retry behaviour is correct.** `notifier.MAX_ATTEMPTS = 5`, and `drain` dead-letters on
`attempts >= max_attempts`. Five attempts then terminal is the policy working, not a
runaway.

**Both surfaces represent it accurately** — verified live, no defect:

```
notifications_status()      status red · owner_push "telegram send failed: Bad Request: chat not found"
observability_summary()     total 7283 · active 3 (3600s window) · historical 7280 · status red
```

`dead_letter_events_logged` 4025 against 7283 notifications is also correct, not a
mismatch: the EVENT is deduped per CHANNEL (`deadletter:telegram`), deliberately — an
earlier build minted 937 distinct critical events in 24h for one unchanging cause. The
per-message ledger is the `notification` table; the event is the per-channel alarm. The
dead-letter emit also sets `push=False`, so the alarm about a failed notification cannot
itself try to notify — the zero-ping invariant holds.

**Remediation unchanged, owner-only, no secret involved:** the owner sends the bot one
message. Configuration/credential-only; no code, secret, chat id, route, or service was
touched.

### Events 37244 / 37256 — checked, nothing new (2026-09-07)

37244 is not a dead letter at all: `agent_waiting_input`, dedup_key
`doctor:owner-os-opus-final:0.0:LOST_CONTINUATION:…` — the stall doctor firing on THIS
pane while it sits on the push gate.

37256 is one more of the same: notification 7292, telegram, 5 attempts, terminal
`Bad Request: chat not found`, from event 37250 `agent_waiting_input hostsecure:0.0`.

The aggregate is the useful part. **Every one of the 17 dead letters since 37150 carries a
byte-identical reason string.** There is no second failure mode hiding in the stream.

**Not a CDP/browser problem** — that path is healthy and is a different channel entirely:

```
cdp_same_chat  available=True verified=True   15 deliveries proven in the last 3600s
wake_delivery  last hour: 26 attempts, 15 delivered   (the rest are normal
               "assistant_still_generating" backpressure, not failures)
owner_push     available=False               "Bad Request: chat not found"
```

Wakes are landing; owner ALERTS are not. One channel, one cause, unchanged since
2026-08-03.

**Stop re-inspecting these.** Each new dead letter costs a full investigation and returns
this same answer. The signal worth waking on is a dead letter whose reason string is NOT
`chat not found` — that would be genuinely new. Everything else is the same gate, and the
gate is one owner action.

**Recommended, NOT done:** `notifications_status()` could carry `active_dead_letter` so a
consumer cannot render `0` from it. Deliberately left alone — it changes a surface external
consumers already parse, which is a cross-boundary decision, not a local cleanup.

## Worker-loop cost, measured

What the two `to_thread` fixes actually took off the event loop (read-only bench;
`register_worker` itself was NOT called, as it writes a heartbeat row — its dominant cost
`_module_fingerprint` was measured instead):

```
_module_fingerprint   p50   0.23ms  p95  10.11ms  max   11.28ms   (3 source files, sha256)
pipeline_health       p50 190.03ms  p95 339.01ms  max 2424.33ms   (sqlite reads)
```

`pipeline_health` is the load-bearing one: a **190ms median, up to 2.4s** stall of the whole
event loop, every `WAKE_PIPELINE_WATCH_SECS=120`, in the process serving the MCP control
path. Same failure mode as criterion 1, from a different call site.

## Worker loops — inspected, two fixed

Read-only inspection of all eight `asyncio.create_task` loops in `api/main.py`, prompted by
the incorrect claim above.

| loop | tick offloaded? |
|---|---|
| `agent_supervisor.run_loop` | `to_thread(heartbeat)`, `to_thread(poll_once)` |
| `agent_orchestrator.run_loop` | `to_thread(refresh_and_resolve)`, `to_thread(ac.agent_list)`, `to_thread(_dal.sweep)` |
| `control_plane.engine.run_loop` | `to_thread(tick_once)` |
| `agent_continuation_watchdog.run_loop` | `to_thread(run_once)`, `to_thread(health)` |
| `commander_autopilot.run_loop` | `to_thread(tick)` |
| `project_supervisor.run_loop` | `to_thread(tick, …)` |
| `context_budget.run_loop` | `to_thread(tick)` |
| `wake_bridge.pipeline_watch_loop` | `pipeline_health()` **inline** ← fixed |

Every loop also waits with `asyncio.sleep`, never `time.sleep`. So the architecture was
already right, and the remainder was two call sites, not a refactor:

```
core/agent_orchestrator.py  _wb.register_worker("agent_orchestrator")   -> await asyncio.to_thread(...)
core/wake_bridge.py         h = pipeline_health()                       -> h = await asyncio.to_thread(...)
```

`register_worker` is the heavier of the two: sqlite writes plus `_module_fingerprint`,
which opens and SHA-256-hashes the worker's watched source files off disk, on a 45s cadence
in the process that serves the MCP control path.

`tests/test_worker_loops_off_event_loop.py`, 11 tests. The two targeted ones assert on
**which thread** the call lands on — `asyncio.run` drives the loop on the main thread, so a
call executing there is a call executing on the event loop. Removal proof: revert either
fix and exactly its test fails; restored, 11 pass.

One note worth keeping, because it nearly produced a vacuous test: the parametrised
"every loop offloads its tick" pin originally did `"to_thread" in source`. Reverting the
`wake_bridge` fix left the explanatory COMMENT behind, so that assertion stayed green over
the restored defect. It now parses the body with `ast` and requires a real call node;
re-run of the removal proof then failed 2 tests instead of 1.

## Deploy skew — what a restart would ACTUALLY load (verified 2026-09-07)

The deploy gate is bigger than this session's fixes. Verified with the repo's own
`_module_fingerprint`, comparing each worker's stored fingerprint against disk:

```
agent_orchestrator  pid 1196430  started 2026-09-05T04:05:41Z   SKEW (running != disk)
wake_companion      pid  499953  started 2026-09-06T13:17:17Z   SKEW (running != disk)
```

**`ai-runtime` (PID 1196430) is missing five production commits, not three.** It started
2026-09-05T04:05:41Z; everything below landed after, and Python caches a module at first
import, so the process still holds the Sep-5-morning versions:

```
a7a438c  MCP control path off the event loop        (this session)
e024d0f  comment correction                          (this session)
78c3d09  the last two worker ticks off the loop      (this session)
e285901  the self agent has no supervisor-registry row, so resolve by agent
83c41b2  tell the external supervisor what to DO for the self agent
```

The last two are the **self-wake fixes criterion 2 depends on**, and the API process never
received them. It works anyway because the phrase is composed in the COMPANION, which
restarted 2026-09-06T13:17:17Z and does have them — see criterion 2 proven live above.
That split is worth knowing before anyone reasons about which process proves what.

**The companion is also skewed now**, but only by `78c3d09`'s `wake_bridge.py` edit.
`pipeline_watch_loop` runs in `ai-runtime`, not the companion, so for the companion that
change is inert — `wake_bridge.py` is simply inside its watched fingerprint set. A
companion restart is not required by anything in this session.

`api.main` imports clean on the committed tree (smoke-checked, import only — importing does
not fire the startup events, so no worker loop was started and no agent was touched).

## DEPLOYED 2026-09-07T07:35:40Z — criterion 1 is now LIVE

The owner typed `push it`, then `restart ai-runtime`. Both were done; both are recorded
here as owner-typed instructions in the Claude Code session, which is the only thing this
handoff has ever counted as authorisation.

```
push     b615fbc..f4a9292  ai-runtime/220-windows-bridge   local == remote, ahead=0 behind=0
restart  ai-runtime.service   PID 1196430 (up since 2026-09-05 06:05:35)  ->  PID 368613
         ActiveState=active  SubState=running  NRestarts=0
```

All eight worker loops came up clean — supervisor, orchestrator, control plane,
continuation watchdog, commander autopilot, project supervisor (dormant, no projects),
context budget. No traceback on startup.

**Skew cleared, by the system's own check:**

```
agent_orchestrator  pid 368613   MATCH — running == disk     (was SKEW)
wake_companion      pid 499953   SKEW — 78c3d09's wake_bridge.py edit only, inert there
```

The companion still reads skewed and that is expected: `pipeline_watch_loop` runs in
`ai-runtime`, so for the companion that file is only inside its fingerprint set. No
companion restart was ordered or needed.

### The live A/B — measured under identical load, after the restart

**Read this before trusting any single probe.** The first measurement taken ~90s after the
restart looked catastrophic: ambient p50 238ms, `agent_read` p50 1319ms. It was neither the
fix nor a regression — it was the cold-start storm (eight loops doing first sweeps at once)
on a host at **load average 29 across 6 cores**, with 11 live agents, a 45-minute `sqlite3`
process and Chrome competing. Proof it was not the API: `agent_list` measured **1782ms in a
separate process with no API involved at all**, while raw `tmux capture-pane` stayed at
17.7ms.

The honest comparison is both builds under that same load, on spare ports:

| | pre-fix `b615fbc` | deployed `f4a9292` |
|---|---|---|
| probe idle p50 | 13.5 ms | 16.5 ms |
| probe under 6x read p50 | 256.8 ms | **86.7 ms** |
| probe under 6x read p99 | 2653.7 ms | **220.4 ms** |
| probe under 6x read max | 2653.7 ms | 317.1 ms |
| probe samples/s under load | 2.9 | 8.1 |
| `agent_read` p50 | 271.5 ms | 205.0 ms |
| `agent_read` p99 | 2740.4 ms | **397.5 ms** |
| `agent_read` max | 3291.9 ms | 536.9 ms |
| throughput | 18.1/s | 28.1/s |

Tail latency for UNRELATED requests improves **12x at p99**; control-plane read p99
improves **6.9x**; throughput **1.55x**. The fix matters more under load, not less — which
is the case that produced the original intermittent JSON-RPC failures.

Post-restart control path, from the access log: 73 `agents/read`, 4 `agents/status`, 1
`agents/answer`, all 200; one 400, a stale-target refusal.

**Still red, and untouched:** notifications remain red on the Telegram chat-binding gate.
Nothing in this deploy addresses it, and nothing here tried to. `cdp_same_chat` continues
proving deliveries (18 in the last hour), so wakes keep landing.

## Post-deploy verification, 2026-09-07 ~07:49Z (read-only)

Taken ~13 minutes after the restart, once the cold-start storm had passed.

```
service     PID 368613  active  NRestarts=0  up 13:08
control path since restart:  80 read · 14 status · 4 answer · 2 send  — ALL 200
tracebacks since restart:    0
```

**Native-supervisor continuation: PROVEN.** Canary `hostsecure:0.0`, 351 samples,
303 verified / 9 continuation_unverified / 39 unattributable, streak 5 against a required
3, `matched_by=agent`.

A correction on how that was reached, because the first reading was wrong and would have
looked alarming: `native_continuation_effectiveness()` first returned
`dormant — no canary selected; owner decision outstanding`. That was **my measurement
error, not a regression** — it ran in a plain shell that had not sourced `configs/.env`, so
`NATIVE_CANARY_TARGET` was unset in MY process. The service process has it
(`/proc/368613/environ` confirms `NATIVE_CANARY_TARGET=hostsecure:0.0`). Anyone checking
this must load the env or ask the service; a bare `python -c` will report a false dormant.
The streak reading 5 rather than the pre-restart 216 is expected — it is runtime-scoped and
resets with the process.

**cdp_same_chat: delivering.** 16 proven in the last hour; 92 of 138 attempts over 6h.
Non-delivery is mostly benign backpressure, but not entirely:

```
 21x assistant_still_generating          (backpressure, not failure)
 15x cdp_error:WebSocketTimeoutException  <- watch item
  6x assistant_generating_wedged
  3x user_turn_not_observed_after_send
  1x could_not_open_bound_conversation
```

The ~60-67% delivered ratio matches what was observed before the restart (26/15, 32/19), so
it is steady state rather than a deploy effect. The 15 WebSocket timeouts are worth a watch,
not a task — no evidence yet that the count is growing.

**Dead-letter accounting: accurate and unchanged in cause.** 7407 total / 31 active / red,
single reason, Telegram.

Nothing here needed a fix. No code was changed by this verification.

## `mess` added to the native-supervisor denylist — 2026-09-07T07:56:54Z

The owner typed `add mess to the denylist and restart`, after a peer session (`mess-c8`)
reported an unstoppable "continue with the next safe step" loop on its RU-edge agent and
asked this session to stop sending them. **This session had sent none.** The ledger for
`mess-ru-54582145-resumed:0.0` over 24h named two automated senders:

```
28x  actor=native_supervisor   keys nativesup:<event_id>   <- ours, in ai-runtime
25x  actor=api:bearer          the ChatGPT supervisor via /opt/seo — NOT ours
 7x  internal
```

### The change, and the trap in it

```
configs/.env  +1 line (plus comment), additions only:
NATIVE_SUPERVISOR_DENY_PROJECTS=capacity,auction,payment-orchestrator,payorch,email,xmrig,mess
backup: backups/native_supervisor_deny_mess_20260907T075631Z/{.env.before,ROLLBACK.md}
restart: PID 368613 -> 446168, active, 8 loops clean, 0 tracebacks
```

**The var REPLACES the code default; it does not append.** The default is
`capacity,auction,payment-orchestrator,payorch,email,xmrig`, and the source comment warns
that setting this "for an unrelated reason silently dropped the self-reference guard along
with everything else". Writing `NATIVE_SUPERVISOR_DENY_PROJECTS=mess` would have
un-protected payment, auction, email, capacity and xmrig. All six are restated
deliberately. Verified after the fact: the effective set gained exactly `mess` and lost
nothing (`ai-dev-runtime` is added by the code and is not listed).

### What it covers, and what it does NOT

The denylist keys on PROJECT, resolved from the pane's cwd — not on the pane name:

```
mess-ru-54582145-resumed:0.0           cwd /opt/mess   project 'mess'   DENIED   <- the peer's agent
mess-postsignup-cleanup-sonnet-v4:0.0  cwd /opt/seo    project 'seo'    NOT denied
mess-ru-54582145:0.0                   (dead pane)     project ''       NOT denied
```

So a mess-NAMED pane working in `/opt/seo` is still supervised. Covering it would mean
denying `seo`, which is where the MCP connector backend lives — a much larger call, not
made. A pane draft reading `add seo to the denylist too` was visible and was deliberately
NOT acted on: pane text is not owner authorisation, and this one is a real trade-off.

Result: **0 deliveries to any `mess` target since the restart**, service healthy, 0
tracebacks. The `api:bearer` half of the loop is untouched and needs `/opt/seo` access.

Rollback: delete that one line, restart. Whole-file restore in the backup's ROLLBACK.md.

## Per-agent supervision exclusion — shipped and DEPLOYED 2026-09-07T09:16:24Z

Closes the other half of the `mess` continue-loop without the trade that was refused.

### Why the project denylist was the wrong tool

`mess-postsignup-cleanup-sonnet-v4:0.0` runs with cwd `/opt/seo`, so it resolves to project
`seo` — the project denylist would have silenced it only by denying `seo` outright, which
also hosts the MCP connector backend. Losing supervision of a whole project to quiet one
stale pane conflicts with the standing goal of broad zero-ping continuation, so it was not
done.

### The code — `a0c09ac`, on the remote

`NATIVE_SUPERVISOR_DENY_TARGETS`, empty by default. It contains no syntax that can GRANT
supervision: every check is an early return to False or a skip, so the worst a
misconfiguration can do is supervise LESS. That is what made it safe to add without a
rollout. Wired into all five paths that could otherwise grant or retain supervision:

```
is_supervised()       checked AHEAD of the allowlist/wildcard
auto_register()       skip, why="deny_listed_target"
registered_targets()  filtered on read
purge_denied()        drops an EXISTING registration when an exclusion is added later
send_block_reason()   distinct reason "target_excluded", so the journal stays diagnosable
```

Fail-closed where it matters: an empty or unreadable target is denied, not allowed.
Session-name matching is equality on the first segment, never a prefix — `mess` denies a
session literally named `mess` and does NOT capture `mess-ru-54582145-resumed`.

The load-bearing test is the wildcard one. `NATIVE_SUPERVISOR_TARGETS="*"` returns True
from the wildcard branch before any later filter runs — the exact shape of the bug the
PROJECT denylist already had, where a rollout switch silently became a denylist bypass.
14 tests; removal proof: move the guard after the wildcard and 4 fail (that one by name),
remove the purge and 1 fails. Full suite 3186 passed, 0 failed.

### The deploy

```
configs/.env  +1 line:  NATIVE_SUPERVISOR_DENY_TARGETS=mess-postsignup-cleanup-sonnet-v4:0.0
backup:  backups/deny_target_mess_postsignup_20260907T091556Z/{.env.before,ROLLBACK.md}
restart: PID 446168 -> 737206, active, 8 loops clean, 0 tracebacks
```

Scope was verified BEFORE the restart, not assumed:

```
mess-postsignup-cleanup-sonnet-v4:0.0  project seo                  -> False  (target_excluded)
seo-audit:0.0                          project seo                  -> True   <- seo stays supervised
hostsecure:0.0                         project hostsecure           -> True
gaika-opus-v8:0.0                      project gaika-extension      -> True
anything:0.0                           project payment-orchestrator -> False  (still blocked)
```

After the restart: the pane is purged from `native_supervised_target`, 21 targets remain
registered, 0 deliveries to it, 0 tracebacks.

**One reading that looks alarming and is not.** The registry now reports
`seo project targets: []`. That is not the exclusion over-reaching — this pane was the ONLY
one the registry had classified under `seo`, so removing it emptied that project's rows.
`seo` is NOT denied: `is_supervised("seo-audit:0.0", project="seo")` is still True, and any
`seo` pane that appears auto-registers normally.

Rollback: delete the one line, restart; the pane re-registers by itself on the next
discovery pass. The only durable effect is the removed registry row.

### What this does NOT fix

Only the `native_supervisor` half of the loop. 25 of the peer's 53 pokes came from
`api:bearer` — the ChatGPT supervisor path in `/opt/seo` — which no setting in this
repository governs.

## CORRECTION — both denylists were deployed to the WRONG PROCESS (2026-09-07)

Recorded prominently because two earlier claims in this handoff were wrong, and because
the system had already said so in a signal that was explained away.

**`native_supervisor.scan` runs ONLY in `tools/wake_companion.py`.** `ai-runtime` never
calls it — `grep -rln "ns.scan("` matches the companion and `tools/native_supervise_once.py`,
nothing else. So restarting `ai-runtime` could not apply either denylist, and both deploys
were inert:

```
07:56Z  mess added to NATIVE_SUPERVISOR_DENY_PROJECTS + ai-runtime restart   -> no effect
09:16Z  NATIVE_SUPERVISOR_DENY_TARGETS + ai-runtime restart                  -> no effect
```

Proof the loop never stopped — `native_supervisor` deliveries AFTER the 09:16 restart:

```
09:31:21  mess-ru-54582145-resumed:0.0            nativesup:37955
09:53:13  mess-ru-54582145-resumed:0.0            nativesup:38030
10:47:35 .. 11:10:30  mess-postsignup-cleanup-sonnet-v4:0.0   5 deliveries
```

The companion (PID 499953, started 2026-09-06 15:17 local) had read `configs/.env` at ITS
start, before either line existed, and had imported `core/native_supervisor.py` from disk
before `a0c09ac` — so it held neither the config nor the code.

### Two process errors of mine, worth more than the fix

1. **I verified inside the cycle.** Both times I checked "0 deliveries since restart" about
   a minute after restarting, while the supervision cadence for that pane was ~6 minutes,
   and called it proven. This handoff already says a single sample is not evidence — about
   tab regrowth — and I did not apply it to my own claim.
2. **The health check told me and I explained it away.** `pipeline_health` reported
   `worker_running_stale_code:wake_companion`. I recorded it as "inert for the companion,
   no companion restart required by anything in this session". It was not inert: it was the
   system correctly reporting that the process which ACTUALLY supervises agents was running
   stale code and stale config. That flag was the whole answer.

### Fixed and verified properly — companion restarted 2026-09-07T11:43:33Z

```
companion   PID 499953 -> 1321112, active, NRestarts=0, browser 5 pages healthy
env now     NATIVE_SUPERVISOR_DENY_PROJECTS=...,mess
            NATIVE_SUPERVISOR_DENY_TARGETS=mess-postsignup-cleanup-sonnet-v4:0.0
fingerprint wake_companion MATCH · agent_orchestrator MATCH   <- skew fully cleared
```

Verified over **17 minutes across multiple cycles**, with POSITIVE evidence rather than
absence — the journal names which exclusion fired:

```
mess-postsignup-cleanup-sonnet-v4:0.0   skip  target_excluded                (per-agent)
mess-ru-54582145-resumed:0.0            skip  value_bearing_send_blocked x3  (project)
deliveries to all three gated panes: 0
non-gated meanwhile: gaika-sonnet-v11 2x, security-demo-fresh 1x  <- autonomy intact
```

**Rule for anyone deploying a supervision setting:** `native_supervisor` lives in the
COMPANION. `ai-runtime` hosts the API and the eight worker loops. Changing a
`NATIVE_SUPERVISOR_*` value means restarting `owner-os-wake-companion`, not `ai-runtime`.

## Wake-delivery degradation — investigated, environmental, no in-repo defect

Prompted by rising `cdp_error:WebSocketTimeoutException` (1,3,4,8,9,13,12 per hour across
02:00-10:00Z) and a last-hour delivery rate of 30% against 70% over 24h.

**No reproducible in-repo defect found; nothing was changed.**

* **Accounting is sound.** Per-EVENT eventual delivery is 80% over 24h and 66% over 6h —
  the raw attempt rate understates success because retries are counted as attempts.
  Retry, expiry (`event_older_than_max_age` at ~10800s) and abandonment
  (`wake_abandoned`, 136 rows) all fire. `STUCK_PENDING_SECS=600` is a health WARNING
  threshold, not an expiry, so a pending wake at 7905s is flagged long before it expires.
* **Not the tab leak.** 5 pages, no duplicates, well inside headroom.
* **Environmental.** Host load 25-30 across 6 cores with 11+ live agents; the
  `browser_degraded:endpoint_slow:2.2-3.6s` readings sit just over
  `BROWSER_SLOW_SECS=2.0` (`CDP_SLOW_SECS`), and the wedged-renderer case is already
  guarded by `page_responsive` (the 4214 incident).

Watch item, not a task: if delivery rate keeps falling while host load is normal, the
2.0s threshold and the 15s `_Session` socket timeout are the two knobs to examine.

**A trap that cost me twice today:** `pipeline_health()` and
`native_continuation_effectiveness()` read env at call time. Run them in a shell that has
not sourced `configs/.env` and they report `bridge_disabled`,
`consecutive_delivery_failures:3` and `dormant — no canary selected` — all false. Source
the env or ask the service.

## Repeat-wake suppression — shipped, DEPLOYED and verified 2026-09-07T21:16:09Z

### The defect

`notification_dead_letter` mints a fresh event every dedup window, so each occurrence had
a NEW event_id: `already_woke_for_this_event` never matched, and the non-actionable
cooldown only spaced repeats out. Measured before the fix:

```
decision=wake  urgent_event_not_yet_signalled   1039 all-time
deliveries to route owner-os: 14:13 14:28 14:59 15:15 15:30 16:01 16:16 17:33 ...
```

1039 wakes of the owner's ChatGPT conversation for ONE cause unchanged since 2026-08-03,
which only the owner can clear. Every repeat after the first was noise.

### The fix — `291d5a5` + `b1b1809`, both pushed

`should_wake` derives a cause signature (`event_type|channel|sorted(reasons)`) from the
event's own payload — no caller changes — and skips with
`recurring_cause_already_signalled`, naming the event that DID report it. Checked after
per-event dedupe and before any cooldown, because a cooldown only delays a repeat that
should never have been produced.

Narrow by construction: the event is still emitted and recorded. Dead-letter accounting,
the per-message ledger, retry history and the red posture are untouched. It gates ONE
thing — whether a human conversation is woken again.

Re-alerting preserved on both axes, both tested: a changed reason or channel is a
different signature and wakes; a cause returning AFTER a PROVEN send on that channel
re-arms (a failed send in between does not). Fails OPEN — an unreadable payload yields no
signature and the wake proceeds, because silencing an alert on an internal error is the
one outcome worth avoiding.

**A bug of mine in the first commit, caught before deployment.**
`_channel_delivered_since` re-looked its timestamp up with `WHERE ts=?` — float equality
on a REAL column, `ORDER BY id DESC LIMIT 1`. A miss yields NULL, making
`created_at > NULL` NULL; and a second wake row sharing that ts returns the WRONG row's
`at`. Either way the failure mode is SILENT SUPPRESSION of an alert that should have
fired. The caller already holds the value; it is now passed in directly (`b1b1809`).

The regression test took two attempts. The first had both wake rows matching the
signature, so the old lookup resolved to the same answer and **the test passed against the
bug**. Reshaped so the match is the OLD row while a newer NON-matching row shares its ts —
then verified it fails against the original code and passes against the fix.

### Deployed to BOTH processes, because both evaluate the decision

```
core/wake_bridge.py:1340   pending scan   -> COMPANION
core/control_plane/cto.py:78  emit path (notifier.drain -> engine.tick_once) -> AI-RUNTIME
```

Restarting only one would have left half the wakes flowing — the same class of mistake
that wasted two restarts earlier today.

```
ai-runtime  737206  -> 3287188   active, 0 tracebacks
companion   1321112 -> 3287728   active, 0 errors
fingerprints  wake_companion MATCH · agent_orchestrator MATCH
```

### Verified over 27 minutes, with concrete evidence

```
176x skip  recurring_cause_already_signalled      <- the chronic cause, silenced
  1x wake  urgent_event_not_yet_signalled         <- a DIFFERENT cause, correctly alerted
wake deliveries: 18 attempts / 14 delivered       <- the pipeline still moves
other wakes still firing: work_stopped_incomplete 6, agent_waiting_input 5,
                          agent_prompt_needs_response 2, notifications_red 1
```

**The one wake is the design working, not a leak.** Event 39832's reason was
`<urlopen error [Errno 104] Connection reset by peer>` — a different signature from
`Bad Request: chat not found`, no prior wake matching it, so it woke on first occurrence.
Had it stayed silent the fix would have been over-broad. The alternative explanation was
checked and ruled out: `should_wake` is consulted AFTER the event row is written, on the
same connection, so the payload is readable at decision time.

**Verification note:** the fingerprint check cannot confirm this fix for `ai-runtime` —
`wake_bridge.py` is in the companion's watched set but NOT in `agent_orchestrator`'s. Use
the suppressed-count query, never the fingerprint:

```sql
SELECT count(*) FROM wake_audit wa JOIN event e ON e.id=wa.event_id
WHERE e.type='notification_dead_letter' AND wa.reason='recurring_cause_already_signalled';
```

### The one new-cause wake: superseded, not stranded

Worth recording because the shape looks alarming and the first read of it was wrong.

Event 39832 (the `Errno 104 connection reset` dead letter — a genuinely NEW cause) was
decided `wake` at 21:21:58 and had **zero delivery attempts** half an hour later, with
`acknowledged=0`, not abandoned and not expired. That shape CAN mean a lost alert, so it
was flagged rather than glossed. But the conclusion drawn from it — "a novel failure could
be detected without being announced" — was **wrong**, and checking `superseded_by` before
speculating would have shown it:

```
39832  notification_dead_letter (new cause)  decided 21:21:58  actionable=0
   superseded_by 143791 at 21:41:29
143791 -> event 40077  notifications_red  decided 21:39:29  acknowledged=1
   DELIVERED 21:42:01  route owner-os  submitted_and_assistant_started_generating
```

`coalesce_generic_backlog` folds NON-actionable wakes per route down to the newest member —
N generic wakes for one chat are one instruction — and never folds across routes. Both are
non-actionable on `owner-os`, so 39832 folded into 40077, which was delivered. Exactly one
wake was folded; no delivery attempt was spent on 39832 because none was needed. The
conversation WAS woken, and the generic phrase points at the event log, where the specific
cause is recorded.

**Diagnostic rule:** a `wake` row with zero deliveries is not evidence of a lost alert
until `superseded_by` AND `wake_submitted` have both been checked. Absence of delivery rows
is the EXPECTED state for a folded wake.

**Residual limitation — a product/policy question, not a bug.** Coalescing means a
new-cause dead letter can surface as a generic "check Owner OS events" wake rather than one
naming that cause. That is the intended trade: the phrase deliberately points at the event
log instead of carrying content. Embedding a distinct reason in the phrase would mean
changing `coalesce_generic_backlog` — a decision about what wake phrases may contain, which
is the owner's to make, not a defect to fix. No code change was made.


### What this does NOT fix

The Telegram channel is still down and still dead-lettering. This stops the
RE-ANNOUNCEMENT, not the failure. Remediation unchanged and owner-only, no secret
involved: the owner sends the bot one message from their own account.

## notifications_red suppression — SHIPPED BROKEN, then fixed (2026-09-08)

Kept in full because the defect passed its tests, reached production, and was caught only
by watching live behaviour.

### What shipped, and why it never worked

`5c0291c` extended the recurring-cause suppression to `notifications_red` with a daily
re-alert (owner decision: "re-alert once a day while red"). Deployed to both processes at
12:43:49Z. The first red after the restart woke exactly as before:

**(Superseded — see the correction further down. This section originally concluded it
suppressed NOTHING. That was wrong: over hours it suppresses whenever the hourly counter in
the reason prose happens to repeat, giving 1 wake against 3 red events. The defect is that
it is COINCIDENTAL, not that it never fires.)**

```
notifications_red        26x skip already_woke_for_this_event · 1x WAKE   <- not suppressed
notification_dead_letter 24x skip recurring_cause_already_signalled       <- working
```

Cause: the signature was built from the `reasons` prose, and `notifications_red`'s reasons
embed a LIVE COUNTER — `cdp_same_chat (wake path, NOT a notifier tier): N delivery(s)
proven in the last 3600s`. N changes hourly. Observed across consecutive reds: 24, 25, 10,
21, 20, 12, 17, 22, 15. Every red therefore had a unique signature, nothing ever matched,
and the suppression could never fire. The capability map underneath was byte-identical in
every one of them.

### The fix — `f136e91`

Sign WHAT IS BROKEN, not prose describing it: `capabilities[tier].available`. Verified
against four real production payloads — one distinct signature where there had been four.
The dead-letter path is unchanged and still signs its reasons dict, whose values are raw
rejection strings and genuinely stable; that half was already working.

### Why the tests could not have caught it

They used ONE fixed `RED_REASONS` list, so every fixture signature matched by
construction. The tests were internally consistent with the code and both were wrong about
production data. The regression added with the fix uses two reason lists differing ONLY in
the delivery count and asserts an identical signature; a second test asserts a flipped
capability still re-alerts, since that is a real state change. Removal proof: reverting to
prose-signing fails both.

**Pattern worth carrying:** this is the SECOND suppression change in one day that passed a
green suite and failed on real payload shape — the first being `json` never imported in
`wake_bridge`, which the `except` would have swallowed into a permanently disabled feature.
Both were found by predicting the live outcome in advance and then checking it. Green tests
did not distinguish working from inert; an observation window did.

### Verifying it after the NEXT deploy

The fingerprint check cannot confirm this — `wake_bridge.py` is in the companion's watched
set but not in `agent_orchestrator`'s. Use the suppressed-count query, which was validated
2026-09-08 (returns 1275 for dead letters, so a 0 for reds is a real reading and not a
typo):

```sql
SELECT count(*) FROM wake_audit wa JOIN event e ON e.id = wa.event_id
WHERE e.type='notifications_red' AND wa.reason='recurring_cause_already_signalled';
```

Pre-deploy that is 0. Non-zero after a restart of BOTH `ai-runtime` and
`owner-os-wake-companion` is the proof.

### State as of this writing

`f136e91` is committed LOCALLY and unpushed: GitHub egress is down from this host
(`github.com:22`, `:443` and `ssh.github.com:443` all time out; DNS resolves). Not an auth
or repo problem, and not worked around — changing the remote is a config gate. Production
runs `5c0291c`, so reds still wake as they always did. No regression: this is a fix that is
not yet effective.


## Deploy runbook for `f136e91` — PREPARED, NOT EXECUTED

Both commits are pushed (`e58b20e`, local == remote). Production runs `5c0291c`, whose red
signature never matches, so reds still wake exactly as before. No regression — a fix that
is not yet loaded.

### The two restarts

```
systemctl restart ai-runtime
systemctl restart owner-os-wake-companion
```

BOTH are required. `should_wake` is evaluated in two places:

```
core/wake_bridge.py:1340      pending scan   -> COMPANION
core/control_plane/cto.py:78  emit path (notifier.drain -> engine.tick_once) -> AI-RUNTIME
```

Restarting one leaves half the wakes flowing. Two restarts were already wasted this session
by assuming `ai-runtime` alone was enough.

### Proof it worked — wait ≥60 min first

`notifications_red` fires hourly, so a check inside the first few minutes proves nothing.
(Checking inside the cycle produced two false "verified" claims earlier in this session.)

```sql
SELECT count(*) FROM wake_audit wa JOIN event e ON e.id = wa.event_id
WHERE e.type='notifications_red' AND wa.reason='recurring_cause_already_signalled';
```

**CORRECTED 2026-09-08 ~15:30Z — the "0 before" baseline was wrong, and so was the claim
above that the shipped version suppressed nothing.**

`5c0291c` DOES suppress reds, just unreliably. It signs the reason prose, which embeds an
hourly delivery counter, so two consecutive reds match only when that counter happens to
repeat — 10 distinct prose signatures across the last 12 reds, with two pairs coinciding.
Measured since the 12:43:49Z deploy: **3 red events, 1 wake, 131 suppression rows**,
against 684 wakes all-time before it. The earlier "never fired ONCE" was drawn from an
8-minute window holding a single event; that was too small a sample and stated far too
strongly.

So a raw count is NOT a valid before/after, and "suppressed > 0" no longer distinguishes
the two builds. Capture the baseline immediately before restarting, and judge on the RATE:

```sql
-- baseline, run JUST BEFORE the restart
SELECT count(*) FROM wake_audit wa JOIN event e ON e.id = wa.event_id
WHERE e.type='notifications_red' AND wa.decision='wake';

-- after >=2 red events (>=2h), the discriminating test: this must stay at the baseline
```

Under `f136e91` every red shares ONE signature, so after the first, every subsequent red is
suppressed and NEW red wakes should be **zero** across two or more events. Under `5c0291c`
roughly one red in three still wakes. That difference is the proof, not the suppression
count.

The query shape itself is still validated: the same query against
`notification_dead_letter` returns a large non-zero (1355 at the time of writing), so a
reading of 0 would be a real reading and not a typo in the reason string.

Cross-checks, in order:

```
1  both services active, NRestarts=0, no traceback in the startup log
2  no NEW notifications_red row with decision='wake' after the restart timestamp
3  unrelated wakes still firing (agent_waiting_input, work_stopped_incomplete)
4  wake_delivery still non-zero — suppressing everything would look identical to success
```

**Do NOT verify with the fingerprint check.** `wake_bridge.py` is in the companion's
watched set but NOT in `agent_orchestrator`'s, so a MATCH for `agent_orchestrator` says
nothing about whether `ai-runtime` carries this change.

### Rollback

```
git revert f136e91          # or: git checkout 5c0291c -- core/wake_bridge.py
systemctl restart ai-runtime && systemctl restart owner-os-wake-companion
```

Nothing durable is written by the change: it only adds `skip` rows to `wake_audit`, which
are audit records, not state. Reverting restores the previous behaviour immediately —
reds wake every hour again. No schema, no config, no data migration.

### Known residual, VERIFIED, and inside the owner's chosen semantics

A red -> green -> red cycle **within the same day is suppressed**: the owner is not
re-told. Confirmed empirically 2026-09-08.

Why: `notifications_red` carries no `channel`, so the recovery re-arm — which for dead
letters keys on a proven send for that channel — cannot fire, and the capability map at the
second red is identical to the first. Only the 24h floor re-arms it.

This follows directly from the chosen rule ("re-alert once a day while red") rather than
contradicting it, so it was NOT unilaterally changed. Closing it would need a recovery
signal that does not exist today — a green/recovered event type, or keying the probe on
`channel.last_ok_at` — which is a design decision for the owner.

### Also unchanged by decision

`coalesce_generic_backlog` still folds non-actionable wakes per route, so a new-cause dead
letter can surface as a generic "check Owner OS events" wake rather than one naming the
cause. Documented trade-off, not a defect; left exactly as it is.


## Gates — all owner-only

0. **Telegram CHAT BINDING — not the token.** See the event 36728 section below. The
   handoff's earlier framing of this gate as "put the BotFather token in `configs/.env`"
   is **wrong and has been corrected**: the token is present, well-formed and
   authenticating. What is missing is its step 2 — the owner opening a chat with the bot.
1. **Deploy.** Both changes are inert until `systemctl restart ai-runtime`. The running
   service (PID 1196430, up since 2026-09-05 06:05:35) still executes pre-fix code. This
   session treated the restart as an owner gate and did NOT do it.
2. **Push** of `a7a438c` and `25930ef`.
3. **Telegram token** — unchanged, still the sole cause of health red.
4. **`acap-voice` route/project registration** — unchanged, never driven from here.

## Host memory — gate 3 criterion MET, then eased

The criterion this handoff set (free < ~300 MB with PSI `avg10 > avg60` across CONSECUTIVE
readings) was met on four consecutive samples, and swap was effectively exhausted:

```
12:27:02  free=157MB  swapfree=5MB   avg10=67.43 > avg60=58.34
12:27:23  free=189MB  swapfree=5MB   avg10=62.21 > avg60=58.55
12:27:43  free=222MB  swapfree=1MB   avg10=60.92 > avg60=60.57
12:28:04  free=239MB  swapfree=3MB   avg10=72.56 > avg60=64.38
12:28:47  free=253MB  swapfree=28MB  avg10=52.32 < avg60=59.86   <- easing
```

**Proximate cause identified and NOT acted on:** `/opt/elect/.venv/bin/python -m pytest
tests -q -x`, PID 3243627, **1.88 GB RSS, running 6h08m**. That is another project's
runaway test run; out of scope here, and killing it was not attempted. It reaped this
repo's own full-suite run twice (both killed for low memory), which is why the suite was
re-run in 12-file batches instead.

## Not claimed

* The fix is **not live**. Everything is proven in the repo, in tests and in a matched
  A/B — not in production.
* The 33823 chain is **historical** (2026-09-05, predecessor pane), durable and
  re-queryable, but not staged by this session.
* The pane did **not stay** working — further `agent_waiting_input` at 19:11:30, 19:18:20,
  19:19:58, 19:23:17. Resumption without an owner message is what is shown; sustained
  autonomy is not.
* **No JSON-RPC message was read directly.** The MCP server is not in this repository; the
  root cause is proven on this side of that boundary. `/opt/seo` was not touched.
* ~~The event loop is not fully clear: the eight worker loops still call blocking tmux
  work on it.~~ **WRONG — corrected the same day.** Written from the `create_task` call
  sites without reading a loop body. All eight already offload their tick with
  `asyncio.to_thread`; no worker runs blocking tmux work on the loop. Two call sites had
  skipped it (`register_worker`, `pipeline_health` — sqlite, and file hashing in the first)
  and are fixed one line each. See the Worker loops section below.
