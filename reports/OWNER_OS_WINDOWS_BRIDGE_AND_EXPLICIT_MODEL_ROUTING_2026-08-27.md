# Owner OS — Windows bridge + explicit model routing (task 220 / runtime job #81)

Date: 2026-08-27 · Session: manual Opus 5 · Branch: `ai-runtime/220-windows-bridge`
(cut from `ai-runtime/182-retry-fix-wake-continuation-star` @ `daeda13`)

Three things were delivered: the explicit model-routing defect that killed jobs
#219/#81 is fixed at its actual cause, the missing runtime endpoint behind the
retry path's HTTP 404 is implemented, and the Windows Owner OS bridge is built
end to end with a working local simulation.

---

## 1. Explicit model routing — the real defect

**Symptom.** "Dispatch this on Opus" never worked; the dispatcher always chose
Sonnet (task #219 cancelled for it, #220/#81 tried and failed to fix it).

**Cause — two separate gaps, both real:**

1. `core/model_router.py:route()` had **no input for a requested model at all**.
   Its only inputs were `task_class`, `risk` and `prior_attempts`, so a caller
   who wanted Opus could only hope the static partition landed there. There was
   nothing to fix in the caller — the capability did not exist.
2. `core/job_executor.py:_route_model()` reads `job["escalation_reason"]`, which
   is the ONLY way past the task-213 hard gate — but `escalation_reason` **was
   never a column on the `jobs` table**. `create_job()` dropped it, so every job
   re-read from the store arrived without it and was de-escalated to Sonnet.
   The existing test passed because it set the field on an in-memory dict and
   never round-tripped it through the store.

**Fix.**

* `route(..., explicit_model=...)` — an explicit tier request that is an INPUT
  to the policy, never a bypass of it:
  * asking **up** raises the decision, then still has to clear the task-213 hard
    gate (category + evidence + expected_benefit + context_pack). An unjustified
    "give me opus" de-escalates to sonnet with the refusal recorded.
  * asking **down** is refused whenever a money/security/high risk floor or a
    prior-attempt escalation is what put the decision where it is.
  * `explicit_model` / `explicit_granted` are recorded on `router_decision`.
* `jobs.requested_model` and `jobs.escalation_reason` are now durable columns
  (additive `ALTER TABLE`, forward-only, matching the existing migration idiom).
* `POST /api/v1/jobs` accepts `requested_model` + `escalation_reason`;
  `POST /api/v1/router/route` accepts `explicit_model`; the job view returns
  `requested_model`.

The safety properties of task 213 are unchanged: automated dispatch still never
sets an escalation reason, so routine work still lands on Sonnet.

## 2. The retry path's HTTP 404 — root cause and fix

`/opt/seo/backend/services/runtime_client.py:141` gates every runtime retry on
`POST {RUNTIME_URL}/smoke`, and its own comment cites *"ai-dev-runtime's POST
/api/v1/smoke (core/ai_planner.smoke)"* — **but that route was never implemented
here.** `core/ai_planner.smoke()` (PHASE 45) existed; nothing exposed it. Every
retry therefore failed its gate with 404 and dispatched no replacement job
(job #81 now, job #72 in `reports/OWNER_OS_WAKE_CONTINUATION_HANDOFF_2026-08-13.md`).

Implemented `POST /api/v1/smoke` in `api/v1.py`: auth-gated, read-only by
construction (no `project_path`, no tools, one hard-capped non-agentic call, no
internal retry), returning the exact contract the caller reads. A provider
failure is a 200 with `ok=false`, never an HTTP error — the gate reads the body,
and a 500 would be indistinguishable from the 404 it replaces.

**No provider config was touched.** The seo-side caller was inspected read-only.

## 3. Windows Owner OS bridge

Full design and setup: `docs/OWNER_OS_WINDOWS_BRIDGE.md`.

* **Outbound only.** The PC long-polls Owner OS over HTTPS. No listening socket,
  no port forward, no firewall rule on Windows.
* **Per-device identity.** Single-use, expiring, hash-stored enrollment code →
  device id + 256-bit secret. Every later request is HMAC-SHA256 over
  `oos-win-v1 | device | ts | nonce | path | sha256(body)`. Rotatable, revocable.
* **Replay-proof.** ±300 s window *and* a burned per-device nonce; binding path
  and body hash means a captured signature cannot be re-pointed.
* **No remote shell.** Six allowlisted actions, closed parameter vocabulary,
  unknown params refused rather than ignored.
* **No paths on the wire.** Commands name a workspace id; the device resolves it
  against its own local enrollment file. The server cannot express a path.
* **No command injection.** Claude runs as an argv list with the prompt on
  stdin — `claude` on Windows is a `.cmd` whose arguments cmd.exe re-parses.
* **Explicit enrollment.** A folder is reachable only after `add-workspace` is
  run ON the PC. The server can disable a workspace, never add one.
* **Idempotent + bounded + redacted.** UUID command ids, 16 KB prompts, 256 KB
  results, 15-minute TTL, `agent_control.redact()` applied inside the structure.
* **Fabric integration.** `win:<device>:<workspace>` joins `tmux:` and
  `runtime:` in `GET /fabric/agents`, with `platform` explicit on every entry
  (`linux` | `windows`). tmux behaviour is unchanged.

### A concurrency defect the simulation caught

The owner-side wait originally ran its blocking poll loop **inside the async
endpoint**, which starved the event loop serving the device's own long-poll:
the only party that could answer the command was blocked behind the handler, so
every command timed out. `/windows/command` and the fabric's `win:` verbs now
run their wait via `asyncio.to_thread`; tmux/runtime refs keep their exact inline
path. Pinned by `test_owner_command_does_not_block_the_event_loop`.

## 4. Files

**New**

| Path | Role |
| --- | --- |
| `core/windows_bridge.py` | server half: enrollment, device auth, allowlist, queue, inventory |
| `clients/windows/owner_os_agent.py` | device half: stdlib-only agent, workspace resolution, Claude session runner |
| `clients/windows/install.ps1` | one-command bootstrap (PS 5.1 compatible): prereqs, enroll, workspace, ACLs, scheduled task |
| `tools/windows_bridge_sim.py` | narrated end-to-end simulation on 127.0.0.1 |
| `docs/OWNER_OS_WINDOWS_BRIDGE.md` | architecture, security table, setup, endpoints, rollback |
| `tests/test_windows_bridge.py` | 47 server tests |
| `tests/test_windows_client.py` | 38 device tests |
| `tests/test_windows_fabric.py` | 20 fabric tests |
| `tests/test_windows_e2e.py` | 3 (deadlock regression + full simulation) |
| `tests/test_explicit_model_routing.py` | 26 (explicit routing, durability, the 404) |

**Modified**

| Path | Change |
| --- | --- |
| `core/model_router.py` | `explicit_model` input + audit columns |
| `core/job_store.py` | `requested_model` / `escalation_reason` columns + migration |
| `core/job_executor.py` | passes the persisted explicit selection into the router |
| `core/agent_fabric.py` | `win:` ref kind, windows inventory, explicit `platform` |
| `api/v1.py` | `POST /smoke`, 12 `/windows/*` routes, explicit-model fields, event-loop offload |

## 5. Tests

* Focused: **134 passed** (`test_windows_bridge`, `test_windows_client`,
  `test_windows_fabric`, `test_windows_e2e`, `test_explicit_model_routing`).
* Broad relevant: **425 passed** (`agent_fabric`, `model_router`,
  `runtime_model_routing`, `model_routing`, `job_executor`, `phase13`,
  `agent_control`, `control_plane`, `api_wake_routes`, `owner_os_policy`,
  `owner_os_adversarial`, `agent_supervisor`, `agent_orchestrator`,
  `direct_pane_control`, `direct_agent_lifecycle`, `job_kinds`, `job_workspace`,
  `runtime_bridge`).
* Full suite run in halves (a single run exceeds the 600 s cap — the same limit
  that failed job #81, not a defect in this work).
* `venv/bin/python tools/windows_bridge_sim.py` → SIMULATION PASSED (10 steps).

## 6. Activation (server) — nothing here is live yet

No service was restarted and no config was changed. To activate:

1. Merge/deploy this branch to `/root/ai-dev-runtime`.
2. `systemctl restart ai-runtime.service` — this is the ONLY step. The `win_*`
   tables and the two `jobs` columns are created on first touch by the existing
   additive-migration paths.
3. Verify: `curl -H "Authorization: Bearer $RUNTIME_TOKEN" .../api/v1/windows/policy`
   and `.../api/v1/smoke` (the latter also closes the retry-path 404).

**Rollback:** revert the branch and restart; or leave the code in place and
never enroll a device — every `/windows/*` route is inert without one. The new
columns and tables are additive and harmless if unused.

## 7. Owner gates (what is left)

1. **Deploy + restart `ai-runtime.service`** — deliberately not done here.
2. **Run the Windows command** on the PC (needs a code minted at that moment;
   codes expire in 15 minutes):

   ```powershell
   powershell -ExecutionPolicy Bypass -File install.ps1 `
     -Server https://<owner-os> -Code OOS-XXXXX-XXXXX-XXXXX `
     -WorkspacePath "C:\Users\0962871647\Desktop\GAIKA_Basket_Chrome_Extension_MVP_v0.1.0\gaika-basket-extension"
   ```

   Prereqs if missing: `winget install -e --id Python.Python.3.12` and
   `npm install -g @anthropic-ai/claude-code`.
3. **HTTPS reachability** — the PC must be able to reach Owner OS on a TLS
   endpoint. The installer refuses a non-HTTPS server without `-Force`.

## 8. Known blockers, unchanged

* **Runtime retry remains unproven end to end.** The 404 cause is fixed in this
  branch, but the fix is not deployed, so the retry path stays unavailable until
  gate 1 above. Do not re-trigger a runtime retry before then.
* The full pytest run still exceeds 600 s in one pass; run it in halves.

---

# Deployment record — 2026-08-27 18:02 UTC

Deployed and verified live on the Owner OS server. Owner-authorized; no
destructive step was taken.

**Deployed:** `ai-runtime/220-windows-bridge` @ `33cd3f2`, local == remote.
`ai-runtime.service` runs `WorkingDirectory=/root/ai-dev-runtime` directly, so
the checkout IS the deploy and the restart is what activates it. `git diff HEAD`
over `api/ core/ clients/ tools/ tests/ docs/` was empty — the running tree is
byte-identical to the tested commit. 29 unrelated dirty/untracked `reports/*`
files were left untouched.

**Rollback point:** `backups/predeploy_win_bridge_20260827T160240Z/` — copies of
`runtime_jobs.db`, `control_plane.db`, `agent_control.db` plus `ROLLBACK.md`.
Code rollback alone is sufficient (`git checkout
ai-runtime/182-retry-fix-wake-continuation-star && systemctl restart
ai-runtime.service`); the schema changes are additive and the previous code
ignores them.

**Restart:** zero in-flight jobs beforehand. PID 1586787 → 4042415, `NRestarts=0`,
clean shutdown/startup, all six workers back (supervisor, orchestrator, control
plane, continuation watchdog, commander autopilot, context budget). Zero
errors/tracebacks in the log since restart.

**Post-restart checks**

| Check | Before | After |
| --- | --- | --- |
| `GET /api/v1/health` | ok | ok, `provider_available: true`, 100 jobs |
| `POST /api/v1/smoke` unauthenticated | **404** | 401 (route exists, auth-gated) |
| `POST /api/v1/smoke` authenticated | — | **`ok: true`**, claude-cli, 5.53 s |
| `GET /api/v1/windows/policy` | **404** | 401 unauth / full surface with auth |
| `GET /api/v1/windows/devices` | — | `{"devices": []}` |
| `GET /api/v1/fabric/agents` | — | 23 agents (11 tmux + 12 runtime), **no errors** |
| explicit opus, no justification | — | de-escalates to **sonnet**, gate `false` |
| explicit opus, justified | — | reaches **claude-opus-5**, gate `true` |
| `jobs.requested_model` / `escalation_reason` | absent | present, 100 rows untouched |
| `win_*` tables | absent | all five created |

**The retry-path 404 is closed.** The exact call that failed every Runtime retry
now returns `ok: true` against the real provider.

## Remaining owner gate — network ingress (blocks the Windows step)

`ai-runtime.service` listens on **172.17.0.1:8199 only** (docker bridge, plain
HTTP). nginx serves :80/:443 on this host but **nothing proxies to 8199**, and no
`server_name` maps to the runtime API. The Windows PC therefore cannot reach
Owner OS at all yet, and the agent refuses a non-HTTPS server by design.

Publishing a control-plane API to the public internet is an outward-facing
security decision and a config change, so it was NOT made here. The owner must
choose one:

1. **Public HTTPS reverse proxy** — an nginx `server_name` (e.g.
   `owneros.<domain>`) with a certificate, `proxy_pass http://172.17.0.1:8199;`,
   ideally restricted to `/api/v1/windows/` and `/api/v1/health`. Widest
   exposure; only the device-authenticated routes need to be public.
2. **Tailscale / WireGuard** — the PC joins a private network and reaches the
   API on its tailnet address. No public exposure at all; recommended.
3. **Cloudflare Tunnel** — no inbound firewall change on this host.

Enrollment cannot be completed until one of those exists. Everything on the
server side is otherwise ready and idle: no device is enrolled, and every
`/windows/*` route is inert without one.

---

# Tailscale connectivity — 2026-08-27 18:2x UTC

The ingress gate from the deployment record is now closed, without publishing
anything to the internet.

**Tailscale was already installed, logged in and running** on this server
(`tailscale0` = `100.108.182.33`, tailnet `tail9bce4e.ts.net`, node
`polyinput-server`). No login, no auth key and no device enrollment was needed
or performed. The owner's Windows PC is **already a member of this tailnet**:
`DESKTOP-23PUSRG` (`100.116.241.62`, last seen 13 days ago; an older record
`100.95.119.118` also exists). It is currently offline.

**HTTPS certs are unavailable on this account** — `tailscale cert` returns
*"your Tailscale account does not support getting TLS certs"*, so
`tailscale serve --https` is not possible. Enabling HTTPS certificates in the
admin console is an owner/account decision and was not taken.

**What was configured (one command, reversible):**

```bash
tailscale serve --bg --http=8199 --set-path=/api/v1/windows \
  http://172.17.0.1:8199/api/v1/windows
```

`http://polyinput-server.tail9bce4e.ts.net:8199/api/v1/windows` → the runtime
API, tailnet only.

**Verified**

| Check | Result |
| --- | --- |
| Enrollment through the tailnet proxy | works (throwaway device, then revoked + deleted) |
| Signed poll through the proxy | **accepted** — the HMAC path binding survives the proxy |
| `/api/v1/jobs`, `/agents`, `/health` over tailnet | **404** — only the device routes are mounted |
| Funnel (public exposure) | off; serve reports "tailnet only" |
| Routing table | byte-identical to the pre-change capture |
| iptables rules | 0 before, 0 after |
| Exit node / advertised routes / RouteAll / ShieldsUp | unchanged (empty / false) |
| nginx | untouched, still no reference to 8199 |
| Direct `172.17.0.1:8199` path | intact (`/health` ok) |
| `ai-runtime.service` | NOT restarted (PID 4042415, NRestarts=0) |
| Wake pipeline | enabled, kill switch off, last delivery ok |
| Fabric | 23 agents, no errors |
| Registry after cleanup | 0 devices, 0 workspaces, 0 open enrollment codes |

**Client support.** `install.ps1` previously refused any non-HTTPS server. It
now accepts `http://` for Tailscale addresses only (`*.ts.net` or
`100.64.0.0/10`), because tailnet traffic is already WireGuard-encrypted and
peer-authenticated — the same guarantee TLS provides, one layer down. Plain
HTTP to anything else is still refused. The CGNAT range check is pinned by 11
tests so the exception cannot quietly widen. The installer also pre-checks
tailnet reachability and fails with a clear message if Tailscale is down on
the PC.

**Rollback:** `tailscale serve --http=8199 off`.

## Next Windows step (owner)

1. Bring `DESKTOP-23PUSRG` online and confirm Tailscale is up on it
   (`tailscale status` should list `polyinput-server`).
2. Copy `clients/windows/owner_os_agent.py` and `clients/windows/install.ps1`
   to the PC.
3. Mint a code on the server (single use, expires — mint it at install time):

   ```bash
   curl -sS -X POST http://172.17.0.1:8199/api/v1/windows/enroll-code \
     -H "Authorization: Bearer $RUNTIME_TOKEN" -H 'Content-Type: application/json' \
     -d '{"label":"owner windows pc","ttl_secs":3600}'
   ```

4. On the PC, in the folder holding both files:

   ```powershell
   powershell -ExecutionPolicy Bypass -File install.ps1 `
     -Server http://polyinput-server.tail9bce4e.ts.net:8199 `
     -Code OOS-XXXXX-XXXXX-XXXXX `
     -WorkspacePath "C:\Users\0962871647\Desktop\GAIKA_Basket_Chrome_Extension_MVP_v0.1.0\gaika-basket-extension"
   ```

The device will then appear in `GET /api/v1/windows/devices` and as
`win:win-<id>:gaika-basket-extension` in `GET /api/v1/fabric/agents`.

---

# Stuchalka (wake) repair + GAIKA reconciliation — 2026-08-27 evening

## Wake: three root causes, fixed and live

All three were separate reasons a project agent could not ring its own ChatGPT
chat, forcing the owner to relay by hand.

1. **Session-vs-project key mismatch.** `agent_watcher` labels a transition with
   the tmux SESSION (`payorch-live-buttons`, `chemmy-fast`), while `wake_route`
   is keyed by the PROJECT (`payment-orchestrator`, `mess`). The keys never
   matched, so every project wake fell through to the owner-os control chat.
   The mapping already existed in the control plane's own `agent.project_id`.
   `wake_routes.resolve()` now consults it — a lookup, not a guess: an ambiguous
   session refuses, a missing registry degrades to previous behaviour.

2. **Dead marks were permanent.** A dead route is never selected for delivery
   and only a delivery clears the mark, so one transient
   `composer_did_not_clear_after_send` pair silenced `gaika-video` for twelve
   days. `wake_bridge` already exempted owner-os from this self-locking gate;
   project routes were left inside it. Marks now expire after
   `WAKE_DEAD_ROUTE_RETRY_SECS` (1h) and earn one retry per window.

3. **Cooldowns were global.** Both floors queried the most recent wake for ANY
   project, so the busiest chat silenced every other one — owner-os traffic alone
   accounts for most of 17,289 `cooldown_active` skips in fourteen days. Both are
   now matched on the decision's resolved route; `wake_audit.route_key` was added
   (additive) to make that possible, with legacy NULL rows counted as owner-os so
   that chat keeps exactly its current protection.

**Live evidence, production service:** four chats each cleared their own floor
seconds apart (owner-os / payment-orchestrator / mess / treasure) where a global
floor would have passed only the first; `payorch-*`, `chemmy-fast`,
`treasure-*`, `jobhunter-media-audit` now resolve to their own conversations;
`gaika-video` recovered from its dead mark. End-to-end: event 9864 decided at
17:46:34 with `route_key` persisted, delivered 17:47:00
(`submitted_and_user_turn_appeared`). Commits `076e096`, `e525c4a`; 271 wake
tests pass; no route was rebound (`wake_route_audit` today = 0).

## Notification tiers are RED — one owner gate

Event 9800 (`notification_dead_letter`) and 9864 (`notifications_red`) share one
cause. The bot token is VALID (`@ezzetasecurity_bot`), but `getChat` with the
configured `TELEGRAM_CHAT_ID` returns `400 Bad Request: chat not found`, so
every Telegram notification dead-letters after 5 attempts — 30 in 48 hours,
100%. The other two tiers are red by configuration, not by fault:
`same_chat_wake` has no inbound trigger and `scheduled_chatgpt` is disabled.

This is separate from the ChatGPT wake path, which is healthy and delivering.

**Owner action:** send `/start` to `@ezzetasecurity_bot` from the account that
should receive alerts; the correct chat id can then be read from `getUpdates`
and set in `configs/.env`. Not done here — credentials are an owner gate.

## GAIKA reconciliation — no unique Windows work

The owner's archive (`gaika-basket-extension.zip`, sha256 `c349f2a8…`) was
extracted read-only to `/tmp`; neither copy was modified.

It carries no `.git`. Hashing its 31 files against every commit in the server
history matches **`45082dd` "chore: baseline import of GAIKA extension v0.4.3"**
exactly — 31/31 byte-identical, 0 files differing from that baseline. The Windows
copy is a strict ANCESTOR of `/opt/gaika-extension` `main` (`f3c405b`), not a
divergence.

| Group | Count |
| --- | --- |
| identical to server HEAD | 18 |
| changed (server advanced, Windows did not) | 12 |
| server-only additions since the fork | 35 |
| Windows-only | 1 — `content/store-adapter.js` |

The single Windows-only file is a server-side RENAME, verified in git
(`R071 content/store-adapter.js -> content/gaika-core.js`), and the Windows copy
of it is byte-identical to the pre-rename version. **Nothing to merge, nothing
to salvage.**

Recommended (all owner-gated, none performed): tag `45082dd` as the Windows
snapshot; give `/opt/gaika-extension` a backup remote — 33 commits currently
exist in one place with no remote, and `cloudrec/gaika` is a DIFFERENT project
(docs/investor material), so it is the wrong destination; move the ZIP out of
the working tree before it lands in a milestone archive.
