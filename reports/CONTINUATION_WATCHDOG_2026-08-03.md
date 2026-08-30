# Owner OS — server-side direct-agent continuation watchdog

**Date:** 2026-08-03 · **Branch:** `ai-runtime/156-owner-os-reliable-tmux-agent-com`
**Commit:** `b734208` — feat(commander): server-side direct-agent continuation watchdog with verified submission

## Root cause (2026-08-03 01:07 → 01:54 incident)

An approved agent finished a step around 01:21 and went idle. Its pane held a typed
`continue with the next safe step` that was **never submitted**. The owner had to
intervene at 01:54.

The delivery path (`agent_control._deliver`, used by the orchestrator watcher's
`ac.agent_send(...)` resume, which itself runs inside a `try/except: pass`) reports:

```python
rc_enter, _, _ = _tmux(["send-keys", "-t", resolved, "Enter"])
...
"submitted": rc_enter == 0,          # only means the tmux keystroke COMMAND returned 0
"pane_changed": before != after,     # true just from the pasted text appearing
```

`submitted=true` is claimed when the Enter **keystroke command** succeeds — not when
the agent actually consumed the line. So a typed-but-not-submitted continuation looks
delivered, and nothing re-checks it. Continuation also depended on an hourly external
(ChatGPT) automation, which did not catch the idle agent.

## Fix

New **`core/agent_continuation_watchdog.py`** — an always-on, server-side watchdog
(no external automation). Every 30s it polls live tmux agents; for an **approved
(managed-auto) agent that is idle/waiting with a documented SAFE next step**, it
delivers via the existing multiline-safe `agent_send` path and then **proves** the
submission, requiring ALL five:

| Proof | How |
|---|---|
| `submitted` | Enter keystroke returned 0 |
| `pane_changed` | pane capture differs before/after |
| `prompt_consumed` | the input line no longer holds the continuation (**the check the old path lacked**) |
| `conversation_modified` | Claude's own conversation `.jsonl` mtime advanced |
| `state_transitioned` | agent left `idle` / recent activity changed |

Failure handling: if the prompt was **only typed** (not consumed), it presses Enter
**once more**, safely, and re-verifies. If it still will not submit, it records a
**durable blocker** and emits an owner notification (`agent_continuation_blocked`) —
it never silently claims success.

Safety / correctness invariants:

- **No duplicate agents** — only ever acts on the exact live pane; never spawns.
- **No repeat continuation** — durable idempotency by `(target, conversation_id,
  step_hash)` in `cw_step`; a verified or blocked step is never re-sent, **including
  after a service restart** (persisted in the shared SQLite DB).
- **Allowlist** — only sessions with `mode: auto` in the orchestrator config (plus an
  optional `CONTINUATION_WATCHDOG_SESSIONS` env). All others observed, never actuated.
- **Prohibited actions** — any pending text matching destructive / live / payment /
  credential / publication tokens (`rm`, `drop`, `push`, `publish`, `deploy`, `stripe`,
  `withdraw`, `credential`, `api_key`, `.env`, `mainnet`, `systemctl`, `curl`, `ssh`, …)
  is **never auto-submitted** — it is surfaced to the owner as a blocker.
- **False-idle debounce** — active-exec markers (`esc to interrupt`, `thinking…`,
  spinners) and a required idle-dwell (`IDLE_CONFIRM_SECS`, default 20s across polls)
  prevent acting while Claude is mid-turn/thinking.
- **Owner-dialog boundary** — a real `waiting_owner` permission prompt is left to the
  existing supervisor, not treated as a continuation.
- Pure decision core (`decide`, `verify`, `is_safe_continuation`) + injected
  side-effect `Controller`, so it is fully testable without tmux.

Trading services and exchange credentials are untouched.

## Changed files (commit `b734208`, 5 files, +822)

| File | Change |
|---|---|
| `core/agent_continuation_watchdog.py` | **new** — watchdog: pure `decide`/`verify`/`is_safe_continuation`, `deliver_and_verify` (submit/deliver + one Enter retry), `run_once` sweep, durable `cw_step`/`cw_target`/`cw_health`, `run_loop`, `health()` |
| `api/main.py` | startup hook `_start_continuation_watchdog` (alongside supervisor/orchestrator), gated by `CONTINUATION_WATCHDOG_ENABLED` |
| `api/v1.py` | new read-only `GET /agents/continuation-watchdog/health` |
| `core/agent_orchestrator.py` | `status()` now includes `continuation_watchdog` health |
| `tests/test_agent_continuation_watchdog.py` | **new** — 23 tests |

## Service / timer

- Runs **inside the existing `ai-runtime.service`** (systemd, `/etc/systemd/system/
  ai-runtime.service`) as an asyncio task started at app startup — same model as the
  supervisor and orchestrator loops. No new unit/timer needed; the watchdog is a
  30s in-process loop (`CONTINUATION_WATCHDOG_INTERVAL_SECS`, default 30).
- Env knobs: `CONTINUATION_WATCHDOG_ENABLED` (default on), `_INTERVAL_SECS` (30),
  `_IDLE_CONFIRM_SECS` (20), `_VERIFY_TIMEOUT_SECS` (8), `_DEFAULT_STEP`,
  `_SESSIONS`, `_MAX_PER_CONV` (25).

## Tests

- Focused: `tests/test_agent_continuation_watchdog.py` — **23 passed**:
  - **typed-but-not-submitted repro** (`verify` fails when prompt not consumed;
    `run_once` submits + verifies the exact bug scenario);
  - verify-then-**retry Enter once**-then-succeed, and give-up→**blocker**;
  - **false idle while thinking** (active markers / active state → skip);
  - **idle-dwell** confirmation required;
  - **duplicate prevention** (same step not repeated);
  - **owner gate** (unsafe pending text blocked, never submitted, 0 Enter presses);
  - **dead pane** + non-allowlisted → skip;
  - **recovery after service restart** (fresh controller + same DB → no re-submit);
  - health surface reports last action.
- Full suite (`python -m pytest -q`): **821 passed** (798 prior + 23 new), no regressions.

## Deployment evidence (live)

- Deployed by restarting **`ai-runtime.service` only** (`MainPID 96912`, `active`).
  No trading service or exchange credential touched.
- Startup log: `continuation watchdog started (interval 30s)` at 2026-08-03 01:05:49,
  alongside supervisor + orchestrator.
- Live watchdog health (read from the DB / endpoint), ticking every 30s:
  `{enabled: true, last_run_at: 2026-08-02T23:06:20Z, agents_checked: 0, submitted: 0,
  verified: 0, retried: 0, blocked: 0, errors: 0}` — no eligible managed-auto idle
  agent this tick, and **zero false actions**: `cw_step` empty, `0` `cw-*` commander
  events. (The watchdog is conservative: allowlist + dwell + safe-only.)
- Health route registered: `GET /api/v1/agents/continuation-watchdog/health` → 401
  (auth required, not 404); `agent_orchestrator.status()` now carries
  `continuation_watchdog`.

## Rollback

- Code: `git revert b734208` then `systemctl restart ai-runtime.service`.
- Kill switch without redeploy: set `CONTINUATION_WATCHDOG_ENABLED=0` in
  `/root/ai-dev-runtime/configs/.env` and restart — the loop no-ops; supervisor /
  orchestrator unchanged.
- The `cw_step` / `cw_target` / `cw_health` tables are additive and droppable; no
  existing table or behaviour was altered.

## Known limitations

- Proactive delivery of a documented step into an EMPTY input line is **opt-in**
  (`proactive_continue: true` per session) and capped (`MAX_PER_CONV`); the default
  behaviour is to **submit what the agent already typed**, so the watchdog never
  invents open-ended direction.
- Eligibility is `mode: auto` sessions only; a project must be added to the
  orchestrator config to be actuated.
- The conversation-modified proof relies on Claude's `~/.claude/projects/<slug>`
  history; if history is disabled, the state-transition proof still gates success.
- Live functional submission was proven by the 23 deterministic tests + the live loop
  running with zero false actions; it was **not** exercised against a real user pane
  (no synthetic continuation was injected into a live agent, to avoid disturbing work).

---

# ADDENDUM 2026-08-03 — acceptance gap closed + REAL live acceptance PASS

The first cut was **not accepted**: `arbitrage2-opus:0.0` was idle with a safe typed
continuation but the watchdog reported `agents_checked=0` because the session was not
configured — and health still looked "ok". Fixes below, ending in a real live PASS.

## Additional changed files

| File | Change | Commit |
|---|---|---|
| `config/agent_orchestrator.yaml` | add `arbitrage2-opus` as managed-auto (`mode: auto`) scoped to `/opt/arbitrage2`, `proactive_continue: true`, documented `safe_continuation`; bounded — `advance_phases: false`, no `auto_push`/`service_ops` (pushes/service-ops/credentials stay owner-gated). File-based ⇒ survives restart | `e0ef33a` |
| `core/agent_continuation_watchdog.py` | (1) `health()` flags **misconfiguration** (`status: warning`, `no_eligible_sessions`) when enabled with zero managed-auto sessions, and surfaces `eligible_sessions`/`eligible_count`; (2) retry now uses **`robust_submit`** (clear line + paste + Enter via `agent_send`) instead of a bare Enter that does not land; `VERIFY_TIMEOUT` 8→12s; (3) a **BLOCKED step self-heals** — `should_skip_prior` re-attempts after a cooldown (600s) up to a cap (6), while VERIFIED never repeats | `e0ef33a`, `1e31a45` |
| `tests/test_agent_continuation_watchdog.py` | +8 tests: real-config eligibility (`arbitrage2-opus`), zero-eligible warning, ok-when-eligible, missed-Enter→robust-resubmit recovery, blocked cooldown re-attempt / attempt-cap / verified-permanent | `e0ef33a`, `1e31a45` |

Focused suite: **31 passed**. Full suite: **826 passed**.

## Live root cause of the "missed Enter"

The running watchdog re-reads the config each tick, so it picked up `arbitrage2-opus`
and acted at **01:06:17Z**: a `submit` (bare `send-keys Enter`) on the typed line, then
one retry, then a blocker with `verify = {submitted:true, pane_changed:false,
prompt_consumed:false, conversation_modified:false, state_transitioned:false}` — the
Enter did **not** land. The delivery log shows a human then force-continued the same
agent 17s later via `agent_send` (key `arb2-force-continue-after-watcher-missed-enter`,
`pane_changed:true`) — i.e. the paste+Enter path DOES land. So the fix routes the retry
through `robust_submit` (clear + paste + Enter), and blocked steps become re-attemptable.

## REAL live acceptance — PASS (2026-08-03 01:22:23Z)

Sequence observed on the live `arbitrage2-opus:0.0` pane (bounded read-only monitor):

1. Agent finished a 15m32s turn and went **idle** (empty input line), conv
   `64715514-…`. Idle confirmed across the dwell (ticks [07],[08]).
2. Watchdog (proactive) **delivered** the documented safe step `Continue with the
   fault-matrix extension and replay harness` via the reliable `agent_send` path.
3. **All five proofs true** — `verify = {submitted:true, pane_changed:true,
   prompt_consumed:true, conversation_modified:true, state_transitioned:true, ok:true}`,
   `retried:false`.
4. Pane transitioned into real work: `❯ Continue with the fault-matrix extension and
   replay harness` → `· Osmosing… (8s · thinking)`; conversation mtime advanced
   `01:21:45Z → 01:22:22Z`.

Durable evidence:
- **cw_step**: `verified=1, blocked=0, attempts=3, last_outcome=verified,
  conv=64715514, updated_at=01:22:23Z` (the stale blocker self-healed to verified).
- **cw_health**: `agents_checked=1, submitted=1, verified=1, retried=0, blocked=0,
  errors=0, last_action=continued:arbitrage2-opus:0.0:deliver, status=ok`.
- **Commander event** `agent_continuation_submitted` (dedup `cw-ok:…`) carrying the
  full `verify` block above + `step` + `cwd=/opt/arbitrage2`.
- **Retry-once behaviour** was exercised live at 01:06:17Z (attempts=2 = deliver + one
  retry) and is covered by the `missed-Enter → robust-resubmit` regression test.

## Health misconfiguration surface

`GET /api/v1/agents/continuation-watchdog/health` (and `agent_orchestrator.status()`)
now return `eligible_sessions`, `eligible_count`, and `status` — `warning` +
`no_eligible_sessions` when enabled with zero managed-auto sessions (so a watchdog that
can never act no longer reports healthy). Post-fix live value: `status=ok`,
`eligible_sessions=[arbitrage2-opus, job, seo-audit]`.

## Scope / safety

No trading service, exchange credential, secret, or git history touched; nothing
pushed/published. `arbitrage2-opus` is bounded to `/opt/arbitrage2` analysis+code/tests;
the watchdog refuses any destructive/live/payment/credential/publication text. Commits
`e0ef33a`, `1e31a45` are local only.

## Acceptance verdict: **PASS** — live watchdog detected the idle approved agent,
delivered + verified the safe continuation (all five proofs), and drove
`arbitrage2-opus` into real work, with durable `cw_step`/health/event evidence.
