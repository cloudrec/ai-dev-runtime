# Native Claude Code integration (Owner OS)

Replaces the custom continuation stack (ChatGPT/browser wake → AES → tmux "continue") with
Claude Code 2.1.310's own primitives. Owner OS keeps only the event bus, audit and decision
ledger. Real-project actuation is OFF until the aes-canary passes (see "Canary").

| Concern | Native mechanism | Owner OS module |
|---|---|---|
| Routine continuation | `/goal <condition>` inside the session — Claude keeps working across turns until the condition holds | `core/native_goals.py` + `config/native_goals.yaml` |
| Session state | `claude agents --json` (documented: "Print active sessions (interactive and background) as a JSON array and exit (for scripting; does not require a TTY)") | `core/native_agents.py` |
| Agent coordination | cross-session `ListAgents` / `SendMessage` from a controller Claude session | `core/cto_bridge.py` outbox (`native_outbox`) |
| High-level reasoning | GPT CTO provider (OpenAI Responses API), disabled by default | `core/cto_bridge.py` |

## Controller loop — `core/native_owner_os.py` (wired in `api/main.py` startup)

Disabled by default (`NATIVE_OWNER_OS_ENABLED=0`: `run_loop` returns immediately).
Interval `NATIVE_OWNER_OS_INTERVAL_SECS` (60). Each tick:

1. state via `native_agents.list_sessions()`;
2. only with `NATIVE_GOALS_ENABLED=1`: `native_goals.install` for each explicit config entry
   (idempotent; already-active / latched / non-idle are no-ops);
3. control-plane event deltas through the existing durable `cto_cursor` (consumer
   `native_owner_os`; first start = bus head, no history replay). Explicit reasoning types map
   directly, `owner_action_required` → `owner_needed`, the stack's own events are skipped.
   The cursor is acked per event only AFTER `cto_bridge.handle_event` committed it; an event
   whose handler throws is durably recorded `handler_error` and acked (no wedged bus); if even
   that record cannot be written the tick stops without acking;
4. `cto_bridge.retry_waiting()` once a provider is configured;
5. `cto_bridge.deliver_pending_agent_control()` (no-op unless delivery mode `agent_control`).

## State — `core/native_agents.py`

`list_sessions()` runs `claude agents --json` once (timeout `NATIVE_AGENTS_TIMEOUT_SECS`=30,
output cap `NATIVE_AGENTS_MAX_BYTES`=2 MiB), validates every row (name, kind, cwd, session
id, state; unknown states become `unknown`, never idle) and joins each session to its tmux
pane by Claude pid (`agent_control.agent_list()[].claude_pid`) only when exactly one pane
matches. Non-zero exit, invalid JSON, wrong shape or oversized output → fallback to the
`agent_control` inventory, labelled `source: agent_control_fallback` with the reason.
`capabilities()` reports the installed version and whether `agents --json` exists.

## Continuation — `/goal`

* Goals come only from `config/native_goals.yaml` (explicit per-session condition).
  `payorch-ha-fresh` is hard-denied in code (`HARD_DENY`) and can never get a goal.
* `NATIVE_GOALS_ENABLED=0` by default: `install()` refuses; `status()` is read-only.
* Detection: the last `/goal` command record in the session's own transcript
  (`~/.claude/projects/<cwd>/<sessionId>.jsonl`); `clear` → no goal.
* Install: `/goal <condition>` typed as the session's own input via `agent_control.agent_send`
  (a cross-session message is read literally and would not run a slash command). One
  durable latch per (session, condition hash) before the send; replay → no resend; an
  unknown-outcome send is never retried automatically. Only into an `idle` session
  located through native state.
* No settings change is needed. `idleCompaction` (Claude Code ≥ 2.1.290) is on by default and
  only compacts an idle session over ~200K tokens before its prompt cache expires; the
  "turn off with `"idleCompaction": false`" text is a hint on that notice, not a requirement.
  Compaction keeps the session id and appends to the same transcript, so goal detection and
  messaging are unaffected either way.

## Coordination — native messaging

Host processes have no documented message-sending interface in 2.1.310 (`claude --help`
and `claude agents --help` expose none; the daemon is on-demand and "exits when the last
client disconnects"). So delivery is done by a controller Claude session with `SendMessage`:
it reads `cto_bridge.pending_messages()`, sends each to `to_agent`, then
`cto_bridge.mark_sent(msg_key, receipt)` (pending → sent, exactly once). No tmux keystrokes
are used for coordination. Caveat from the tool contract: a receiving session in a
different permission mode holds cross-session messages for its user's approval, and a
successful send means "reached the inbox", not "read".

## GPT CTO bridge — `core/cto_bridge.py`

* Only `architecture_conflict`, `repeated_failure`, `shared_core_change`,
  `milestone_review`, `owner_needed` reach the provider.
* Per event key: latch (`cto_decision`, PK, with the compact context) → provider call →
  record in `cto_decision` and the control-plane `decision` ledger → enqueue at most one
  outbox message. Replay → `duplicate_event_key`, no second call, no second message.
* Multi-turn: one durable OpenAI conversation per Owner OS project (`cto_conversation`:
  `POST /v1/conversations` on first use, then `"conversation": <id>` on every
  `POST /v1/responses`; `instructions` are sent on every response). Survives restarts.
* No provider / model / key → `waiting_provider` (durable, carries the gate reason) and is
  decided by `retry_waiting()` once config exists. Provider errors retry up to 3 attempts,
  then `provider_failed`. A `latched` row (crash mid-call) is never retried automatically.
* `owner_needed` is never auto-approved and its advice is NOT forwarded to the agent. An
  `owner_needed` event already notified the owner itself; a provider escalation of a
  non-owner event emits `owner_needed_by_cto` once.

Delivery (`OWNER_OS_CTO_DELIVERY_MODE`):

| Mode | Behaviour |
|---|---|
| `native_controller` (default) | outbox left for a controller Claude session: `pending_messages()` → `SendMessage` → `mark_sent()` |
| `agent_control` | server-side fallback for rare advice: `agent_control.agent_send` only into an `idle` session located via native state; busy/unlocated stay pending; payorch-ha-fresh → `denied`; `sent` only when agent_send reports submitted+delivered; text prefixed `[Owner OS CTO advice — automated, not owner approval]` |
| `off` / unknown | nothing delivered |
* Context is compact and bounded (project, agent, goal, state, question, git diff summary,
  ≤5 recent decisions, ≤20 affected agents) — never raw pane text.

| Env | Default | Meaning |
|---|---|---|
| `OWNER_OS_CTO_PROVIDER` | _(empty = disabled)_ | `openai` enables the Responses API adapter |
| `OWNER_OS_CTO_MODEL` | _(none)_ | required when a provider is set; no model is hardcoded |
| `OPENAI_API_KEY` | _(none)_ | read from the environment only; never logged/stored/returned |
| `OWNER_OS_CTO_BASE_URL` | `https://api.openai.com/v1` | override endpoint |
| `OWNER_OS_CTO_DENY_AGENTS` | _(empty)_ | extra agents that never receive a message (payorch-ha-fresh always denied) |
| `OWNER_OS_CTO_DELIVERY_MODE` | `native_controller` | see Delivery |
| `NATIVE_OWNER_OS_ENABLED` | `0` | controller loop on/off |
| `NATIVE_OWNER_OS_AGENTS` | _(empty = all)_ | canary scope: only events for these sessions/targets reach the CTO bridge; others are acked and ignored |

Missing pieces surface as explicit gates: `missing_config:OWNER_OS_CTO_MODEL`,
`missing_secret:OPENAI_API_KEY`.

## Canary (aes-canary only)

1. state from `claude agents --json` (`source: claude_agents_json`, pane joined by pid);
2. `/goal` → ≥2 safe turns without an external "continue";
3. native message reaches aes-canary with no new `deliveries` row (no tmux keystroke);
4. owner-needed → no auto-approval, no forwarded message;
5. replayed event → no duplicate decision or message;
6. no non-canary session changed.

## Live canary service config (ai-runtime)

| Drop-in (`/etc/systemd/system/ai-runtime.service.d/`) | Sets |
|---|---|
| `96-native-owner-os-canary.conf` | `NATIVE_OWNER_OS_ENABLED=1`, `NATIVE_GOALS_ENABLED=1`, `NATIVE_OWNER_OS_AGENTS=aes-canary`, `OWNER_OS_CTO_DELIVERY_MODE=off` |
| `97-native-cto-provider.conf` (repo: `deploy/`) | `OWNER_OS_CTO_PROVIDER=openai`, `OWNER_OS_CTO_MODEL`, `OWNER_OS_CTO_DELIVERY_MODE=native_controller` (sorts after 96, so it wins), `EnvironmentFile=-/etc/ai-runtime/cto-openai.env` |
| `/etc/ai-runtime/cto-openai.env` (root 0600, never in git) | `OPENAI_API_KEY` only |

`native_controller` delivery keeps advice in `native_outbox` for a controller Claude session;
the server never types it into an agent. Hard denies are unchanged (`payorch-ha-fresh` in
code for goals, CTO events and delivery).

All steps are owner-run as root via `tools/native_cto_key.sh`; each restarts
`ai-runtime.service` only and then runs `tools/native_cto_verify.py` against the running
process (env from `/proc/<MainPID>/environ`, key reported only as present/absent):

| Step | Command | Verified state |
|---|---|---|
| stage provider, no key | `tools/native_cto_key.sh preconfig` | `--expect missing_secret`: gate `missing_secret:OPENAI_API_KEY`, decisions only `waiting_provider` |
| add the key | `tools/native_cto_key.sh install` (prompts, echo off) | `--expect ready --key-check`: gate empty, `GET /models/<model>` accepted (no tokens) — on failure the key is removed again |
| rollback | `tools/native_cto_key.sh remove` | `--expect disabled`: provider off; the 96 canary keeps running |

Every check also fails on: canary scope empty, `agent_control` delivery mode, missing
controller cursor, decisions for agents outside `NATIVE_OWNER_OS_AGENTS`, pending advice for a
denied agent, or a delivery made by this stack (`goal:` / `cto:` keys) outside the scope.
Other senders (e.g. `nativesup:`) are counted, not judged. Watchdog: unread bus events while
the controller cursor has not moved for 10 minutes fail every check (loop dead or wedged). The
`deliveries` table is pruned by retention, so `--since-rowid` evidence covers only retained rows.
`agent_control` fallback delivery also enforces `NATIVE_OWNER_OS_AGENTS` (out-of-scope advice
stays `pending`), in addition to the event-level scope filter and the hard denies.

Offline acceptance: `tests/test_native_owner_os_acceptance.py` chains event → decision → outbox
→ delivery → verifier through the real controller tick (all delivery modes, busy, out-of-scope,
payorch, owner_needed, provider outage, missing secret, replay) and runs `native_cto_key.sh`
preconfig / install / failed install / rollback in a sandbox. Full rollback of the canary: also
remove `96-native-owner-os-canary.conf`, `systemctl daemon-reload`, restart.

## Old custom supervisor

`core/agent_event_supervisor.py` stays as rollback/reference, disabled by default
(`AGENT_EVENT_SUPERVISOR_ENABLED=0`).
