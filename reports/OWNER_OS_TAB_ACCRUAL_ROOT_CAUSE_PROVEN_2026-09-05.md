# Duplicate tab accrual — root cause proven

2026-09-05. Investigation opened from `OWNER_OS_HANDOFF_2026-09-05.md`, "Browser — the top
OPEN technical issue". Driven by an automated Owner OS API instruction; no owner sign-off
is recorded here.

## Verdict

`/json/new` could time out **after** Chrome had already created the tab, and the code then
threw away the only chance to close it. One leaked page per failed recovery, permanently,
on the bound conversation.

The handoff's standing hypothesis — a verified replacement followed by both `_reap`
attempts failing on the OLD tab — is **refuted** by direct measurement against the live
browser.

## The defect

`tools/cdp_composer.py`, both creation paths, identically:

```python
try:
    fresh = _http(f"/json/new?...", method="PUT")          # _http timeout was 8 s, fixed
except Exception:  # noqa: BLE001 — pre-111 Chrome used GET here
    fresh = _http(f"/json/new?...")                        # Chrome 151 answers 405
if not fresh.get("id"):
    return None
```

1. `/json/new` is the only browser-level DevTools call that does real work: Chrome spawns a
   renderer and starts the navigation before it answers. Every other call is a lookup
   answered from memory in milliseconds. All of them shared one hard 8 s ceiling in `_http`.
2. When the answer missed that ceiling, **the tab existed anyway** — the timeout was on the
   answer, not on the action.
3. The `except` then re-issued the create as a GET. Chrome 151 rejects it:
   `HTTP Error 405: Using unsafe HTTP verb GET to invoke /json/new. This action supports
   only PUT verb.`
4. That `HTTPError` escaped the inner `try` into the function's outer handler, where `fresh`
   was still `None`. Its cleanup guard is `if fresh and fresh.get("id"): _reap(...)`, so
   **nothing was closed**.
5. `recover_wedged_tab` returned `None`, the caller recorded `renderer_unresponsive`, and the
   orphan finished loading the bound conversation a few seconds later — a duplicate on a
   ROUTED conversation, with no root URL and no `WEB:` placeholder to give it away. That is
   exactly the signature the handoff describes and exactly why no orphan hunt ever found it.

## Evidence

Measured against the live browser and `control_plane.db` at ~06:00–08:10Z. Read-only except
one `about:blank` tab created and closed as a controlled transport test (page count returned
to 13; no routed conversation touched).

**Transport.** `Chrome/151.0.7922.75`. One `PUT /json/new` at the live 13-page state took
**4.56 s** — against an 8 s ceiling, on a host that this session recorded at load 29 with
20 GB of swap in use. `GET /json/new` returns `405` and creates nothing. `/json/close/<id>`
returns HTTP 200 `Target is closing` and the page leaves `/json/list` in **under 0.5 s** —
so `_close_target` and `_reap` work correctly against this browser, which is what rules the
old hypothesis out.

**Count.** Cleanup ended 03:46Z at 8 pages (first non-degraded attempt, `wake_delivery`
11117). The next `too_many_pages:13` refusal is 11195 at 05:37:39Z. Five pages accrued in
that window, and there are exactly five failed recoveries in it:

| leaked page (conversation) | failed recovery | recorded |
|---|---|---|
| e672 ×2 | 11163, 11194 hostsecure | `renderer_unresponsive` |
| 1648 ×1 | 11137 mess | `renderer_unresponsive` |
| 0690 ×1 | 11185 seo | `renderer_unresponsive` |
| e63a ×1 | 11151 / 11181 payment-orchestrator | `assistant_generating_wedged` recovery |

Five extras against five failures, matched conversation by conversation.

**Timing.** `performance.timeOrigin` read from each live tab gives the document's load time.
For every leak that can still be measured, the document was created **7–9 s** before its own
failure record — the 8 s timeout plus an immediate 405:

```
F50ACC3FC28A  e672  created 05:36:22   ->  11194 renderer_unresponsive 05:36:31  (+9 s)
416895EB5762  0690  created 05:17:09   ->  11185 renderer_unresponsive 05:17:16  (+7 s)
F3FF276A6CC9  1648  created 00:13:25   ->  10925 renderer_unresponsive 00:13:34  (+9 s)
```

This also excludes the verification-timeout branch, which cannot return in under 30 s
(15 iterations × `sleep(2)`), and the success branch, which returns a tab rather than `None`.
Only an exception escaping the loop exits that fast, and the 405 is the exception.

## The fix

`_create_tab()` — one function, now the literal single choke point for creating tabs, with
both callers (`recover_wedged_tab`, `open_chatgpt_page`) routed through it. Three parts, all
three needed:

* **A budget that fits the call.** `CDP_NEW_TAB_SECS` (default 30 s) for `/json/new`;
  `CDP_HTTP_SECS` (8 s) stays for every lookup. `_http` takes a per-call `timeout`.
* **The GET fallback fires only on a verb refusal** (405/501). A timeout must never re-issue
  a create — on the pre-111 Chrome that fallback exists for, the second call would open a
  SECOND tab.
* **A create that fails for any reason sweeps for the tab it may have made anyway.**
  `_sweep_unnamed_tab()` closes a page that (a) was not in the snapshot taken immediately
  before the create and (b) sits on a URL that create could have produced — the requested
  conversation, or the blank/root it occupies until that navigation lands. A page open
  before the create is never a candidate, so no bound conversation and no tab of the owner's
  can be caught. If the before-snapshot could not be READ, the sweep closes nothing rather
  than guess. Retried three times, because the create timed out precisely because the
  browser was slow, so the tab may appear a moment after we gave up on it.

## Tests

8 new tests in `tests/test_cdp_composer.py`. Each was confirmed by removal — with
`tools/cdp_composer.py` reverted to HEAD they all fail, the central one as
`assert [] == ['ORPHAN']`: no close was even attempted.

## Not done, and why

* **Nothing was cleaned up.** The 13 pages and the 5 duplicates are preserved as evidence.
  Delivery stays refused at `too_many_pages:13` until a guarded cleanup is run, which has
  been an owner-typed action every time.
* **The fix is not live.** `ai-runtime` and the wake companion were not restarted, per the
  task's constraints, so the running companion still carries the defect and will keep
  leaking one page per failed recovery. It reaches the running process only on the next
  companion restart.
