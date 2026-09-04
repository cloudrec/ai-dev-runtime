# Browser tab leak — production proof of both recovery branches

**Date:** 2026-09-04 · **Repo:** `/root/ai-dev-runtime` · **Branch:** `ai-runtime/220-windows-bridge`

Companion `owner-os-wake-companion` PID 2943386, started 19:44:37 CEST, running
`842ded3` + `6a51582` + `42870de`. An automated instruction was received via the Owner OS
API scoping this work; it is not owner sign-off and nothing here was owner-approved.

Follows `OWNER_OS_TAB_ACCRUAL_ROOT_CAUSE_2026-09-04.md`, which diagnosed the first of the
three defects.

## The defect 42870de fixed

`_close_target` waits for the target to leave `/json/list` before it believes a close, so
`False` means the browser ACCEPTED the request and still holds the page. That happens:
observed three times during live cleanups on 2026-09-04, each of those closes completing
on its own a moment AFTER the deadline.

The old-tab close on the SUCCESS path already asked twice for exactly that reason. The
four CLEANUP closes did not — both failure branches of `recover_wedged_tab` and both of
`open_chatgpt_page` asked once and ignored the answer. A recovery that fails verification
closes the replacement it opened, and if that close was merely accepted, the page stayed
open on a bound conversation, counting against `BROWSER_MAX_PAGES`, permanently.

`_reap` closes and, if the browser has not dropped the target yet, asks exactly once more.
All five closes of a tab this module opened now go through it. Stopping at two preserves
the standing rule that a zombie tab is worse to fight than to leave.

This was the third of three defects, and the one `842ded3` did NOT close:

| commit | defect |
|---|---|
| `842ded3` | a close believed on acknowledgement alone |
| `6a51582` | a wedged page handed to the unguarded navigate path |
| `42870de` | the cleanup close never retried |

## Success path — 50 minutes, no leak

```
20:00:11 - 20:50:19   20s samples   persistence threshold 3 samples (~60s)

pages: baseline 8 -> final 8, peak 9
clean replacements: 3        failed recoveries: 0
longest run above baseline:      1 sample (20s)
longest run holding a duplicate: 1 sample (20s)
VERDICT: NO LEAK — every rise resolved within the threshold
```

Seven create-then-close cycles and three replacements. EVERY rise resolved in exactly one
sample — never two, never three — and the count returned to 8 each time.

The classifier matters here. An earlier 50-minute run flagged two "LEAK CANDIDATE" events
that were healthy replacements caught mid-flight, because it treated any single-sample
rise as growth. A create-then-close spans more than one sample, so the test was changed to
require PERSISTENCE: a leak is the count staying above baseline for 3+ consecutive
samples, or a duplicate URL persisting that long, or a final count above baseline. For
contrast, the run that exposed the defect looked nothing like a transient — the duplicate
appeared at 18:16:47 and was still open 42 minutes later, and that window ended one page up.

## Failed-recovery path — live production proof

The branch fires ~14 times a day (14 on 09-04, 10 on 09-03, 7 on 09-02), about one per
100 minutes, and did not occur inside the 50-minute window: 24 deliveries, zero
`renderer_unresponsive`. Rather than call it verified on the strength of the success path,
the branch was forced directly.

Forcing method: `chat.openai.com` redirects to `chatgpt.com`, so a tab opened on the
legacy domain can NEVER match the requested URL — `find_target` fails for a real reason
rather than a stubbed one.

```
target:   https://chat.openai.com/c/e35ee4e7-...       (asserted against every route first)
baseline: 8 pages, degraded=ok

recover_wedged_tab(...) -> None      after 32.5s

tab 3CB5F4AEBA83
  landed on 'https://chatgpt.com/c/e35ee4e7-...'   <- redirected, so never matches
  appeared +1.3s   REMOVED +32.5s   (open 31.2s)

pages 8 -> 8   leftover: none
```

Four properties, against real Chrome and real `/json/close` semantics rather than a stub:

* **failure detected** — `find_target` never matched; the loop ran its full 15 x 2s and
  the branch was entered for the right reason.
* **cleanup performed** — the tab `/json/new` returned was created at +1.3s and removed at
  +32.5s, immediately after the loop exhausted. `_reap` closed it, verified against
  `/json/list`.
* **state restored** — count back to exactly 8, no leftover, no bound route touched,
  `old_target` never closed, nothing typed into any page, no DB write.
* **accounting honest** — returned `None`, so the caller records `renderer_unresponsive`
  rather than a false success. That is precisely the defect: before `42870de` the single
  unretried close could leave the page open while the caller moved on, which is how
  `006DB4FC881E` sat on the `mess` conversation for 42 minutes.

### The attempt that proved nothing, and was excluded

The first injection used `https://chatgpt.com/c/<uuid>` and took the SUCCESS branch in
4.7s: ChatGPT preserves a non-existent conversation id in the address bar, so
`find_target` matched and `page_responsive` answered. No proof of the failure branch. The
tab it created was cleaned up (net change 0) and the attempt was reported as no-proof
rather than counted. Recorded because the next person will reach for that URL first.

## Final state

```
pages 8   headroom 4   reclaimable 0   duplicated []   orphaned []   degraded=ok
routes without a tab: 5/14 — auction, gaika-drop, gaika-video, jobhunter-ai, treasure
                             (dormant auto-discovery routes, no live agent)
```

Both test tabs closed, net change zero. Coverage 5 covered / 5 denied / 0 uncovered, with
the denylist and `SELF_PROJECT` unchanged.

## What this does and does not settle

Settled: both branches of `recover_wedged_tab` are proven in production not to leak a page.

NOT settled: what navigates a VERIFIED tab off its bound conversation to a `WEB:`
placeholder afterwards. `5EAECCDE1973` did exactly that earlier today — created as an
owner-os replacement at 14:59:20, verified on the bound URL, later found on
`.../c/WEB:b535cc94-...` and unresponsive. That is post-verification drift, a different
mechanism from the create-path leak, and this module logs nothing that would name it.
Orphan accumulation is its visible symptom.
