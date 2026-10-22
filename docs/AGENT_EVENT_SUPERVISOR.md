# Agent event supervisor — design, deployment and enable instructions

Module: `core/agent_event_supervisor.py` · owner CLI: `tools/agent_event_supervisor.py` ·
API: `GET /api/v1/agents/event-supervisor/status`, `GET /api/v1/agents/event-supervisor/history`.

**Status at commit time: NOT deployed, NOT enabled.** The loop is started by `api/main.py` on
service start but every tick is a no-op until `AGENT_EVENT_SUPERVISOR_ENABLED=1`.

## What it does

```
agent event (control-plane `event` table)
   │  work_stopped_incomplete · task_completed/agent_completed · agent_waiting_input ·
   │  agent_prompt_needs_response · agent_process_failed/agent_dead/agent_crash_loop ·
   │  wake_loop_no_progress/agent_no_progress · owner_decision_required/owner_gate_opened
   │  (older than MAX_EVENT_AGE_SECS at ingest → ledgered "ignored_stale", never acted on)
   ▼
VERIFYING  (episode opened; later events fold in, strongest kind wins)
   │  identity pinned: pane, tmux session, cwd, conversation id, transcript mtime
   │  wake: bridge decision reused (cto.emit already decided it) — never a 2nd wake
   │  wake state aggregated over EVERY episode event from wake_audit/submitted/delivery:
   │     delivered (verified delivery row only) → settle window → verify
   │     not_sent (never fired / retired before firing) → verify
   │     pending past timeout · fired-unconfirmed · acked-without-delivery · wake error
   │     → OWNER_NEEDED(wake_delivery_ambiguous)                         (fail closed)
   ▼
verify from FRESH evidence: agent_status pane state+tail, conversation id/mtime, git HEAD/dirty
   ├─ Claude working / background shell·monitor·subagent ............ WORKING (observe)
   ├─ background work + stall event ................................. OWNER_NEEDED
   ├─ dead / exited / vanished after failure event .................. FAILED
   ├─ dialog, prompt event, owner-decision event, owner-gate text,
   │  staged composer text (waiting_input) ........................... OWNER_NEEDED
   ├─ transcript written since the event (someone acted) ............ WORKING
   ├─ completion event, at rest, no failure markers, clean git tree . DONE
   ├─ completion with dirty tree / no git evidence .................. OWNER_NEEDED
   └─ incomplete/waiting/stalled (or completion with failure markers):
        eligible session · same conversation+cwd+session (missing = mismatch; cwd shared
        by two agent panes = unprovable) · no agent_send into the pane since the event ·
        no watchdog submit within the guard · cooldown + budget OK
        → lease agent:<target> (held elsewhere → stay VERIFYING, retry next tick)
        → re-judge under the lease → durable latch → re-check pause
        → ONE continuation via the watchdog's deliver_and_verify (5 proofs)
        → verified: WORKING   | not verified: OWNER_NEEDED (never retried)
PAUSED: owner CLI only; events still ledgered; nothing woken/verified/continued; no replay.
        Every sweep write is `WHERE paused=0` in SQL, so a pause mid-sweep always wins.
Sweeps are serialised across processes/workers by lease `agent_event_supervisor:sweep`; an
unfinished latch at sweep start = died mid-send → counted as a continuation + OWNER_NEEDED.
```

The woken assistant is asked to *review* continuable episodes
(`requested_action=review_supervisor_continues_if_safe` on the status API); the supervisor
alone performs the single continuation. A wake that never fired (bridge disabled, cooldown)
does not block the supervisor — that is intentional: the supervisor must still work when the
bridge is off.

**Known residual risks** (documented, not eliminated):
- An assistant that types into the pane via a path that writes no `deliveries` row AND does
  not advance the transcript before the supervisor's send could still overlap one send.
  `agent_send` refuses mid-turn sends, which bounds this to sequential, not interleaved, input.
- Gate detection is lexical (deny-by-default regexes); a false positive costs an owner glance.

Owner-gate categories (any match → OWNER_NEEDED, never continued): `prod_release`,
`money_spend`, `credentials_secrets`, `captcha_antibot`, `paid_proxy_vpn`, `irreversible`,
`product_decision` (decision phrasing or a trailing question other than "shall I continue?").

OWNER_NEEDED emits one `agent_supervisor_owner_needed` event (severity high,
owner_action_required, dedup 6h per target/event/reason). The supervisor never ingests its
own event types. Set `AGENT_EVENT_SUPERVISOR_EMIT=0` to keep OWNER_NEEDED ledger-only.

Internal stall: an agent tracked WORKING that is at rest with no pane/transcript change for
`STALL_SECS` (background work: 2×) emits `agent_no_progress` (severity high, `push=False` →
wake-eligible, no Telegram; one per stall window) which then travels the normal ingest →
wake → verify path.

## Persistence (control_plane.db, created on first use, additive only)

`aes_agent` (per target: project, session, cwd, conversation_id, pinned_conv_mtime,
episode_start_ts, state, last_event_id/key/type,
last_check_at, last_progress_at, last_verified_result, last_evidence, next_action,
owner_gate_reason, wake_*, continuation_count, continuation_window_start, last_continue_ts,
paused*), `aes_event` (dedup ledger, PK event key), `aes_cursor`, `aes_wake` (requested
action + evidence per wake), `aes_continuation` (exactly-once latch), `aes_transition` (audit).

## Tunables (env; defaults)

| var | default | meaning |
|---|---|---|
| `AGENT_EVENT_SUPERVISOR_ENABLED` | `0` | master switch, read every tick |
| `AGENT_EVENT_SUPERVISOR_INTERVAL_SECS` | `60` | loop period |
| `AGENT_EVENT_SUPERVISOR_VERIFY_SETTLE_SECS` | `90` | wait after event / after delivery before judging |
| `AGENT_EVENT_SUPERVISOR_WAKE_CONFIRM_SECS` | `900` | unresolved wake → ambiguous → OWNER_NEEDED |
| `AGENT_EVENT_SUPERVISOR_WAKE_COOLDOWN_SECS` | `600` | per-target wake floor |
| `AGENT_EVENT_SUPERVISOR_CONTINUE_COOLDOWN_SECS` | `600` | per-target continuation floor |
| `AGENT_EVENT_SUPERVISOR_MAX_CONTINUATIONS` | `3` | per rolling window, then OWNER_NEEDED |
| `AGENT_EVENT_SUPERVISOR_CONTINUATION_WINDOW_SECS` | `3600` | window |
| `AGENT_EVENT_SUPERVISOR_STALL_SECS` | `1800` | internal no-progress threshold |
| `AGENT_EVENT_SUPERVISOR_FOREIGN_GUARD_SECS` | `600` | recent watchdog submission blocks ours |
| `AGENT_EVENT_SUPERVISOR_STEP` | `continue with the next safe step` | must pass `is_safe_continuation` |
| `AGENT_EVENT_SUPERVISOR_REPLAY` | `0` | `1` = process history on first start (don't) |
| `AGENT_EVENT_SUPERVISOR_EMIT` | `1` | emit owner-needed events |
| `AGENT_EVENT_SUPERVISOR_MAX_EVENT_AGE_SECS` | `3600` | older events are history, not acted on |
| `AGENT_EVENT_SUPERVISOR_SWEEP_LEASE_SECS` | `300` | cross-worker sweep lease TTL |

Only sessions in the continuation-watchdog allowlist (orchestrator `mode: auto` sessions +
`CONTINUATION_WATCHDOG_SESSIONS`) are ever continued; all others go OWNER_NEEDED.

## Deployment / enable — requires separate owner approval

These steps change the live service and are NOT performed by the implementing task.

1. **Baseline + backup**
   ```bash
   cd /root/ai-dev-runtime && git status --short && git log --oneline -1
   systemctl status ai-runtime.service --no-pager | head -5
   sqlite3 control_plane.db ".backup backups/control_plane.pre-aes.$(date +%F-%H%M).db"
   ```
2. **Merge** `ai-runtime/agent-event-supervisor-clean` into the deployed line (fast-forward
   or merge onto `main`; the branch is cut from `origin/main` 0fd3353). Run
   `pytest tests/test_agent_event_supervisor.py tests/test_agent_event_supervisor_integration.py`.
3. **Restart, still disabled** (picks up new code; supervisor tick is a no-op):
   `systemctl restart ai-runtime.service`; verify
   `GET /api/v1/agents/event-supervisor/status` → `{"enabled": false, ...}`.
4. **Canary enable** — add to `configs/.env`, scoped to a single canary session through the
   watchdog allowlist (`CONTINUATION_WATCHDOG_SESSIONS=<canary>` if the canary is not already
   `mode: auto`), then:
   ```
   AGENT_EVENT_SUPERVISOR_ENABLED=1
   AGENT_EVENT_SUPERVISOR_MAX_CONTINUATIONS=1
   ```
   restart; first tick initialises the cursor to the current bus head (no history replay).
5. **Live verification** (required before claiming live): produce one
   `work_stopped_incomplete` on the canary; confirm on the status API the sequence
   VERIFYING → (wake delivered) → WORKING with `last_verified_result=continuation_verified`
   and exactly one `aes_continuation` row; confirm a gated agent shows OWNER_NEEDED with no
   continuation row; `python tools/agent_event_supervisor.py history <target>`.
6. **Rollback**: set `AGENT_EVENT_SUPERVISOR_ENABLED=0` (takes effect next tick, no restart);
   full rollback = revert the merge + restart. The `aes_*` tables are additive and can stay.

Pause a target at any time: `python tools/agent_event_supervisor.py pause <target> --by owner`;
resume only with `unpause <target> --by owner`.

## Test-environment note (pre-existing, not changed here)

`tools/wake_companion.py` and `tools/cdp_composer.py` hard-code
`sys.path.insert(0, "/root/ai-dev-runtime")`. In any checkout other than the live tree, their
tests therefore import the LIVE tree's modules, which is the likely cause of the 25
cdp_composer / wake_delivery_verification / wake_assistant_proof failures that are present
on clean `origin/main` (0fd3353) in a worktree. The supervisor's API test loads `api.v1` from
its own checkout explicitly so it is immune to that pollution.
