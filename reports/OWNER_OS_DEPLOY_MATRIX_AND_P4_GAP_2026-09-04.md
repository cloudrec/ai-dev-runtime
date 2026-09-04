# Deployment matrix for the 6 local commits, and the P4 evidence gap

**Date:** 2026-09-04 · `/root/ai-dev-runtime` · branch `ai-runtime/220-windows-bridge`

An automated instruction was received via the Owner OS API; it is not owner sign-off.
Nothing here was pushed, restarted, or deployed. The zero-ping goal remains UNRESOLVED.

## Deployment matrix

Running: `ai-runtime` PID 2690604 (up 2026-09-02 05:25:28) · companion PID 2943386
(up 2026-09-04 19:44:37). All three code commits landed AFTER the companion started.

| commit | files | status |
|---|---|---|
| `aaf1bd4` | report | report-only |
| `0988577` | `hooks/owneros_hook.py` | **ALREADY LIVE** |
| `0988577` | `core/agent_control.py` | live on the hook path only |
| `2288f7c` | `core/agent_watch.py`, `core/stall_doctor.py` | **needs companion restart** |
| `1f4581d` | report | report-only |
| `81778e6` | `core/os_task_queue.py` | **needs restart** (companion + ai-runtime) |
| `c1a3ab5` | report | report-only |

The hook is not a long-running import. `~/.claude/settings.json` invokes
`venv/bin/python hooks/owneros_hook.py` as a FRESH SUBPROCESS per hook event, so it
reads the file from disk every time — 86 hook-emitted events since `0988577` were
already handled by the fixed code. `core/agent_control.redact` is live in that
subprocess for the same reason, and stale inside every long-running process.

Everything else is stale, and there is live proof: event 30381 (20:29:47Z) carries
`pending: "push it"` UNREDACTED — emitted by `stall_doctor` inside the pre-`2288f7c`
companion. Harmless in this instance; the mechanism is exactly what the commit fixes.

Suites re-verified green on the current tree (201 passed: `test_agent_control`,
`test_owneros_hook`, `test_os_task_queue` — the three covering all four changed
modules). The wider six-suite run (420 passed) was green on this same tree and was not
repeated, because nothing has changed since.

No local correctness or observability defect was found this pass. Two candidates were
investigated and both are benign: a notification at `attempts>=5` still in `failed` was
dead-lettered on the very next drain (`pending_notifications` selects
`state IN ('pending','failed')` with no attempts filter, so nothing can stick), and four
notifications with no source event carry `event_id=0`, a bring-up sentinel — no event
has ever been pruned (oldest surviving id is 1).

## The P4 evidence gap, exactly

PROVEN — the supervisor issues continuations, unprompted, to existing agents:

```
20:24:45  event 30369  agent_turn_stopped, idle_prompt ("waiting for your input")
20:24:53  native-supervisor: continued security-demo:0.0 from event 30369
                             agent_created=False, continued_same_agent, ok=1
browser wakes that route:  20:17:09 FAILED · 20:22:39 FAILED · 20:28:26 delivered
```

No chat message landed in that window. Five further continuations came from the idle
sweep (`from event 0`), which has no triggering event at all. 12 continuations in 3h,
every one on an EXISTING agent.

NOT PROVEN — that a natively continued agent then did work.

The cause is structural, not a bug. `closed_loop_wake.register_delivery` tracks "a wake
that a companion DELIVERY just confirmed landed (a real ChatGPT user turn)". A native
continuation never passes through companion delivery, so it is never registered, and no
`closed-loop-watch: deregistered ... ` line references any native-continuation event id
— checked for all eight event-triggered continuations. Silence afterwards is consistent
with an agent working and is not evidence that it is.

**Zero-ping DELIVERY is proven. Zero-ping EFFECTIVENESS is not.**

## The minimal owner decision

One decision unlocks it: **name a single canary target.** The handoff records the canary
gate as declined — "no canary selected; P4 verified-continuation remains deferred" — and
that is the only input still missing. No code, route, policy or credential change is
required to make the decision; implementation stays deferred until it is made.

## Fail-closed canary plan (no ChatGPT delivery accounting)

Designed, NOT implemented — P4 is owner-deferred.

Evidence source is the native Stop hook alone, which is already live and completely
independent of the browser:

1. **One** owner-named canary target, denylist-exempt, low value. No new agents.
2. At continuation, record `(target, event_id, t0)` — the supervisor already logs the
   `nativesup:<event_id>` key, so this is a ledger row, not new machinery.
3. **Effect = a NEW `agent_turn_stopped` hook event for that target with a digest
   differing from the one at `t0`, arriving after `t0`.** A turn boundary is proof the
   agent ran; the digest change is proof it is not the same stalled frame.
4. **Attribution guard:** if ANY `wake_delivery` to that target's route landed between
   `t0` and the observed turn, record `unattributable` and discard the sample. ChatGPT
   delivery can then never be credited to the supervisor.
5. **Fail closed:** no qualifying turn within N minutes records
   `continuation_unverified`. Absence of evidence is recorded as failure, never as
   silence, and never as success.
6. Verdict requires k consecutive attributable successes; any `unverified` resets it.

This proves stop -> continuation -> subsequent work end to end without reading a chat,
without trusting delivery accounting, and without a browser in the path.

## Gates still standing

Push (6 commits) · companion restart · `ai-runtime` restart · Telegram BotFather token ·
rows 21903/24179 · canary selection. The unsent `push it` draft is preserved, unsubmitted.
