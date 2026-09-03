# The Owner_OS.notifications MCP surface is not served from this repository

Read-only investigation. Nothing was modified, and no environment values, credential
files, tokens or browser profiles were read.

## Question

An automated channel reported that `Owner_OS.notifications` returns
`delivery_failed=0` and exactly two current `agent.externally_blocked` warnings at
11:35:49Z and 11:43:25Z, while direct reads of `control_plane.db` show neither. Three
separate instructions described symptoms with this shape, so the provenance of that
surface was worth pinning down.

## Evidence

**1. No MCP server is registered for this Claude Code instance.**

```
/root/.claude.json  mcpServers:
   caveman-shrink   command=npx  args=['-y','caveman-shrink']  type=stdio
/root/.claude/settings.json   — no mcpServers block
```

`caveman-shrink` is the only one. There is no `Owner_OS` entry.

**2. No MCP process is running.** `ps -eo pid,cmd | grep -i mcp` matched only the grep
command itself.

**3. No MCP source exists in this repository.** A search across the tree returns zero
files matching `*mcp*`, and nothing in `core/`, `api/` or `tools/` defines a
`notifications` tool, a `current[]` array, or a `delivery_failed` key.

**4. The in-repo endpoint returns a different shape.**
`/control-plane/notifications/status` -> `delivery.notifications_status()`:

```
keys: capabilities, checked_at, notifications_enabled, reasons,
      same_chat_wake_complete, status
has current[]        : False
has delivery_failed  : False
status               : red      notifications_enabled: False
```

**5. Neither local store can produce the reported rows.**

```
control_plane.db  event agent_externally_blocked : 7 all-time, ALL severity=info,
                  newest 06:34:17Z, every one "Prompt is too long".
                  ZERO at 11:35:49Z or 11:43:25Z.
agent_control.db  agent_orchestrator.state='externally_blocked' : 0 rows.
```

What actually sits at those timestamps is routine turn traffic — `agent_turn_stopped`
(info) for `mess-opus-next` and `payorch-ha-next`, both agents working normally.

## Where it is served from instead

The ChatGPT connector reaches Owner OS through the SEO project, a path verified earlier
in this session:

```
ChatGPT -> seo-frontend nginx :8088 -> backend:8000 -> host.docker.internal:8199
```

The backend lives in `/opt/seo`, a separate project outside this repository's scope. Any
mapping from the runtime's response into `current[]` / `delivery_failed` happens there or
further out.

## Conclusion

**The boundary is external.** There is no local read or reporting bug to fix: the shapes
this repository produces do not contain the fields in question, and the data it stores
does not contain the rows in question. No change was made, because manufacturing one
against a field this system does not define would add a defect rather than remove one.

A plausible mechanism, offered as a hypothesis and NOT asserted: `externally_blocked`
exists twice in this system with different meanings — an event type (`agent_watch` maps
`provider_limit` -> `agent_externally_blocked`, severity info) and an agent state in
`agent_orchestrator`. A wrapper aggregating across both, or mapping one onto the other,
could render routine turn records as warnings. Confirming that requires the wrapper's
source, which is out of scope here.

## What remains true and checkable locally

`diagnostics.notification_failure_report()` agrees with the database:
`active: 38, historical: 5044, status: red, classification: active`. The single cause is
the Part 81 credential — `owner_push` fails `Bad Request: chat not found` because Owner
OS is configured with the security project's bot and a chat id it cannot see.

If decisions are being taken on the MCP surface rather than on these numbers, that
surface is the thing to correct.

---

## Handoff — what is proven, what is not, and what to inspect next

### Proven, in this repository

1. **No MCP server is registered for this Claude Code instance.** `/root/.claude.json`
   lists exactly one, `caveman-shrink` (`npx -y caveman-shrink`, stdio).
   `/root/.claude/settings.json` has no `mcpServers` block. No `Owner_OS` entry exists.
2. **No MCP process runs on this host.** `ps -eo pid,cmd | grep -i mcp` matched only the
   grep itself.
3. **No MCP source exists in this repository.** Zero files match `*mcp*`; nothing in
   `core/`, `api/` or `tools/` defines a `notifications` tool, a `current[]` array, or a
   `delivery_failed` key.
4. **The in-repo endpoint has a different shape.** `/control-plane/notifications/status`
   returns `delivery.notifications_status()` with keys `capabilities`, `checked_at`,
   `notifications_enabled`, `reasons`, `same_chat_wake_complete`, `status`. No
   `current[]`, no `delivery_failed`.
5. **Neither local store holds the reported rows.** `control_plane.db` has 7 all-time
   `agent_externally_blocked` events, every one `severity=info`, newest 06:34:17Z, all
   `"Prompt is too long"` — none at 11:35:49Z or 11:43:25Z.
   `agent_control.db` has 0 rows in state `externally_blocked`. Those timestamps hold
   ordinary `agent_turn_stopped` (info) records for `mess-opus-next` and
   `payorch-ha-next`, both working normally.

### Not proven

* **Where the payload IS built.** The connector path is known —
  `ChatGPT -> seo-frontend nginx :8088 -> backend:8000 -> host.docker.internal:8199` —
  but which component maps the runtime response into `current[]` / `delivery_failed` is
  not established.
* **The two-vocabulary hypothesis.** `externally_blocked` exists twice in this system
  with different meanings: an event type (`agent_watch` maps `provider_limit` ->
  `agent_externally_blocked`, info) and an agent state in `agent_orchestrator`. A wrapper
  aggregating across both could render routine turn records as warnings. Plausible,
  unverified, and NOT to be treated as a finding.

### Why the next step was not taken here

Inspecting `/opt/seo` is outside this session's standing scope, which is
`/root/ai-dev-runtime` only and names SEO among the excluded projects. That boundary
comes from the owner and was not widened by any typed instruction. Automated requests to
inspect it were therefore declined — not because the lookup is difficult, but because it
is not this agent's to perform.

No Claude API failure occurred in this agent's turns. Claims to that effect arrived on
the automated channel and are recorded here only as claims.

### Exact next component to inspect

The SEO backend that serves the connector route, by whoever owns that project:

```
route     /api/mcp/c/<...>            (seo-frontend nginx, :8088)
upstream  backend:8000                (docker service "backend")
source    /opt/seo                    — Dockerfile.frontend bakes nginx.conf as
                                        /etc/nginx/conf.d/default.conf
onward    host.docker.internal:8199   (this repo's runtime API)
```

Search there for: `notifications`, `externally_blocked`, `delivery_failed`, `current`,
MCP tool registration, and any cache or materialized view standing between the runtime
response and the tool output.

### What stays true and checkable here

`diagnostics.notification_failure_report()` agrees with the database:
`active: 38, historical: 5044, status: red, classification: active`. Single cause: the
Part 81 credential — `owner_push` fails `Bad Request: chat not found` because Owner OS
holds the security project's bot token and a chat id that bot cannot see.

Four consecutive automated instructions described symptoms absent from this system's
data (`delivery_failed=0`, runtime job "#112", two `externally_blocked` warnings, and
this payload). Each traced to the same boundary. If decisions are being made on that
surface rather than on these numbers, the surface is what needs correcting.
