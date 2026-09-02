# Handoff addendum — 2026-09-02, after `ad705eb`

Corrects two things in `OWNER_OS_HANDOFF_2026-09-02.md` that are now stale.

## 1. The `recover_wedged_tab` item is DONE

The handoff lists it under "remaining safe work". It is closed by **`ad705eb`**.

Finding was sharper than a leftover tab: `_close_target` swallows failures by
design, but the success path then did `return find_target(conversation_url)` — a
re-scan returning the FIRST url match. With the old tab still open because its
close failed, that match could be the old WEDGED renderer, so a recovery reporting
success handed back the tab it had just replaced. Now returns the verified `t`, and
retries the close exactly once (pinned at one — this module refuses to fight zombie
tabs).

4 tests, 2 fail when reverted. Full gate at commit: 2 995 passed.

## 2. Two of the three tab fixes are NOT live

This is the part worth carrying forward.

```
companion started   2026-09-02 06:23:33 CEST
404496b             2026-09-01 22:53   LIVE
877edaf             2026-09-02 06:25   NOT live  (2 min after the restart)
ad705eb             2026-09-02 07:53   NOT live
worker_skew()       wake_companion, code 4 593 s newer than the running process
```

So the current healthy browser state — 1 page, 0 bare roots, 5 of 6 deliveries
succeeding, one wedge absorbed, zero `too_many_pages` — reflects **`404496b` plus
the manual tab cleanup at ~05:57Z only**. It is not evidence that `877edaf` or
`ad705eb` work in production. Do not read it as such.

Activating them needs a `owner-os-wake-companion` restart, which has been an owner
decision every time in this session.

## Remaining safe work

None identified. Both items the handoff listed are now closed or blocked on a gate.

## Owner gates

* **Restart the companion** — activates `877edaf` + `ad705eb`; until then `worker_skew()`
  stays non-empty and the two newer tab guards are inert.
* **Push** — 3 commits unpushed (`135123d`, `ad705eb`, and this addendum's commit).
* Telegram Start · `canary_agent_selection` · the three shared route keys ·
  the outstanding Windows enrollment code — all unchanged.
