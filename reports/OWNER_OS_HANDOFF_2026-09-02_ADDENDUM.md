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

## 2. All three tab fixes are now LIVE

Superseded. Section 2 previously recorded that two of the three were inert; the
owner authorised the restart and they are loaded.

```
companion restarted  2026-09-02 08:19:00 CEST   PID 3717100
404496b   2026-09-01 22:53   LIVE
877edaf   2026-09-02 06:25   LIVE
ad705eb   2026-09-02 07:53   LIVE
worker_skew()        []
```

Verified by introspecting the module the running process imports, not by commit
date alone: `open_chatgpt_page` carries the `browser_degraded()` guard and closes
its unverified tab; `recover_wedged_tab` returns the verified target and retries
the close exactly once. Journal clean, 0 errors since restart. 19 `wake_send`
rows in the first 10 minutes, browser at 1 page, 0 bare roots.

Backup and rollback: `backups/predeploy_companion_tabguards_20260902T061835Z/`
(control_plane.db integrity ok, `ROLLBACK.md`).

The caution in the old section 2 still holds in one respect: a healthy page count
now is consistent with the guards working but does not prove it. Proof needs a
wedge or a `/json/new` failure to actually occur while they are loaded.

## Remaining safe work

None identified. Both items the handoff listed are now closed or blocked on a gate.

## Owner gates

* ~~Restart the companion~~ — done 2026-09-02 08:19, all three tab fixes live.
* **Push** — 3 commits unpushed (`135123d`, `ad705eb`, and this addendum's commit).
* Telegram Start · `canary_agent_selection` · the three shared route keys ·
  the outstanding Windows enrollment code — all unchanged.
