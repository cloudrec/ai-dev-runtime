# Deploy-skew watched only half the companion's code (2026-08-30)

Branch `fix/wake-skew-watched-files`, from deployed `2e4c137`. **Local only: not
pushed, merged, deployed or restarted.**

## How this was found

Investigating the WebSocket timeouts, I noticed
`owner-os-wake-companion.service` started 2026-08-29 20:46:51 CEST — before the
23:40 `ai-runtime.service` restart — so it runs pre-deploy code.

Checking whether that was already detected turned up `worker_skew()`, which exists
for exactly this and whose comment describes the precise incident it was built
for: "after the routing fix went live, the API decided a wake for the gaika-drop
chat while the stale companion delivered it to owner-os ... Same database, two
versions of the truth, wrong chat."

**The mechanism works.** Live check: companion started 18:46:55Z,
`core/wake_bridge.py` mtime 00:44:54 — older, so no skew is reported. That
independently confirms the earlier conclusion that `2e4c137` had no effect on the
companion: it changed `ai_planner`, `job_executor`, `deliver` and
`windows_bridge`, none of which the companion imports.

## The gap

`_WORKER_WATCHED_FILES["wake_companion"]` listed only `wake_bridge.py` and
`wake_routes.py`. But the companion's delivery path is **not** only this module:

| Imported by the companion | Watched before? |
| --- | --- |
| `core/wake_bridge.py` | yes |
| `core/wake_routes.py` | yes |
| `tools/cdp_composer.py` (`submit_phrase`) | **no** |
| `tools/wake_companion.py` (its own entrypoint) | **no** |
| `core/closed_loop_wake.py` | **no** |

`cdp_composer.py` is where the composer selectors, **the latch boundary**,
`page_responsive()`, `recover_wedged_tab()` and the whole post-send verification
loop live — the code that decides whether a wake is delivered and whether that is
believed. A fix there would have changed delivery behaviour while raising **no
skew at all**: precisely the failure this mechanism exists to catch, and the same
shape as the routing incident in its own comment.

The list's comment even says "the wake companion cares about its own delivery
code" — `cdp_composer.py` *is* that code, and it was not listed.

## The fix

Adds `closed_loop_wake.py`, `../tools/cdp_composer.py` and
`../tools/wake_companion.py`. `_module_mtime` joins each entry against `core/`, so
`..` reaches `tools/`; verified resolving. The `agent_orchestrator` list is
untouched.

## Verification

* `tests/test_wake_pipeline_health.py` 36 -> **41 passed**. New tests: the
  companion watches its own delivery code and entrypoint; it still watches the
  bridge modules it already covered; **every** watched path across **all** workers
  exists (a typo would silently contribute mtime 0 and weaken the alarm rather
  than fail); a change to the composer drives the newest-mtime; the orchestrator
  list is unchanged.
* Gate: **204 passed** (`wake_pipeline_health`, `wake_bridge`,
  `agent_orchestrator`, `wake_delivery_verification`, `zero_human_ping`).
* Mutation: reverting the list fails the delivery-code test; a typo'd path fails
  the existence test.

**A weak test caught and fixed.** The first version of the composer test compared
real mtimes, which is vacuous in a fresh checkout where every file shares the
checkout timestamp — it passed even with the fix reverted. Rewritten to make only
the composer distinctly newer and require `_module_mtime` to return it; it now
kills that mutant.

## Rollback

Never deployed. One dict entry in `core/wake_bridge.py`; no schema, config,
credential or protocol change.

## Note for whoever deploys this

This change makes skew *more* sensitive for the companion, which is the point.
Once `tools/cdp_composer.py` or `tools/wake_companion.py` is newer than the
companion's start time, `worker_skew()` will report it until that service is
restarted — and it is currently stale by that measure only if those files change.
Restarting `owner-os-wake-companion.service` is a separate owner gate; the earlier
deploy restarted `ai-runtime.service` alone.
