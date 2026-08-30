# Owner OS Windows Bridge

Control the Claude Code sessions running on the owner's Windows PC from Owner OS,
with the same verbs already used for the tmux agents on the server — and without
opening a single inbound port on Windows.

Task 220. Server half: `core/windows_bridge.py`. Device half:
`clients/windows/owner_os_agent.py`. Inventory/verbs: `core/agent_fabric.py`.

---

## 1. Shape

```
   Windows PC (behind NAT, no open ports)          Owner OS server
   ┌────────────────────────────────────┐          ┌──────────────────────────┐
   │ owner_os_agent.py                  │          │ /api/v1/windows/*        │
   │  • long-polls, outbound HTTPS ─────┼─────────▶│   enroll / poll / result │
   │  • resolves workspace_id → path    │◀─────────┼─── leased commands       │
   │  • runs: claude -p --resume <sid>  │          │ core/windows_bridge.py   │
   │    (argv only, prompt on stdin)    │          │   durable command queue  │
   │  • enrolled folders ONLY           │          │ core/agent_fabric.py     │
   └────────────────────────────────────┘          │   win:<device>:<ws> refs │
                                                   └──────────────────────────┘
```

The device opens every connection. Control arrives as the *response* to a poll
the Windows machine itself started, so there is no listening socket, no port
forward and no firewall rule on the owner's PC.

## 2. Security properties

| Property | How |
| --- | --- |
| Per-device identity | Single-use, expiring enrollment code → device id + 256-bit secret. The code is stored only as a SHA-256 hash. |
| Request authentication | HMAC-SHA256 over `oos-win-v1 \| device \| ts \| nonce \| path \| sha256(body)`. |
| No replay | ±300 s freshness window **and** a per-device nonce that is burned on first use. Binding the path and body hash means a captured signature cannot be re-pointed at another route or payload. |
| Rotation / revocation | `POST /windows/rotate` (device-initiated, authenticated) and `POST /windows/devices/{id}/revoke` (owner). Revoking also expires that device's queued commands. |
| No remote shell | `windows_bridge.ACTIONS` is the entire surface: `workspace.list`, `agent.status`, `agent.read`, `agent.start`, `agent.send`, `agent.stop`. There is no "run command" verb; unknown params are refused, not ignored. |
| No path traversal | Commands name a **workspace id**, never a path. The device resolves that id against its own local enrollment file, so the server cannot express a path at all. |
| No command injection | Claude is spawned as an argv list with the prompt on **stdin**. On Windows `claude` is a `.cmd` shim whose arguments are re-parsed by cmd.exe (the "BatBadBut" class); stdin keeps the prompt as data. |
| Idempotency | Every command carries a UUID. Re-enqueuing an id returns the recorded result; a device re-posting a result is a no-op. |
| Redacted logs | Device results pass through `agent_control.redact()` **inside the structure** before storage — redacting a serialized document would corrupt its JSON. |
| Least privilege | The agent runs as the logged-in owner (not SYSTEM), reaches only enrolled folders, and its config is ACL-restricted to SYSTEM + Administrators + that owner. |
| Bounded everything | 16 KB per prompt, 256 KB per result, 2000 lines per read, 64 workspaces per device, 15-minute command TTL. |
| Explicit enrollment | A workspace becomes reachable only after the owner runs `add-workspace` **on the Windows machine**. The server can disable a workspace but can never add one. |

No owner secret is in this repository. The device secret exists only in the
control-plane database and in `%ProgramData%\OwnerOS\agent.json` on the PC.

## 3. Owner setup

### 3.1 On the server — mint a one-time code

```bash
curl -sS -X POST https://<owner-os>/api/v1/windows/enroll-code \
  -H "Authorization: Bearer $RUNTIME_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"label":"owner windows pc","ttl_secs":900}'
# -> {"code":"OOS-XXXXX-XXXXX-XXXXX", ...}   single use, 15 minutes
```

### 3.2 On the Windows PC — one command

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 `
  -Server http://polyinput-server.tail9bce4e.ts.net:8199 `
  -Code OOS-XXXXX-XXXXX-XXXXX `
  -WorkspacePath "C:\Users\0962871647\Desktop\GAIKA_Basket_Chrome_Extension_MVP_v0.1.0\gaika-basket-extension"
```

That checks Python 3.9+ and the Claude Code CLI, installs the agent to
`%ProgramData%\OwnerOS`, enrolls the device and the workspace, locks the config
down with `icacls`, and registers the `OwnerOSAgent` scheduled task (at logon,
auto-restart). Re-running is safe.

Prerequisites, if the script reports them missing:

```powershell
winget install -e --id Python.Python.3.12
npm install -g @anthropic-ai/claude-code
```

More workspaces later:

```powershell
python "$env:ProgramData\OwnerOS\owner_os_agent.py" `
  --config "$env:ProgramData\OwnerOS\agent.json" `
  add-workspace --id another-project --path C:\path\to\project
```

## 3.3 Reachability — Tailscale (how the PC finds the server)

The runtime API listens on `172.17.0.1:8199` (a docker-bridge address) and is
deliberately NOT published to the internet. The PC reaches it over the tailnet:

```
Windows PC ──WireGuard──▶ polyinput-server.tail9bce4e.ts.net:8199/api/v1/windows
                          └─ tailscale serve ─▶ http://172.17.0.1:8199/api/v1/windows
```

Configured with one command, already applied on the server:

```bash
tailscale serve --bg --http=8199 --set-path=/api/v1/windows   http://172.17.0.1:8199/api/v1/windows
```

What that does and does not do:

* **Tailnet only.** `tailscale serve` binds the Tailscale interface. Funnel
  (public exposure) is off, and nothing was added to nginx, the firewall, the
  routing table, DNS, or the exit-node/subnet-route settings.
* **Minimal surface.** Only `/api/v1/windows/*` is mounted. `/api/v1/jobs`,
  `/api/v1/agents` and even `/api/v1/health` return 404 over the tailnet — the
  owner-only routes stay reachable exclusively on the internal address.
* **Signatures survive it.** The proxy preserves the request path, so the
  HMAC's path binding still verifies. Proven by enrolling a throwaway device
  through the tailnet URL and completing a signed poll (device then revoked and
  deleted).
* **Plain HTTP is correct here.** Traffic inside the tailnet is already
  WireGuard-encrypted and peer-authenticated, which is the guarantee TLS would
  add, one layer down. `install.ps1` therefore accepts `http://` for `*.ts.net`
  and `100.64.0.0/10` hosts and keeps refusing it everywhere else
  (pinned by `tests/test_windows_client.py`).
* **HTTPS instead, if wanted.** `tailscale cert` currently fails with *"your
  Tailscale account does not support getting TLS certs"*. Enabling HTTPS
  certificates in the Tailscale admin console (DNS → HTTPS Certificates) would
  allow `tailscale serve --bg --https=443 ...` and an `https://` server URL.
* **Rollback:** `tailscale serve --http=8199 off` — the bridge becomes
  unreachable from the PC and nothing else changes.

## 4. Driving it from Owner OS

```bash
# what is out there
curl -H "Authorization: Bearer $RUNTIME_TOKEN" https://<owner-os>/api/v1/windows/devices
curl -H "Authorization: Bearer $RUNTIME_TOKEN" https://<owner-os>/api/v1/fabric/agents

# send a prompt (waits for the answer)
curl -X POST https://<owner-os>/api/v1/windows/command \
  -H "Authorization: Bearer $RUNTIME_TOKEN" -H 'Content-Type: application/json' \
  -d '{"device_id":"win-...","action":"agent.send","workspace_id":"gaika-basket",
       "params":{"text":"add a badge to the cart button"},"wait_secs":60}'
```

Through the fabric, a Windows workspace is just another ref:

| Ref | Meaning |
| --- | --- |
| `tmux:gaika-video:0.0` | Claude in a tmux pane on this server (`platform: linux`) |
| `runtime:<job-uuid>` | a Runtime worker (`platform: linux`) |
| `win:win-<id>:gaika-basket` | Claude in an enrolled folder on the PC (`platform: windows`) |

`GET /fabric/agents`, `/status`, `POST /send`, `/stop`, `GET /result` and
`POST /fabric/start-or-resume {"ref": "win:..."}` all work on Windows refs.
`platform` is explicit on every entry so the two are never confused, and the
tmux paths are byte-for-byte unchanged.

An offline laptop is reported as `alive: false`, and a command it never
collected comes back `timed_out` / `expired` with a reason — never as success.

## 5. Endpoints

Device-authenticated (HMAC, no `RUNTIME_TOKEN` — the PC never holds the
server's own token):

| Route | Purpose |
| --- | --- |
| `POST /api/v1/windows/enroll` | code → device id + secret (code is the credential) |
| `POST /api/v1/windows/poll` | heartbeat + workspace report → leased commands (long-poll) |
| `POST /api/v1/windows/result` | post one command's result |
| `POST /api/v1/windows/rotate` | rotate this device's secret |

Owner-authenticated (existing bearer/HMAC `_auth`):

| Route | Purpose |
| --- | --- |
| `POST /api/v1/windows/enroll-code` | mint a one-time code |
| `GET /api/v1/windows/devices` | devices + online state |
| `GET /api/v1/windows/workspaces` | enrolled workspaces |
| `POST /api/v1/windows/workspaces/enabled` | server-side off switch |
| `POST /api/v1/windows/command` | enqueue (and wait for) one allowlisted command |
| `GET /api/v1/windows/command/{id}` | one command's state/result |
| `GET /api/v1/windows/policy` | the whole remote surface, for audit |

## 6. Operating notes

* **Sessions.** Each workspace keeps one Claude session id. `agent.send`
  continues it (`claude -p --resume <sid>`); `agent.start` opens a new one.
  One session per folder, so there are no duplicate agents.
* **Busy is a refusal.** A second command while a turn is running is refused
  with `workspace busy`, not queued behind it.
* **Blocking waits are offloaded.** `/windows/command` and the fabric's `win:`
  verbs run their wait in a worker thread. Waiting on the event loop would
  starve the device's own long-poll and deadlock every command — see
  `tests/test_windows_e2e.py::test_owner_command_does_not_block_the_event_loop`.
* **Rollback.** Stop the scheduled task (`Unregister-ScheduledTask OwnerOSAgent`)
  or revoke the device server-side; either one ends all reachability. The
  `win_*` tables are additive and can be dropped without touching anything else.

## 7. Tests

| File | Covers |
| --- | --- |
| `tests/test_windows_bridge.py` (47) | enrollment, signing/replay/rotation/revocation, action allowlist, traversal-shaped ids, queue + idempotency, redaction, expiry |
| `tests/test_windows_client.py` (38) | local config, workspace resolution, prompt-on-stdin, session resume, bounded transcript, backoff, signature parity with the server |
| `tests/test_windows_fabric.py` (20) | refs, inventory with explicit platform, send/status/stop/result/start, offline and failure reporting, tmux untouched |
| `tests/test_windows_e2e.py` (3) | event-loop non-blocking regression + the full simulation |
| `tools/windows_bridge_sim.py` | the same simulation, standalone and narrated |
