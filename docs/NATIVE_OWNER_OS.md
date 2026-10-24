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
* Per event key: latch (`cto_decision`, PK) → provider call → record in `cto_decision` and
  the control-plane `decision` ledger → enqueue at most one outbox message. Replay →
  `duplicate_event_key`, no second call, no second message.
* `owner_needed` (event type, or provider says so) → OWNER_NEEDED via `emit_owner`; the
  advice is NOT forwarded to the agent; nothing is auto-approved.
* Context is compact and bounded (project, agent, goal, state, question, git diff summary,
  ≤5 recent decisions, ≤20 affected agents) — never raw pane text.

| Env | Default | Meaning |
|---|---|---|
| `OWNER_OS_CTO_PROVIDER` | _(empty = disabled)_ | `openai` enables the Responses API adapter |
| `OWNER_OS_CTO_MODEL` | _(none)_ | required when a provider is set; no model is hardcoded |
| `OPENAI_API_KEY` | _(none)_ | read from the environment only; never logged/stored/returned |
| `OWNER_OS_CTO_BASE_URL` | `https://api.openai.com/v1` | override endpoint |
| `OWNER_OS_CTO_DENY_AGENTS` | _(empty)_ | extra agents that never receive a message (payorch-ha-fresh always denied) |

Missing pieces surface as explicit gates: `missing_config:OWNER_OS_CTO_MODEL`,
`missing_secret:OPENAI_API_KEY`.

## Canary (aes-canary only)

1. state from `claude agents --json` (`source: claude_agents_json`, pane joined by pid);
2. `/goal` → ≥2 safe turns without an external "continue";
3. native message reaches aes-canary with no new `deliveries` row (no tmux keystroke);
4. owner-needed → no auto-approval, no forwarded message;
5. replayed event → no duplicate decision or message;
6. no non-canary session changed.

## Old custom supervisor

`core/agent_event_supervisor.py` stays as rollback/reference, disabled by default
(`AGENT_EVENT_SUPERVISOR_ENABLED=0`).
