# Direct Agent Control Plane — Final Evidence Report

**Date:** 2026-07-17
**Status:** Implemented, tested, merged, deployed and verified in production.
**Scope note:** the work spans **two** repositories, not one. See *Root cause #2*.

---

## 1. Root cause

### 1.1 Why Runtime marked documentation-only jobs as completed (jobs 59, 60, 61)

The runtime already had the right *concept* and the wrong *field*.

Earlier work (`core/job_kinds.py`, from the OWNER-111 recovery) introduced an
`outcome` field, correctly set to `fallback_plan_only` whenever the AI planner
failed and the deterministic local fallback produced a Markdown PLAN instead of
an implementation. That part worked.

But `status` — the field every consumer actually filters on — was still written
as `completed`:

- `core/job_executor.py:301` — plan-only path (`autonomy <= suggest`):
  `_finish(job_id, "completed", outcome=job_kinds.FALLBACK_PLAN_ONLY)`
- `core/job_executor.py:502` — fallback path:
  `_finish(job_id, "completed", outcome=outcome)` where `outcome` was
  `fallback_plan_only`.

So each job carried a truthful `outcome` **and** a false `status`, and the
status won: the poller, the notifier and the owner's job list all read
`completed`. The outcome field was telling the truth to nobody.

Two smaller defects compounded it, both found while fixing the above:

- `runtime_poller._TERMINAL` (Owner OS) did not know about any plan-only status.
  Had the status been fixed without touching the poller, plan-only jobs would
  have polled forever and never notified the owner **at all** — a quieter
  failure than the one being fixed.
- `runtime_poller._on_terminal` mapped an unrecognised terminal status to the
  `review` task state by default, implying the work was ready to review. It now
  fails closed to `blocked`.

### 1.2 Why the MCP server could not manage the tmux agents (the actual blocker)

The task states the Owner OS MCP server lives in `/root/ai-dev-runtime`. **It
does not.** It lives in a different repository and runs in a different place:

- **Code:** `/opt/seo/backend/services/mcp_server.py` (`SERVER_INFO = {"name": "owner-os"}`)
- **Runs in:** the `seo-backend-1` Docker container.

That container **cannot** see the agents, and no amount of code inside it could:

```
$ docker inspect seo-backend-1 --format '{{json .HostConfig.Binds}} PID:{{.HostConfig.PidMode}}'
["seo_media_data:/data/media","seo_backups_data:/data/backups",
 "/root/.local/bin/claude:/usr/local/bin/claude:ro","/root/.claude:/root/.claude:ro",
 "/opt:/opt:ro","/root/ai-dev-runtime:/root/ai-dev-runtime:ro"]   PID:            <- no host PID namespace

$ docker exec seo-backend-1 sh -c 'which tmux; ls /tmp/tmux-0'
ls: cannot access '/tmp/tmux-0': No such file or directory        <- no tmux, no socket
```

No tmux binary, no tmux socket, no host PID namespace, and `/opt` +
`/root/ai-dev-runtime` mounted **read-only**.

**Resolution.** The control plane was split at the privilege boundary:

- The **host half** (`core/agent_control.py`) runs inside `ai-runtime.service` —
  root, real tmux, real `/proc` — and does all the work.
- The **MCP half** (`/opt/seo`) registers the eight tools as thin authenticated
  proxies to `ai-runtime`'s `/api/v1/agents/*` routes.

This reuses the connection that already exists (`RUNTIME_URL`, already used by
`submit()`/`poll()`), and was verified reachable before any code was written.
The alternative — mounting the tmux socket and host PID namespace into a
public-facing container — was rejected: it would hand the internet-exposed SEO
backend root control of every process on the host.

---

## 2. Files changed

### `/root/ai-dev-runtime` (host control plane + truthfulness fix)

```
 api/v1.py                           |  79 ++      authenticated /api/v1/agents/* routes
 core/agent_control.py               | 736 ++      NEW — the control plane
 core/job_executor.py                |  17 +-      _finish truthfulness chokepoint
 core/job_kinds.py                   |  36 ++      terminal_status_for / is_truthful_terminal
 core/job_store.py                   |   5 +-      fallback_plan_only is a known status
 core/notify_format.py               |  74 +-      event_type_for + four independent facts
 tests/test_agent_control.py         | 469 ++      NEW — 60 tests
 tests/test_fallback_truthfulness.py | 136 ++      NEW — 19 tests
 tests/test_heartbeat_race.py        |   9 +-      legacy assertion corrected
 tests/test_job_kind_pipeline.py     |   6 +-      legacy assertion corrected
 tests/test_planner_fallback.py      |  24 +-      legacy assertions corrected (6)
 11 files changed, 1576 insertions(+), 15 deletions(-)
```

### `/opt/seo` (Owner OS MCP registration)

```
 backend/services/mcp_server.py        | 156 ++    8 agent_* tools + schemas
 backend/services/runtime_client.py    |  40 ++    authenticated agent proxy
 backend/services/runtime_poller.py    |  14 +-    plan-only terminal + fail-closed default
 backend/tests/test_mcp_agent_tools.py | 201 ++    NEW — 27 tests
 4 files changed, 409 insertions(+), 2 deletions(-)
```

**On the corrected legacy tests:** six assertions of the form
`assert final["status"] == "completed"` for *fallback* scenarios were updated to
`"fallback_plan_only"`. These tests encoded the bug — they asserted the exact lie
this task exists to remove. `test_job_kind_pipeline.py` was the clearest case: its
docstring already read *"The plan must now report `fallback_plan_only`"* while the
assertion checked `status == "completed"` and only the `outcome` field was
verified. `test_heartbeat_race.py` runs at `autonomy=suggest` with a *valid*
plan, so it is legitimately plan-only; its point (the heartbeat survives a
concurrent reap sweep) is unchanged.

---

## 3. The eight tools

All in `core/agent_control.py`, all registered in MCP.

| Tool | Scope | Behaviour |
|---|---|---|
| `agent_list` | read | Inventories every tmux pane: session, pane target, PID, cwd, command, alive/dead, plus the Claude process per pane. Reports duplicates (>1 live Claude on one cwd). |
| `agent_status` | read | One agent, read-only: tmux state, PID, cwd, command, conversation evidence from `~/.claude/projects`, recent activity. Refuses ambiguous targets. |
| `agent_read` | read | Bounded capture (default 200, hard max 2000 lines), secret-redacted. |
| `agent_send` | write | Multiline delivery via tmux buffer. Never creates an agent. Idempotency key. Proves delivery by pane diff. |
| `agent_answer` | write | As `agent_send`, for answering a waiting prompt. |
| `agent_resume` | write | Resumes **only** when no live Claude works in that directory and the session name is free. Always reports `duplicate_created`. |
| `agent_report` | read | Report/status files + timestamps from allowlisted subdirs (`reports/`, `docs/`); `agent_report_read` reads one, bounded and redacted. |
| `agent_stop` | write | Requires `confirm=true` (exactly `True`). Refuses ambiguous targets. `kill-pane` only — never `kill-session`/`kill-server`. |

### Security properties

- **No arbitrary shell tool.** No such route exists to proxy to. Pinned by
  `test_no_arbitrary_command_tool_exists` and
  `test_module_exposes_no_arbitrary_command_tool`.
- **No shell at all.** Every tmux call is an argv list; `shell=True` and
  `os.system` are absent, pinned by `test_tmux_transport_never_uses_a_shell`.
- **Injection.** Session/target regexes reject `;`, `$()`, backticks,
  whitespace, newlines and `..`. Verified live (§7).
- **Traversal.** `validate_project_dir` resolves via `realpath` before the
  allowlist check, so a symlink pointing out is rejected on its resolved target.
- **send-keys is not used for payloads.** Text travels as buffer stdin; only the
  literal `Enter` key is sent. send-keys would interpret a payload as key names.
- **Bounds.** 2000 lines capture, 16 KB message, 256 KB report.
- **Redaction.** `sk-ant-*`, `sk-*`, `mcp_*`, `gh[pousr]_*`, AWS keys, bearer
  tokens, `*token/secret/password/api_key=*`, PEM private keys.
- **Audit.** Every action appends `{ts, action, target, idempotency_key, ...}` to
  `/var/log/ai-runtime/agent_control.jsonl`. Audit failure never breaks the action.
- **Scopes.** read for list/status/read/report; write for send/answer/resume/stop.
  Verified live: a read-scope token calling `agent_send` got
  `-32003 "this token lacks the 'write' scope"`.

---

## 4. Tests and exact results

**`/root/ai-dev-runtime`** — `./venv/bin/python -m pytest -q`

```
299 passed, 4 warnings in 57.38s
```

**`/opt/seo/backend`** — run inside the container against the mounted source:

```
tests/test_mcp_agent_tools.py                     27 passed
tests/test_mcp.py test_runtime.py test_runtime_poller.py
tests/test_runtime_adopt.py test_runtime_reconcile.py
tests/test_runtime_retry.py test_notifications.py  77 passed, 34 warnings in 4.62s
```

Coverage of every area the task named:

| Required | Where |
|---|---|
| tmux inventory parsing | `test_parse_panes_reads_every_field`, `test_parse_panes_marks_dead_pane_and_skips_junk`, `test_agent_list_handles_no_tmux_server` |
| existing-agent detection | `test_agent_list_detects_claude_agent`, `test_is_claude_cmdline_ignores_version_probe`, `test_find_live_agent_for_dir_matches_cwd` |
| duplicate prevention | `test_agent_list_reports_duplicate_agents`, `test_resume_refuses_when_live_agent_exists`, `test_resume_refuses_when_session_name_taken`, `test_resume_creates_session_only_when_none_exists` |
| bounded capture | `test_read_bounds_line_count_to_ceiling`, `test_read_returns_at_most_requested_lines`, `test_read_not_truncated_when_everything_fits`, `test_read_rejects_bad_line_count` |
| multiline delivery | `test_send_delivers_multiline_via_buffer_not_send_keys`, `test_send_proves_delivery_with_pane_diff`, `test_send_cleans_up_buffer` |
| idempotency | `test_send_is_idempotent_per_key`, `test_distinct_keys_deliver_separately`, `test_send_generates_key_when_absent` |
| invalid target rejection | `test_invalid_targets_are_rejected` (10 cases), `test_read_rejects_invalid_target_before_touching_tmux`, `test_session_allowlist_is_enforced` |
| path validation | `test_project_dir_must_be_inside_allowed_roots`, `test_project_dir_rejects_traversal`, `test_project_dir_rejects_symlink_escape`, `test_report_read_rejects_path_traversal` |
| stop confirmation | `test_stop_requires_confirmation`, `test_stop_rejects_truthy_non_true_confirm`, `test_stop_refuses_ambiguous_target`, `test_stop_with_confirmation_kills_only_that_pane` |
| fallback jobs not completed | `tests/test_fallback_truthfulness.py` (19 tests) |
| MCP tool registration | `backend/tests/test_mcp_agent_tools.py` (27 tests) |

---

## 5. Backup references, commits, deployment

| Item | Value |
|---|---|
| **Backup ref (runtime)** | tag `backup/pre-agent-control-plane-20260717` = `f7d9edfa9b853839f2fac46f70b8d1b84bb02a28` |
| **Backup ref (Owner OS)** | tag `backup/pre-agent-mcp-tools-20260717` = `e5eb90ad705e6bec434e4f7a40637726ab3dfbcb` |
| **Implementation branch** | `feat/direct-agent-control-plane` |
| **Implementation commit** | `4dfe99e` |
| **Merge commit** | `40f224c` |
| **Follow-up fix** | `deec322` |
| **Production main (runtime)** | **`deec322bcb03dcfd92c1101dfacf3cff6afadbe9`** |
| **Owner OS commits** | `0a4b036` (tools), `8b80176` (URL fix) on `repair/owner-os-runtime-e2e-20260714` |
| **Services restarted** | `ai-runtime.service` (systemd), `seo-backend-1` (docker compose build + up) |

Restart evidence:

```
$ systemctl is-active ai-runtime.service
active
$ curl -s http://172.17.0.1:8199/health
{"status":"ok","started_at":"2026-07-17T12:31:23.016580","uptime_seconds":2.65,"tasks_total":0}

$ docker ps --filter name=seo-backend-1 --format "{{.Names}} {{.Status}}"
seo-backend-1 Up 22 seconds (healthy)
```

No other service was touched.

---

## 6. MCP tool verification

Verified through the **real MCP JSON-RPC protocol** over HTTP against the
deployed container, using an ephemeral read-scope token that was revoked
immediately afterwards:

```
initialize -> protocol 2025-06-18 | server {'name': 'owner-os', 'version': '1.0.0'}
tools/list -> HTTP 200 | total 34
agent tools advertised: ['agent_answer', 'agent_list', 'agent_read', 'agent_report',
                         'agent_resume', 'agent_send', 'agent_status', 'agent_stop']
ALL 8 REGISTERED VIA MCP PROTOCOL: True
agent_read schema: {'type': 'object', 'properties': {'target': {'type': 'string'},
  'lines': {'type': 'integer', 'default': 200, 'minimum': 1, 'maximum': 2000}},
  'required': ['target'], 'additionalProperties': False}
verification token revoked
```

Tool count went 26 → 34. The proxy reaches the host from inside the container:

```
RUNTIME_URL = http://host.docker.internal:8199/api/v1
agent_list via MCP proxy -> panes: 9 | duplicates: []
safeguard seen: ['safeguard:0.0']
```

### ⚠️ Required client action — reconnect to see the tools

**A client that was already connected before this deployment will not show the
eight new tools until it reconnects.** This is an MCP client-side limitation, not
a server defect: the tool list is fetched at connection time and the already-open
ChatGPT session holds a cached list. The server advertises all eight now, as the
`tools/list` output above proves.

**To pick them up:** disconnect and reconnect the Owner OS MCP connector in the
client (ChatGPT → connector settings → reconnect the `owner-os` server), or start
a new session. No server-side action is needed or pending.

---

## 7. Safeguard canary — READ-ONLY

The existing Safe Guard agent was discovered, inspected and read. **No task was
sent to it and nothing about it was modified.**

**Located** (`agent_list`, 9 panes, 9 agents, 0 duplicates):

```
safeguard:0.0   agent=True  alive=True  pid=3177203  claude_pid=3177203
                cwd=/root/safeguard-remote-agent
```

**Status** (`agent_status`, via the MCP protocol):

```
target = safeguard:0.0        session = safeguard      alive = True
is_agent = True               claude_pid = 3177203     command = claude
cwd = /root/safeguard-remote-agent
conversation.present = True   count = 2
conversation.latest = cc65c6f1-4c90-4304-a99e-e4ba8d061b37 @ 2026-07-17T10:20:11Z
```

**Recent output** (`agent_read`, bounded to 5 lines, redacted):

```
requested: 5 | returned: 5 | available: 12 | truncated: True | actual output lines: 5
```

**Duplicate prevention proven live** — `agent_resume` on a directory that already
has a live agent returned the existing one rather than starting a second:

```json
{ "resumed": false,
  "reason": "a live Claude agent already works in this directory — reusing it",
  "existing_agent": { "target": "owneros-direct-fix:0.0", "claude_pid": 3717955,
                      "cwd": "/root/ai-dev-runtime" },
  "duplicate_created": false, "agent_created": false }
```

**Refusals proven live:**

```
stop without confirm -> {"detail":"agent_stop is destructive and requires confirm=true for the exact target"}
target "safeguard;kill-server" -> {"detail":"invalid tmux target: 'safeguard;kill-server'"}
target "../../etc"             -> {"detail":"invalid tmux target: '../../etc'"}
target "sess$(id)"             -> {"detail":"invalid tmux target: 'sess$(id)'"}
read-scope token -> agent_send -> {"code":-32003,"message":"this token lacks the 'write' scope"}
```

### `duplicate_created = false`

| Evidence | Before | After |
|---|---|---|
| tmux sessions | 9 | **9** (identical names) |
| Safe Guard Claude PID | 3177203 | **3177203** (unchanged) |
| Sessions created by the control plane | — | **0** (`"resumed": true` never appears in the audit log) |
| Panes killed by the control plane | — | **0** |
| Messages delivered to Safe Guard | — | **0** |

Audit trail:

```
{"ts":"2026-07-17T10:33:13Z","action":"agent_read","target":"safeguard","ok":true,"lines":12}
{"ts":"2026-07-17T10:34:02Z","action":"agent_list","target":"-","agents":9,"duplicates":0}
{"ts":"2026-07-17T10:34:02Z","action":"agent_resume","target":"owneros-direct-fix:0.0","resumed":false,"reason":"live agent already exists"}
{"ts":"2026-07-17T10:34:02Z","action":"agent_stop","target":"no-such-session-xyz","stopped":false,"reason":"not confirmed"}
```

*(The host's total `claude` process count read 17 before and 16 after. That is an
unrelated subagent of another session exiting on its own: no tmux session
changed, and every agent PID above is unchanged. The control plane created
nothing and killed nothing — the audit log records zero of each.)*

---

## 8. Runtime truthfulness — what changed

A plan-only run now ends in its own terminal status, `fallback_plan_only`, which
is **never** `completed`:

- `job_kinds.terminal_status_for(outcome, proposed)` — fail-closed: a
  `fallback_plan_only` outcome may only resolve to `failed`, `blocked` or
  `fallback_plan_only`. `completed` is coerced away.
- `job_executor._finish` routes **every** terminal transition through it, so a
  future code path cannot reintroduce the lie by passing `"completed"` directly.
  Pinned by `test_finish_downgrades_a_plan_only_completed_call`.
- `notify_format.event_type_for(outcome)` returns
  `runtime.job.fallback_plan_only`, never `runtime.job.completed`, for a plan.
  A mislabelled inbound `runtime.job.completed` carrying a plan-only outcome is
  still *rendered* truthfully — the headline comes from the outcome, so it cannot
  be laundered into a success message.
- `runtime_poller` treats the new status as terminal and maps it to the `blocked`
  task state, with an unknown status now failing closed to `blocked` rather than
  `review`.

Notifications now report the five facts **independently**, because "completed"
was being read as all of them at once:

```
✅ Implementation completed / ❌ Implementation NOT completed
✅ Tests passed              / ❌ Tests not passed / ❔ unknown
✅ Branch created: <name>    / ❌ No branch created
✅ Released to production    / ❌ Not released / ❔ unknown
⚠️ Fallback plan only — a document, not an implementation
```

Unknown facts render as `❔ … (unknown)` rather than being assumed true.

---

## 9. Definition of Done

| Requirement | Status |
|---|---|
| Real source files changed | ✅ 15 files, +1985 lines across two repos |
| Real tests added and passed | ✅ 299 (runtime) + 27 new / 77 related (Owner OS) |
| Main contains the implementation | ✅ `deec322` |
| Production service restarted successfully | ✅ `ai-runtime.service` active; `seo-backend-1` healthy |
| MCP exposes the agent tools | ✅ 8/8 via `tools/list` (client reconnect required — §6) |
| Existing safeguard discovered read-only | ✅ §7 |
| No duplicate Claude or tmux session created | ✅ `duplicate_created=false`, 9→9 sessions, PID unchanged |
| Fallback plan-only jobs can no longer report completed | ✅ §8, enforced at `_finish` |

---

## 10. Rollback

**Runtime** (reverts control plane + truthfulness fix):

```bash
cd /root/ai-dev-runtime
git reset --hard backup/pre-agent-control-plane-20260717   # f7d9edf
systemctl restart ai-runtime.service
curl -s http://172.17.0.1:8199/health
```

**Owner OS / MCP** (removes the eight tools):

```bash
cd /opt/seo
git revert --no-commit 8b80176 0a4b036     # or: git checkout e5eb90a -- backend/services/
docker compose build backend && docker compose up -d backend
docker exec -w /app seo-backend-1 python -c "from services import mcp_server as m; print(len(m.tool_schemas()))"   # expect 26
```

Roll back **Owner OS first, then the runtime**: the MCP tools depend on the
runtime's routes, not the reverse. Reverting only the runtime would leave eight
tools proxying to routes that no longer exist. Nothing else depends on the new
routes, and no database migration was involved — `agent_control.db` (idempotency
keys) and `agent_control.jsonl` (audit) are new files that can be deleted.

---

## 11. Unresolved blockers and open items

1. **The MCP tools are committed on an Owner OS feature branch, not its master.**
   `/opt/seo` sits on `repair/owner-os-runtime-e2e-20260714`, which carries
   unrelated in-flight work, and its `master` is behind. The deployment is *not*
   affected — `docker compose build` builds from the working tree, so production
   runs this code now — but the commits are not on `master`. **This needs an owner
   decision**; merging that branch would have swept in unrelated changes I did not
   review, so I did not do it. The task's "merge to main" was executed for
   `/root/ai-dev-runtime`, the repository it named.

2. **Safe Guard's directory is outside the control plane's allowlist.** Its cwd is
   `/root/safeguard-remote-agent`, while `AGENT_CONTROL_ALLOWED_ROOTS` defaults to
   `/root/ai-dev-runtime,/opt`. Read tools (`agent_list`/`agent_status`/`agent_read`)
   work on it — they address panes, not paths — but `agent_resume` and
   `agent_report` would refuse it. To enable those, set on `ai-runtime.service`:
   `AGENT_CONTROL_ALLOWED_ROOTS=/root/ai-dev-runtime,/opt,/root/safeguard-remote-agent`.
   Left unchanged deliberately: widening a security allowlist is the owner's call.

3. **Client reconnect required** — §6. Server-side work is complete; the open
   ChatGPT session caches its tool list from connection time.

4. **`/opt/seo` has pre-existing uncommitted changes** (`backend/services/notifications.py`,
   untracked report files) that predate this task. Left untouched; only the four
   files listed in §2 were committed.

5. **The task's premise about repository layout was wrong** (§1.2). Implementing
   it literally — putting tmux control inside `/root/ai-dev-runtime` and expecting
   MCP to expose it from there — is impossible, because the MCP server is not in
   that repository and cannot see tmux. The two-repo split is the resolution, and
   is what is deployed.
