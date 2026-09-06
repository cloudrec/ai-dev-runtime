# Owner OS — the MCP control path, and self-project supervisor resumption

2026-09-06. Two acceptance criteria, taken in order. Root cause proven with durable
evidence before anything was changed; every measurement below was taken from a live
system or a matched A/B, and nothing is recorded as a pass that was not observed.

An automated instruction via the Owner OS API drove this session. That is **not** owner
sign-off, and nothing here is recorded as owner approval.

---

## Criterion 1 — the MCP control path

### Verdict

**The defect is in `ai-dev-runtime`, and it is not an error path.** Every one of
`agent_status` / `agent_read` / `agent_send` / `agent_answer` was declared `async def`
while calling straight into `core.agent_control`, which blocks: `subprocess.run` against
tmux with `_TMUX_TIMEOUT = 15`, plus a hard `time.sleep(0.4)` on the delivery path. On a
single-worker uvicorn that work runs **on the event loop**, so one control-plane call
freezes every other request in the process — including unrelated ones. Upstream, the MCP
client times out and reports a JSON-RPC failure while the server is still busy and
eventually answers `200`.

That is why the failures looked intermittent and why the server logs looked clean: they
**are** clean. The failure is latency, and the server never records it as an error.

### What was ruled out first

The HTTP layer is healthy. Whole-log histogram of `/var/log/ai-runtime/service.log` for
the four routes:

```
20949  GET  /api/v1/agents/status  200        723  GET  /api/v1/agents/status  400
11864  GET  /api/v1/agents/read    200        108  GET  /api/v1/agents/status  401
 5892  POST /api/v1/agents/send    200         19  GET  /api/v1/agents/read    400
 3808  POST /api/v1/agents/answer  200         11  POST /api/v1/agents/send    400
                                                4  POST /api/v1/agents/answer  400
                                                1  POST /api/v1/agents/answer  500
```

42,513 × 200 against ~886 non-200 (2.0%), and the non-200s are not the reported symptom:

* the **400s** are `AgentControlError` refusals for targets that no longer exist. The
  audit log names them — `mess-qa-automation:0.0` (2713), `arbitrage2-opus:0.0` (2679),
  `cp-canary:0.0` (2489) — all internal pollers on dead panes. `agent_status` returns
  `found=false` at **200**, so these 400s are a separate, smaller population.
* the **401s** are auth, not transport.

So the reported failure is not visible in status codes at all. It had to be measured.

### The measurement — live production, read-only

`/health` (a trivial synchronous handler) sampled at 50 Hz against the running service on
`172.17.0.1:8199`, while six concurrent `agent_read` calls ran. Nothing was mutated.

```
health, ambient traffic only   n=324  p50   7.8ms  p90  23.1ms  p99  67.4ms  max 4028.6ms
health, during 6x agent_read   n= 62  p50 213.3ms  p90 322.5ms  p99 385.3ms  max  385.3ms
health, after                  n=296  p50   8.0ms  p90  22.9ms  p99  53.6ms  max   66.3ms
```

p50 rises **27×** under six concurrent reads, and the sampler itself is starved — 324
samples in the idle window against 62 in an equally long loaded one. The `max 4028.6ms`
in the *ambient* row is the important one: with no load applied at all, a trivial endpoint
took **four seconds**, because the loop was inside somebody else's tmux call.

### The A/B — matched worktrees, pre-fix vs post-fix

Both sides: the real `api.v1` router with **no startup workers** (a throwaway app, so no
orchestrator or watchdog loop in that process could actuate an agent), on spare ports,
production service untouched. Probe is `/openapi.json` — no DB, no auth, pure loop
responsiveness. Six concurrent `agent_read` for 20s. `before` = `HEAD` (b615fbc),
`after` = the same tree plus this change.

| | before | after |
|---|---|---|
| probe idle p50 | 15.3 ms | 11.4 ms |
| probe under load p50 | **220.2 ms** | **30.3 ms** |
| probe under load p99 | 787.1 ms | 123.5 ms |
| probe under load max | 787.1 ms | 194.9 ms |
| probe samples/sec under load | 3.7 | 16.2 |
| `agent_read` p50 | 228.2 ms | 85.9 ms |
| `agent_read` p99 | 788.5 ms | 210.8 ms |
| `agent_read` throughput | 23.5/s | 64.7/s |

Loop-blocking p99 for unrelated requests falls **6.4×**; control-plane read throughput
rises **2.75×**. Both runs returned `200` for every request.

### The fix — two parts, both narrow

**1. `api/v1.py` — the five control-path handlers are `def`, not `async def`.**
Starlette then runs them in its threadpool instead of on the event loop. `agent_list` is
included because it shares the path and is its highest-volume caller by far (350,536 calls
in the audit window against 56,212 statuses); the other four cannot be reliable while it
stalls the loop. The remaining ~30 agent routes are **unchanged**.

**2. `core/agent_control.py` — a reentrant `_DELIVER_LOCK`.**
This is the part that makes the first part safe. Two things were being serialised by the
event loop *by accident*, and moving to the threadpool makes both reachable:

* `_deliver` is check-then-act — `_seen_delivery(key)` reads the idempotency key, the paste
  happens, `_record_delivery(key, …)` writes it. Two callers replaying one key would both
  read "unseen" and both paste into the pane.
* `agent_send`'s anti-queue guard is check-then-act for the same reason — two concurrent
  sends to one pane would both read "not busy", and the second would stack behind the turn
  the first had just started. That is exactly the queued-message failure the guard exists
  to prevent.

The lock covers both, and covers the in-process worker loops too, since they call the same
functions. Reentrant because `agent_send` holds it across the busy check and then calls
`_deliver`, which takes it again.

### Tests, and proof they would have caught it

`tests/test_mcp_control_path_concurrency.py`, 12 tests. Removal proof — each half reverted
in turn, suite re-run, then restored:

| reverted | result |
|---|---|
| `agents_read` back to `async def` | **2 failed** — `test_control_path_handler_is_not_a_coroutine[agents_read]`, `test_the_route_table_serves_the_sync_handlers` |
| both `_DELIVER_LOCK` acquisitions | **3 failed** — `test_concurrent_deliveries_never_overlap`, `test_one_idempotency_key_delivers_once_under_concurrency`, `test_a_second_concurrent_send_to_one_pane_is_still_refused` (4 messages pasted into one pane instead of 1) |
| nothing (restored) | 12 passed |

One honest note on how that proof was reached: the first version of the lock removal proof
**passed vacuously**. The fixture stubbed the 0.4s redraw wait via `ac.time.sleep`, and
`ac.time` *is* the `time` module — so it also silenced the delay in the fake tmux, and
nothing overlapped whether the lock was there or not. The stub was removed and the reason
is recorded in the fixture, so it is not re-introduced.

---

## Criterion 2 — self-project supervisor resumption

### Verdict

**Proven, from durable records, with one attribution gap now closed for future runs.**

The prior handoff recorded this as open: "the wake reaches the bound chat and a turn
starts; whether the supervisor then continues the work is unobserved." It is observable —
the records were already there, across three tables, and had not been correlated.

### The chain, event 33823

Self agent `owner-os-opus-fresh:0.0`, project `ai-dev-runtime`, route `owner-os`.

| # | when (UTC) | what | where it is recorded |
|---|---|---|---|
| 1 | 19:03:16.535 | event 33817 `agent_waiting_input`, severity high | `event` |
| 2 | 19:04:06.080 | event 33823 `work_stopped_incomplete`, severity high, `actionable=1` | `event` |
| 3 | 19:04:06.111 | wake decision `skip` — `actionable_cooldown_active` | `wake_audit` 136272 |
| 4 | 19:06:02.554 | wake decision **`wake`** — **`actionable_waiting_transition`** | `wake_audit` 136282 |
| 5 | 19:09:48.921 | **delivered=1**, `submitted_and_assistant_started_generating`, conversation `6a967789-9b28-83ed-9261-de5162d4ac17`, route `owner-os` | `wake_delivery` 11809 |
| 6 | 19:09:49.031 | wake acknowledged | `wake_audit` 136282 |
| 7 | 19:09:59.036 (**+10.1s**) | `agent_send` → `owner-os-opus-fresh:0.0`, key **`owner-self-handoff-33823-continue`**, actor `api:bearer`, source `172.20.0.2:49860 ua=python-httpx/0.27.0` | `deliveries` + `delivery_attribution` |
| 8 | — | watch resolved **`pane_alive_and_working`** | `wake_loop_watch` 33823 |
| 9 | — | **no** `owner_intervention_log` row for this target that day | `owner_intervention_log` |

Against the criterion, line by line:

* **self agent reaches actionable waiting/idle** — steps 1-2, `actionable=1`.
* **wake delivered to the bound ChatGPT supervisor** — step 5. The conversation is the
  `owner-os` route, bound `by owner` on 2026-09-02, and the reason field says the
  assistant *started generating*, not merely that text was submitted.
* **supervisor performs a safe continuation, pane returns to working** — steps 7-8. The
  source is the connector path (`172.20.0.2`, the seo backend), not a local worker.
* **without an owner message** — step 9, and the +10.1s lag is machine speed.

The route conversation is owner-bound; `bound_by='owner'`, note "owner-typed URL
2026-09-02". That binding is owner provenance recorded earlier, not a claim about this
session.

### Not an isolated case

Of 208 self-pane wake watches examined, **16** are followed by an `api:bearer` continuation
inside the 900s SLO, and **9** of those carry an idempotency key that *names the wake event
id* — `owner-os-fresh-safe-continue-35059` (+2.7s), `owner-os-fresh-no-push-34463`
(+15.7s), `owner-os-fresh-no-push-continue-34391` (+11.9s), `owner-os-fresh-continue-34378`
(+27.3s), `watch-finish-33996` (+36.5s, resolved `progress_observed`),
`self-project-watch-33817-20260905`, `self-wake-33487-idle-gap-observe-20260905`,
`self-wake-33368-safe-continue-20260905`, and 33823 above.

`SELF_WAKE_FLOW` and the `SELF_PROJECT` recursion denial are both intact and were not
touched. Verified live for the **current** self pane:

```
SELF_PROJECT        = ai-dev-runtime
is_self_agent("owner-os-opus-final:0.0")  = True
wake_route['owner-os'] -> chatgpt.com/c/6a967789-…  bound_by=owner
```

`_project_for_target` returns `''` for the current pane, exactly as the code comment
predicted; the `agent`-table fallback carries it, and that fallback is what makes
`is_self_agent` True. The guard holds by its fallback, not by its primary path — worth
knowing, not worth changing today.

### The gap that was real, and is now closed

Deliveries have carried `actor`/`source` since 2026-08-04. **Reads never did.** So
"the supervisor looked at the pane before continuing it" could only be *inferred* — from a
continuation key that names the event, which does imply its author had read the event, but
is not the same as observing the read.

`agent_status` and `agent_read` now accept keyword-only `actor`/`source` and record them,
and the two routes thread the caller through exactly as `send`/`answer` already did.
Attribution is **omitted entirely** when unset, so the in-process worker loops — which call
positionally and know nothing about actors — keep writing byte-identical audit lines.

Live-verified against the throwaway app:

```json
{"action": "agent_status", "target": "hostsecure:0.0", "found": true,
 "actor": "api:bearer", "source": "127.0.0.1:48152 ua=curl/8.5.0"}
{"action": "agent_read", "target": "hostsecure:0.0", "ok": true, "lines": 20,
 "actor": "api:bearer/chatgpt-supervisor", "source": "127.0.0.1:48160 ua=curl/8.5.0"}
```

From the next deploy on, step 7 above is preceded by attributed `agent_status` and
`agent_read` rows, and the inference becomes an observation.

---

## What is NOT claimed

* **CORRECTED 2026-09-06, after the worker loops were actually read.** An earlier
  revision of this report said the eight `asyncio.create_task` worker loops "still run
  blocking tmux work on the loop" and that converting them was "a larger change". **Both
  statements were wrong**, and they were written from the `create_task` call sites in
  `api/main.py` without opening a single loop body.

  What is actually true: **no worker loop runs blocking tmux work on the event loop.** All
  eight hand their tick to `asyncio.to_thread` and wait with `asyncio.sleep`. The
  `agent_send` calls in `agent_orchestrator` sit inside `refresh_and_resolve`, which is
  itself invoked via `to_thread` — so they run in a worker thread, and a worker waiting on
  `_DELIVER_LOCK` blocks that thread, not the loop.

  Two call sites had skipped the `to_thread` their siblings use, and both are now fixed
  (one line each, no refactor):
  * `agent_orchestrator.run_loop` called `wake_bridge.register_worker` inline — sqlite
    writes plus `_module_fingerprint`, which opens and SHA-256-hashes the worker's source
    files off disk, every 45s tick.
  * `wake_bridge.pipeline_watch_loop` called `pipeline_health` inline — sqlite reads,
    every watch interval.

  Pinned by `tests/test_worker_loops_off_event_loop.py`, which asserts on the thread the
  call lands on rather than on behaviour.
* **Threadpool capacity is now the bound.** Starlette's default limiter is 40 threads. A
  flood of concurrent `agent_read` will queue there rather than on the loop — better
  behaviour, but still a bound, and it has not been load-tested to that edge.
* **The fix is not live.** The running service (PID 1196430, up since 2026-09-05 06:05:35)
  still executes the pre-fix code. Deploying it needs `systemctl restart ai-runtime`, which
  this session treated as an owner gate and did not do. Everything above is proven in the
  repo, in tests, and in a matched A/B — not in production.
* **The 33823 chain is historical**, recorded 2026-09-05 on the predecessor self pane
  `owner-os-opus-fresh:0.0`. It is durable and re-queryable, but it was not staged by this
  session.
* **The pane did not stay working.** After 33823 resumed it, further
  `agent_waiting_input` events followed at 19:11:30, 19:18:20, 19:19:58, 19:23:17. The
  criterion is resumption without an owner message, and that is what is shown; a claim of
  sustained autonomy is not.
* **Read attribution for the 33823 window does not exist** and cannot be back-filled —
  the code that records it did not run then.
* **No JSON-RPC message was observed directly.** The MCP server is not in this repository
  (`reports/OWNER_OS_MCP_NOTIFICATIONS_BOUNDARY_2026-09-03.md`); it reaches this runtime
  over HTTP from `172.20.0.2`. The root cause is proven on this side of that boundary — the
  latency that a JSON-RPC client would time out on — not by reading the client's error.
* `/opt/seo` and every other project tree were **not touched**.

## Reproducing the measurements

```bash
# live, read-only, against the running service
venv/bin/python <scratch>/probe2.py

# matched A/B (both sides in throwaway worktrees, no startup workers)
git worktree add --detach <wt> HEAD          # before
cp api/v1.py core/agent_control.py <wt2>/…   # after
uvicorn abapp:app --port 829N                # api.v1 router only
venv/bin/python <scratch>/abprobe.py 829N
```

Scratchpad scripts do not survive the session; the numbers they produced are above, and
the durable inputs — `service.log`, `agent_control.jsonl`, `control_plane.db`,
`agent_control.db` — are all on disk.
