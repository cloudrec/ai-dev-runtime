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
HEAD        31d7cb6                UNPUSHED (ahead 1), tracked tree clean
upstream    origin/ai-runtime/220-windows-bridge   (last pushed: c084d43)
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

### Local, NOT pushed

`31d7cb6` — tests pinning that the stall doctor's suppression closes a WATCH and cannot
gate a DELIVERY. Tests only, no production change. 212 passed across `closed_loop_wake`,
`wake_bridge`, `owneros_hook`.

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
3. **Push of `31d7cb6`.** Requested and forbidden in alternating turns by the automated
   channel all session; the pane's `push it` line is machine-queued and blocked by Owner
   OS's own `queued_line_not_submittable:forbidden_token` guard (events 35059, 35133,
   35193). It needs a typed owner instruction.

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
