# ACTUATOR BLIND-PANE GUARD + DELIVERY ATTRIBUTION

**2026-08-04.** Two items: the deferred actuator-layer unobservable-pane guard, and the
investigation of the "unattributed deliveries writer". Internal work only: no deploy, no
service restart, no live-pane contact, no autopilot activation, no allowlist change, no
destructive / live / payment / credential / publication action, nothing pushed. Read-only
inspection of the caller side (container `docker exec` greps, no writes anywhere).

---

# PART 1 — Actuator-layer blind-pane guard (closes the M2 deferral)

## Why it was deferred, and what changed

M2 was closed in `cw.decide` and `ap.evaluate`, but `actuate()` itself stayed fail-OPEN: a
direct call on a pane tmux could not read would paste blind. The first attempt to refuse
on "empty tail" turned **15 established clean-pane contracts** into refusals, because a
failed `capture-pane` and a genuinely blank pane both produced `tail == ""` —
indistinguishable by inference.

The fix makes the failure a **fact**, not an inference:

| File | Change |
|---|---|
| `core/agent_control.py:831-839` | new `pane_capture(target, lines) -> (capture_ok, tail)`; `_pane_tail` now delegates to it, so its legacy shape (`""` on failure) is preserved for every existing caller |
| `core/agent_continuation_watchdog.py:439-449` | `Controller.snapshot` returns `capture_ok` alongside tail/pending/state |
| `core/control_plane/actuator.py:204-226` | guard 3b3: refuse when `capture_ok is False` (`why=capture_failed`), or when the snapshot carries **no observation at all** — no capture flag, no tail, no pending, no state (`why=empty_snapshot`, the shape a stub or broken controller produces). Emits `action_deferred_unobservable_pane`, returns `unobservable_pane`, zero keystrokes |

Ordering: after the dialog gate (so an explicit `waiting_owner` snapshot keeps reporting
`dialog_open`), before any keystroke. The legacy contracts are untouched because a
snapshot that reports a state but no capture flag is treated as readable — exactly the
convention those 15 fixtures encode.

## Tests — `tests/test_actuator_blind_pane_guard.py`, 12 tests

Baseline proof in a `git worktree` at `8e2b1ee` with `tests/conftest.py` sys.path **and**
PYTHONPATH repointed, import origin asserted (`IMPORT-FROM: …/wt-8e2b1ee/core/agent_control.py`,
`pre-fix has pane_capture: False`).

**8 FAIL on pre-fix `8e2b1ee`:** capture-failure refusal; refusal even when a
cached/derived state still says `idle`; empty-snapshot refusal; the refusal event;
a hidden dialog behind a failed capture never answered; `pane_capture` reporting failure
and success distinctly; `Controller.snapshot` carrying the flag.

**4 pass both sides by design** (anti-overcorrection): readable clean pane still delivers
(`acted=True, verified=True`); legacy no-flag snapshot still delivers — the exact shape
that blocked the first attempt; readable-but-blank pane is not a capture failure;
`waiting_owner` keeps `dialog_open`.

**Full suite: 1183 passed, 0 failed** (1171 before). No existing test or fixture changed.

## Limitation

`capture_ok` is only as honest as the controller that reports it. The production
`Controller` derives it from the tmux exit code; a third-party controller that omits the
key is treated as readable unless its snapshot is entirely empty. Both in-repo production
entry points (watchdog, autopilot) refuse blind panes upstream as well, so this is
defence in depth rather than the only line.

---

# PART 2 — The "unattributed deliveries writer" is identified

## Question

Six `deliveries` rows on 2026-08-03T22:29–22:37Z targeted payment:0.0, owneros-direct-fix
and mess-qa-automation with human-descriptive idempotency keys
(`owneros-cancel-wrong-deploy-selection-…`, `payment-submit-safe-replication-scaffold-…`),
with **no** matching `cp_action` / `cw_step` / `autopilot_run` rows. No audited autonomous
path produced them and no agent of this session did.

## Answer: the owner's own ChatGPT-MCP commander channel, via the authenticated API

Evidence chain, each item verified directly:

1. **Endpoint.** `deliveries` is written by `agent_control._deliver` (`core/agent_control.py:616`),
   reached from `POST /api/v1/agents/send` and `/agents/answer`
   (`api/v1.py:249-256`). Both require `_auth` (`api/v1.py:28-43`): `RUNTIME_TOKEN`
   bearer or an HMAC-signed `X-Runtime-Signature` inside a replay window. The writer holds
   valid runtime credentials — it is not an unauthenticated intruder.
2. **Caller address.** The access log for those calls shows client `172.20.0.2`. That
   address is `seo-backend-1` on the docker network `seo_traffic_os`
   (`docker network inspect`, subnet 172.20.0.0/16).
3. **Caller code.** Inside that container, `/app/services/mcp_server.py:418-433` defines
   the MCP tools `agent_send` and `agent_answer`, which call
   `runtime_client.agent_post("/send" | "/answer", {target, text, idempotency_key})` —
   the exact request shape, including the caller-supplied descriptive idempotency keys.
4. **Declared role.** `/app/config/management_modules.yaml` documents the architecture:
   `chatgpt-mcp` — *"role: commander … Strategic commander via MCP tools + task inbox
   (external assistant)"*, driving `claude-agents` through
   *"ai-dev-runtime /api/v1/agents/*"*. This channel is by design.
5. **Content.** The stored `result.delivery_evidence` of
   `owneros-dismiss-ambiguous-one-20260804-0129` contains this very session's rendered
   pane, including the assistant's "Correction pass launched (doc-only, no code, no
   restart)" line and the ambiguous-"1" clarification. The target
   `owneros-direct-fix:0.0` **is this session's pane**.
6. **Timing.** The pattern continues to the present and matches owner prompts exactly:
   `commander-m1-resume-20260804-0718` at 04:19:47Z (the M1 resume instruction) and
   `owneros-actuator-blind-pane-guard-20260804-0737` at 04:37:25Z — the latter landed
   ~1 minute before this task began, and it is this task's instruction.

**Conclusion:** the writer is the owner's own strategic-commander channel (ChatGPT via the
MCP server in `seo-backend-1`) delivering owner instructions into the agent panes over the
authenticated runtime API. It is a legitimate, declared, credential-holding control path —
not a rogue actor and not an autonomous loop. The earlier audits were right that **no
audited autonomous path** produced those rows; the rows were simply the owner talking to
the agents through a channel outside this repo's ledgers.

## Why it looked unattributed

`deliveries` (`core/agent_control.py:299`) stores only
`idempotency_key, target, action, result, created_at, created_ts` — **no caller identity**.
The API knows the client (auth principal, source address) and discards it before the write,
so attribution required correlating three separate systems by hand.

**Recommendation (not implemented — schema/API change beyond this task's scope):** record
an `actor` / `source` column on `deliveries`, populated from the authenticated principal
and client address at `api/v1.py`, so a future "who sent this?" is a single query. This is
an observability change with no effect on safety gates; it needs its own scoped task
because it touches the API contract and the delivery table.

---

## State at completion

- HEAD before: `8e2b1ee`. Suite: 1171 → **1183 passed, 0 failed**.
- Live service untouched: `ai-runtime.service` ACTIVE, MainPID 4063628, started
  2026-08-04 00:29:37 CEST, running `45cfb37` code. Autopilot dormant; canary allowlist
  `cp-canary:0.0`. None of `f9c06ee` / `9fbb7f4` / `8e2b1ee` / this commit is live —
  deploy remains the owner gate.
- Both targeted-review findings (M1, M2) and the M2 actuator deferral are now closed.
- Open: the `deliveries` actor-column recommendation above; deploy.
