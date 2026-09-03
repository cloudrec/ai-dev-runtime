# Handoff — the duplicate-tab leak, found and fixed (not yet live)

Concise. Full narrative in Part 83 of `OWNER_OS_WAKE_DOORBELL_CANONICAL_2026-08-30.md`.

## Root cause

`tools/cdp_composer.py` → `recover_wedged_tab()` had two exits for a failed recovery
and they disagreed:

```python
        else:
            _close_target(fresh["id"])   # verification TIMED OUT -> replacement closed
            return None
    except Exception:
        return None                       # verification RAISED   -> replacement KEPT
```

Verification calls `page_responsive` up to fifteen times against a browser that is
already timing out, so raising is the EXPECTED shape of a bad recovery. Every such
recovery leaked one tab. `877edaf` gave `open_chatgpt_page` this identical guard and it
was never back-ported to the function it was copied from.

Measured rate: **one leak in three recoveries**.

## How it was found

Not by reading — by recording. A read-only watcher sampled the CDP page set every 5 s
for an hour, printing only changes plus the companion's journal lines at that moment.

The window ran to completion, 04:50:21 -> 05:45:24, and the tally is final:

```
3 creates, 2 closes, net +1 tab in 55 minutes

04:57:26  + 6ECB4BB5  ...1bb4634f4845    created
04:57:33  - 38CABE02  ...1bb4634f4845    old closed 7 s later      PAIRED
05:10:43  + A22E0A24  ...d26156937c57    created, never closed     LEAKED
  07:10:42 companion: not delivered for event 23651; stays pending
                      (renderer_unresponsive)
(later)   + D99FF1C9  ...de5162d4ac17    created
          - C35582B6  ...de5162d4ac17    old closed                PAIRED
```

Leak rate **1 in 3 recoveries**, and the one that leaked is precisely the one whose
delivery logged `renderer_unresponsive`. Drift of +1 tab per ~55 min at this traffic
level matches the 5 -> 7 growth seen earlier over about an hour, and explains how the
browser reached 12 pages and began refusing deliveries with
`browser_degraded:too_many_pages` on 2026-09-01.

`renderer_unresponsive` is the caller's answer to this function returning `None`,
logged one second before the leaking create.

Four earlier fixes to this file (`404496b`, `877edaf`, `ad705eb`, `b0109bf`) each closed
a real defect and none could have found this one: it leaks only when the browser is
misbehaving, which is exactly when nobody is reading the code.

## Changed files

| file | change |
|---|---|
| `tools/cdp_composer.py` | close `fresh` on the exception path; `fresh` pre-initialised; cleanup itself wrapped so a failing close cannot mask the original error |
| `tests/test_cdp_composer.py` | 3 tests |
| `reports/OWNER_OS_WAKE_DOORBELL_CANONICAL_2026-08-30.md` | Part 83 |

`2 files changed, 87 insertions(+)` in code and tests. Commit `b76fee7`.

The fix closes only the id Chrome just returned, so it cannot touch a bound
conversation or any pre-existing tab, and the old tab is left alone — a failed recovery
now ends with exactly the tabs it started with, on both exits.

## Tests run

```
tests/test_cdp_composer.py                                   77 passed
cdp_composer + wake_bridge + closed_loop_wake + agent_watch
  + chat_registry                                           335 passed
```

The asserting test fails when the cleanup is removed, verified by removing it. Controls
pin that the OLD tab is never closed on this path (a failed recovery must never leave
no ChatGPT tab at all) and that a throwing cleanup still returns `None` rather than
raising into the caller.

## Current evidence state — PRESERVED, do not sweep

```
pages 8, not degraded (limit 12)
  x3  ...d26156937c57   A22E0A24  7959E398  05601838
  x2  ...1bb4634f4845   6ECB4BB5  E5CC3BAF
  x1  ...6f08300ac268 / ...de5162d4ac17 / ...1e4c64cc7431
```

`A22E0A24` is the tab whose creation was recorded at 05:10:43 with no matching close.
It is deliberately still open. The other multiples are residue from before `b0109bf`.

Sweeping these destroys the only recorded baseline this file has ever had for a tab
claim.

## Not live

```
companion PID 896179, running b0109bf-era code
worker_skew   wake_companion, code newer by 8725 s
HEAD b76fee7   origin 0c7c984   (1 unpushed)
```

`tools/cdp_composer.py` is in the companion's watched set, so the fix is inert until a
restart.

## Exact next owner action

1. **Push** `b76fee7`.
2. **Restart** `owner-os-wake-companion` to load the fix.
3. Then, and only then, **re-run the watcher** against this baseline for a comparable
   window. The pass condition is explicit: **creates and closes pair 3-for-3, net drift
   zero**, against the 3/2/+1 recorded above. That is the first tab claim in this ledger
   that can be settled by measurement rather than assertion. Clear the residue only
   after the comparison is made.

   Watcher script: `scratchpad/tabwatch2.sh` (read-only; samples `/json/list` every 5 s,
   prints only changes with the companion's journal lines at that moment).

Unrelated and still open: the Telegram credential decision (Part 81) and the unused 24 h
Windows enrolment code.
