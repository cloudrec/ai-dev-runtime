# Owner OS handoff — 2026-09-05

State, not narrative. Current facts only. First written from the repo and runtime at
~05:15Z; the Repo, Tests, Services, Browser and Next-safe-step sections were rewritten at
~08:55 CEST / 06:55Z, finalised at ~09:25 CEST / 07:25Z once both long-running checks had
actually finished, and closed out at ~09:40 CEST / 07:38Z when the first live recovery was
observed. The 05:15Z browser state is kept at the end of that section for the
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
| HEAD | `5919b00` + this handoff commit — pushed, `origin` in sync, **0 unpushed** |
| Upstream | `origin/ai-runtime/220-windows-bridge` |
| Tracked tree | clean |
| Untracked | 34 files, all under `reports/` — preserve, never `git add reports/`; every commit below staged EXPLICIT paths |

19 commits landed this session: 9 fixes, 1 feature, 9 reports. The load-bearing ones,
newest first:

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
companion    PID 1770858  up 2026-09-05 08:28:39 CEST  active   (was 1036186, up 05:28)
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

## Host memory — improving, monitor only

```
load  29.29 -> 9.98      swap 15.4 / 20 GB      free 666 MB      paging si=2208 so=0
```

`so=0` means nothing is being evicted; earlier it was `si=1548 so=3128`, i.e. thrashing.
PSI falling (avg300 > avg60 > avg10). Largest consumers are NOT Owner OS: `postgres`
2.31 GB, `chrome` 2.15 GB, `fastnetmon` 1.54 GB single process, `claude` 2.72 GB spread
over 26 processes; `mariadbd` and `ollama` hold 2.3 GB of swap between them while idle.

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

1. **Telegram BotFather token** — a dedicated bot, token into `configs/.env` as
   `TELEGRAM_BOT_TOKEN`; Owner OS then derives the chat id from `getUpdates` and verifies
   with `getChat`. `owner_push` is `Bad Request: chat not found`; 5986 dead letters, 18
   active. This is the SOLE cause of health red — `loop_liveness`, `registry_health` and
   `browser_degraded`-as-a-check are all green, and 5942 of 5944 dead letters are telegram.
2. **Rows 21903 / 24179** — one question per project: is that value live? If yes, rotate at
   the issuing service; `control_plane.db` is a plain file on this host, so scrubbing the
   row is cosmetic beside rotation. If no, nothing is required.
3. **Host memory** — conditional only. Currently recovering; becomes a gate if it reverses
   and processes must be shed.

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

## Next safe step

Nothing open in this workstream. The tab leak is proven, fixed, pushed, live, and now
observed end to end: at least five live `/json/new` creations after the fix, including one
recorded recovery failure, with the page count holding at 8 and zero duplicates.

Full suite green at this HEAD (3128). Both long-running checks finished and are recorded
above.

Two standing conditions, neither caused nor changed by this work:

* delivery is host-load-limited — `cdp_error:WebSocketTimeoutException` is 21 of 46
  attempts since the cleanup, unrelated to the tab leak;
* the Telegram token remains the sole cause of health red (gate 1 below).

If duplicates ever reappear, the page that appears IS the evidence: capture `/json/list`
and the matching `wake_delivery` row before touching anything.
