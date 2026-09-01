# Owner OS — native-first gap audit

An automated instruction was received directing that no further Owner OS mechanism
be invented or extended where the installed Claude Code already provides the
capability natively, and that an audit precede any further code change. This is
that audit. It is evidence-based: every claim below is a measurement against the
installed build and the live event log, not an inference from documentation.

**Installed build:** Claude Code `2.1.257`.
**Window for all counts:** the 24 h ending 2026-09-01T22:0x UTC.

---

## 1. Which native signals actually fire here

Eight hook events are registered in `/root/.claude/settings.json`; six route to
`hooks/owneros_hook.py`. What actually arrived in 24 h:

| Native hook | Received | Notes |
|---|---:|---|
| `Stop` | 639 | every turn boundary |
| `Notification` | 303 | **100% `idle_prompt`** |
| `StopFailure` | 136 | |
| `SubagentStop` | 124 | |
| `TaskCompleted` | **0** | registered, never fires on this host |
| `TeammateIdle` | **0** | registered, never fires on this host |

And of the `Notification` subtypes Owner OS maps, **only `idle_prompt` ever
arrives** — `agent_needs_input` and `agent_completed` fired zero times.

This single table overturns the most attractive version of "replace the custom
code with the native signal". The native lifecycle surface on this host emits
four kinds of fact, not the six Owner OS maps, and none of the three
*actionable* classes an owner is woken for.

## 2. Where the facts actually come from today

Events by source (24 h): `claude_hook` **1201**, notifier 316, `agent_watch` 253,
work_evidence 128, closed_loop_wake 123, waiting_transitions 98, runtime_jobs 69,
delivery 45, stall_doctor 30, native_supervisor 12, discovery 12, controller 5.

Native hooks already produce **52%** of all events. Split by the fact itself:

| Fact | Native hook | Scraped/derived |
|---|---:|---:|
| `agent_turn_stopped` | 941 | 0 |
| `agent_subagent_stopped` | 124 | 0 |
| `agent_process_failed` | 136 | 4 |
| `agent_waiting_input` | **0** | **104** |
| `task_completed` | **0** | **19** |
| `work_stopped_incomplete` | **0** | **202** |

The migration to native already happened for turn boundaries and crashes. It has
not happened for the actionable classes **because the native signals for them do
not fire**.

## 3. The capability table

| Native capability | Owner OS mechanism | Verdict | Why |
|---|---|---|---|
| `Stop` / `SubagentStop` hooks | turn-boundary inference in `agent_watch` | **Already replaced** | 1 065 native vs 0 scraped |
| `StopFailure` hook | `crashed` class from pane text | **Replaced; keep pane path as fallback** | 136 native vs 4 scraped; the 4 are panes that vanished with no hook to fire |
| Hook `background_tasks` / `session_crons` | `_armed_external_wait` | **Keep — already native-first** | reads those native fields directly; a structural fact, not prose. The model to copy |
| Hook `session_id` | conversation-id inferred from cwd | **REPLACE** | native is authoritative per process; the cwd guess is the direct cause of the Part 52 two-identity bug |
| `claude agents --json` → `status: busy/idle` | `agent_watch.classify()` pane scraping | **REPLACE for liveness/state** | native states it; scraping infers it. See §4 |
| `claude agents --json` → inventory | `agent_control.agent_list()` tmux enumeration | **Keep both** | native carries no tmux target, which is required to send keys. Use native as a cross-check, not a replacement |
| `Notification/agent_needs_input` | `owner_prompt` class | **Keep custom** | native subtype fired 0 of 303 times |
| `TaskCompleted` hook | `completed` class | **Keep custom** | fired 0 times |
| `TeammateIdle` hook | — | **Deprecate the mapping** | fired 0 times; dead branch, but costs nothing and is a free fallback if teammates are ever used |
| `SendMessage` / native agent messaging | `cdp_composer` browser typing | **Keep — not a duplicate** | Owner OS wakes **ChatGPT conversations in a browser**, not Claude sessions. Different endpoint; native cannot address it |
| `claude --resume`, session persistence | `session_recovery.py` | **Assess separately** | out of scope for this pass; not measured, so not judged |

## 4. The one finding that changes an open problem

`claude agents --json` is documented as printing "active sessions (interactive
and background) as a JSON array" and does exactly that — including
`sessionId`, `pid`, `cwd`, `name` and a `status` of `busy` / `idle` / `blocked`.

Checked against Owner OS's own inventory: both see 10 live agents. Native
reported `busy` for six and `idle` for two of the panes Owner OS tracks. Two
pids disagreed in each direction (`email:0.0` 1692437 vs native 1695585,
`hostsecure:0.0` 3260897 vs native 3262329) — the same agents under a wrapper
pid, which is itself worth reconciling.

This matters beyond tidiness. **Part 53 closed with an open problem stated in
these words:** "an event was recorded" is a poor proxy for "the agent is making
progress", and a single turn that runs half an hour emits nothing while it works;
fixing it "means finding a positive liveness signal rather than subtracting more
exceptions". `status: busy` **is** that positive liveness signal, and it was
available the whole time. The closed-loop watchdog subtracts exceptions
(Parts 52, 53) precisely because it never had it.

Cost measured: 0.80 s / 1.26 s / 1.99 s across three calls. Affordable against a
20 s companion tick, but not free, and it must fail open — an unreadable native
listing can never be allowed to mean "not alive".

## 5. What stays custom, and why

Nothing native covers these, and this audit recommends no reduction in any of
them: durable audit and idempotency (`wake_audit`, `wake_send`, `wake_delivery`,
the invalid-alert overlay), safety and approval gates (`native_supervisor`
denylist, `supervisor_self_reference`, owner gates), cross-project routing
(`wake_routes`), service persistence and fail-closed recovery, and the fallback
paths for signals that do not fire.

## 6. Recommended sequence — not yet executed

1. **Adopt `sessionId` from the hook payload as the agent's identity**, retiring
   the cwd→conversation guess. Directly retires the Part 52 defect class rather
   than widening the identity set as `8aba07f` had to.
2. **Consult `claude agents --json status` in `closed_loop_wake`** as a positive
   liveness signal before re-waking or escalating, failing open. This is the
   deletion the instruction asks for: it would let the accumulated
   `_resolution_reason` exceptions shrink instead of grow.
3. **Cross-check the inventory** and reconcile the wrapper-pid mismatch.
4. Leave `TeammateIdle` / `TaskCompleted` mappings in place as zero-cost
   fallbacks; deleting them would trade a real fallback for nothing.

Items 1 and 2 change how live supervision decides an agent is alive. That is
safety-relevant classification, so it is presented here for a decision rather
than executed inside the same turn that discovered it.

## 7. Correction to the standing ledger

`99b6c2f` (the provider-limit hook fix) was previously reported as inert with the
rest of the post-Part-49 work. That was wrong: a hook runs as a fresh process
from the checkout on every invocation, so it took effect immediately and needs no
restart. Verified live — `agent_process_failed` carrying the quota banner ran at
131 per 6 h before the commit and **zero** after it, with the only two events
since being genuine (`Prompt is too long`, `API Error: Connection lost`).

The reclassified `agent_externally_blocked` events are recorded durably and
carry `pushed: False` — confirmed by direct probe against an isolated database —
so the quota pause remains visible in the ledger without waking anyone, which is
what Part 49 specified. No quota banner has arrived since, so the positive path
is correct-by-construction but not yet exercised live.
