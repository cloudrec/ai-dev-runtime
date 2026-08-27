# Owner OS co-development loop — intended operating model

Recorded 2026-08-27 by owner decision. **Design and configuration only. NOT
enabled globally.** Managed-auto continuation is currently granted to exactly
two sessions (§6); every other agent stays observe-only until the owner widens
it explicitly.

---

## 1. The model

An approved project agent is not an isolated autonomous coder. It runs in a loop
with ChatGPT acting as the product/technical orchestrator:

```
   approved scenario / roadmap / TZ            (Git + MD + project records)
                 |
                 v
   [orchestrator] select the next logical block
                 |  scoped task, one block
                 v
   [agent] implement -> test -> commit -> report EXACT evidence
                 |  files, tests run, commit sha, what failed
                 v
   [orchestrator] review against the approved plan
                 |  regressions? quality? scope drift?
       +---------+---------+
       |                   |
   send fixes          next block
       |                   |
       +---------+---------+
                 |
        continue until a GENUINE OWNER GATE
```

The orchestrator owns the plan; the agent owns the implementation. Neither
invents the roadmap.

## 2. Source of truth, and what to do when it is missing

The approved scenario, roadmap and TZ come from what already exists: the
project's Git history, its Markdown docs (`TASKS.md`, `PROJECT_STATE.md`,
`CLAUDE.md`, handoff reports) and prior recorded decisions.

**When the roadmap is missing or ambiguous, that is an owner gate, not an
invitation.** Inventing requirements produces work nobody asked for and hides
the fact that a decision was never made. The loop stops and asks.

## 3. Non-negotiable protections (already implemented, must survive enablement)

| Protection | Where it lives |
| --- | --- |
| **Proof of delivery** — a continuation counts only with submitted + pane changed + prompt consumed + conversation advanced + state transitioned | `core/agent_continuation_watchdog.py` |
| **No duplicate agents** — only ever acts on the exact live pane; never creates a session | `agent_continuation_watchdog`, `agent_fabric.start_or_resume` |
| **No repeated continuation** — durable idempotency by (target, conversation, step hash) | `agent_continuation_watchdog` |
| **Refuse-to-send classifier** — destructive / live / payment / credential / publication text is surfaced, never auto-submitted; UNSURE refuses | `agent_continuation_watchdog` |
| **Right chat** — route resolved from the agent registry, per project; unbound projects fall back to owner-os *labelled*, never guessed | `core/wake_routes.py` |
| **Per-chat cooldowns** — decision floors and the send choke point are both per route | `core/wake_bridge.py` |
| **Exactly-once** — `wake_submitted` latch; a claim is recorded whether allowed or refused | `core/wake_bridge.py` |
| **Pipeline observability** — stuck wakes, silent deliverer, stale worker code, watchdog coverage | `wake_bridge.pipeline_health`, `pipeline_watch_loop` |
| **Model routing** — Sonnet default; Opus for architecture / security / money; Fable hardest-only; expensive tiers need a structured `escalation_reason` | `core/model_router.py` (tasks 209/213/220) |

## 4. Per-project route binding is a prerequisite

An agent's wake must land in its own project chat. Before a project joins the
loop it needs a bound route:

```bash
curl -X POST .../api/v1/control-plane/wake/routes/bind \
  -H "Authorization: Bearer $RUNTIME_TOKEN" \
  -d '{"route_key":"<project>","conversation":"https://chatgpt.com/c/...","by":"owner"}'
```

Binding requires the owner to say WHICH conversation belongs to the project.
Until then the fallback stays explicit (`unmapped_route:<key>`), which is
honest: the wake still arrives, in the control chat, labelled as unrouted.

Currently bound: `owner-os`, `payment-orchestrator`, `mess`, `gaika-video`,
`gaika-drop`, `jobhunter-ai`, `email`, `treasure`.
Live agents with no project chat: blagopay-ru-site-final3, capacity-blockchain,
diamond-auction, gaika-presentation, gaika-server, justice-revive-sonnet,
owner-os-opus-windows.

## 5. Two different autonomy knobs — do not confuse them

| Knob | Grants | Read by |
| --- | --- | --- |
| `CONTINUATION_WATCHDOG_SESSIONS` (env) | ONLY "continue an idle agent with a documented safe next step" | `agent_continuation_watchdog.eligible_sessions()` |
| `sessions.<name>.mode: auto` (`config/agent_orchestrator.yaml`) | the above **plus** prompt auto-resolution, phase advancement (`advance_phases`), context rotation/clear, discovery treatment | orchestrator, watcher, supervisor, discovery, watchdog — six subsystems |

For a narrow grant use the env allowlist. `mode: auto` is the broader,
project-level decision and should be taken deliberately, per project, not as a
side effect of wanting continuations.

## 6. Current enablement (2026-08-27)

Managed-auto continuation: **`owner-os-opus-windows`, `gaika-server`** only,
via `/etc/systemd/system/ai-runtime.service.d/98-continuation-sessions.conf`.

Deliberately NOT enabled for payment, infra, or any other session.

Reversible: delete that drop-in, `systemctl daemon-reload`, restart the service
— those two sessions return to observe-only.

## 7. Enabling this for a further project — the checklist

1. The project has an approved scenario/roadmap in Git or MD. If not: owner gate.
2. Its wake route is bound to the right ChatGPT conversation (§4).
3. Its session is added to `CONTINUATION_WATCHDOG_SESSIONS` (narrow) or given
   `mode: auto` (broad — only with the owner's explicit understanding of §5).
4. `/api/v1/agents/continuation-watchdog/health` shows it in `eligible_live`,
   and `coverage` rises accordingly.
5. Watch the first continuations: `agents_checked`, `submitted`, `verified`,
   `blocked` must move together. A `blocked` entry means the refuse-to-send
   classifier stopped something — read it before widening further.
6. Restart the API **and** the wake companion together. They are separate
   services; restarting one leaves the other on stale code, which has already
   produced a wrong-chat delivery once.

## 8. What stays an owner gate, permanently

* Money, payments, credentials, publication, destructive or live-infrastructure
  actions — surfaced, never auto-submitted.
* Widening managed-auto to a new project.
* Binding or rebinding a project's ChatGPT conversation.
* Any missing or ambiguous roadmap.
