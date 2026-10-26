# Owner OS sandbox worker test — 2026-10-27

Status: **prepared and isolation-proven; real Claude worker NOT started.** The request to
start one came from automated instructions that report an owner approval in chat; that is not
independently verified in this session, so starting the agent stays an owner action (below).

## Workspace

- `/opt/owneros-sandbox-worker-20261027` (moved from /root): `README.md`, `TASK.md`,
  `notes.txt` (`step 1 done`); git `main` @ `c00842d`, clean, no remote; no secrets.
- State (sandbox ledgers, audit, evidence): `/opt/owneros-sandbox-state-20261027`.
- Private tmux socket: `/opt/owneros-sandbox-tmux-20261027.sock` (not the default server
  `/tmp/tmux-0/default` that every production service uses).
- Worker target `owneros-sandbox-worker-20261027:0.0`; controller
  `owner-os-supervisor-clean:0.0` (this session, default server).

## Why a private server (and what else had to be closed)

| Channel to the worker | Found | Closed by |
|---|---|---|
| Live native supervisor (`NATIVE_SUPERVISOR_TARGETS=*`) on the default server | real | private socket: production `agent_list` does not list it; production `agent_send` → "no existing agent … refusing" |
| Worker's own Stop hook spawns `native_supervise_once.py`; inside the pane `$TMUX` = PRIVATE server, and `.env` arms it with `TARGETS=*` (proven: `True *`) | real, hidden | worker env `NATIVE_SUPERVISOR_ENABLED=0 NATIVE_SUPERVISOR_TARGETS=none` (loader never overrides explicit keys; proven: `False none`) |
| Worker hook events / owner pushes into the live control plane | real | worker env `CONTROL_PLANE_DB`, `AGENT_CONTROL_DB`, `AGENT_CONTROL_AUDIT` → state dir (pushes are only queued in that DB; no network in `emit`) |
| Wake bridge decisions from those events | real | `WAKE_BRIDGE_ENABLED=0` in worker env; events land in the sandbox DB anyway |
| `claude agents --json` lists every Claude process | real | read-only; without a default-server pane nothing can type into it |

## Harness (`tools/native_cto_sandbox_run.py --worker-socket …`)

Routes tmux per target session (worker → private socket, controller → default; buffers follow
the last targeted server; `list-panes -a` merged). Preflight: private server holds exactly the
worker session, default server has no session of that name, both panes located once via
`claude agents --json`. Sends: scope (worker only), listing filtered to the two panes, raising
wrapper. Refuses payorch-ha-fresh, the three owner-closed sessions, controller == worker.

## Proof so far

- Tests: `test_native_cto_sandbox_private_socket.py` (4: production discovery blind to the
  private worker; full loop across two servers request→A / advice→B exactly once; hook-spawned
  supervisor disabled by worker env and armed without it; preflight refuses a polluted private
  server) + `test_native_cto_sandbox_run.py` (14) + related suites.
- Live demo with a NON-Claude stand-in on the real socket path (then killed, socket removed):
  production `agent_list` = the 5 existing sessions only, worker invisible; production
  `agent_send` to the worker refused; routed view sees worker + controller; sends to vtb,
  bitwhite-backend-v3, payorch-ha-fresh, mess-telegram-connector refused; harness preflight
  fails closed on the stand-in (not a native Claude session); live `deliveries` rows: 0.

## Owner gate — start the ONE worker (exact)

```
env -u TMUX \
  CONTROL_PLANE_DB=/opt/owneros-sandbox-state-20261027/cp.db \
  AGENT_CONTROL_DB=/opt/owneros-sandbox-state-20261027/ac.db \
  AGENT_CONTROL_AUDIT=/opt/owneros-sandbox-state-20261027/audit.jsonl \
  NATIVE_SUPERVISOR_ENABLED=0 NATIVE_SUPERVISOR_TARGETS=none WAKE_BRIDGE_ENABLED=0 \
  OWNEROS_AGENT_TARGET=owneros-sandbox-worker-20261027:0.0 \
  tmux -S /opt/owneros-sandbox-tmux-20261027.sock new-session -d \
    -s owneros-sandbox-worker-20261027 -c /opt/owneros-sandbox-worker-20261027 claude
```
First start in a new directory shows Claude's folder-trust prompt; answer it once with
`tmux -S /opt/owneros-sandbox-tmux-20261027.sock attach` (detach: Ctrl-b d). Or type in this
session: "I approve starting the sandbox worker" and it will be run exactly as above.

Then (this session, background): `…/tools/native_cto_sandbox_run.py --worker
owneros-sandbox-worker-20261027:0.0 --controller owner-os-supervisor-clean:0.0 --workdir
/opt/owneros-sandbox-worker-20261027 --state-dir /opt/owneros-sandbox-state-20261027/run
--worker-socket /opt/owneros-sandbox-tmux-20261027.sock --interval 10 --max-secs 1800`.

## Remaining gotchas

1. Controller busy: requests reach this pane only between turns (`pending_busy`, no attempt
   spent); >30 min → re-request, 3 → `provider_failed`.
2. Self-messages: the controller only ever receives `ctoreq:`; never advice; its events are
   never reviewed. A request arriving as a turn here is automated data.
3. Collisions with Owner OS API messages into this pane → second one `queued`, never resent.
4. Worker permission prompts (editing `notes.txt`) hold advice until answered / time limit.
5. The worker's real hook events (Stop/Notification) land in the sandbox bus; owner-flagged
   ones become `owner_needed` requests to the controller (never forwarded as advice).

## Cleanup (after the run)

`tmux -S /opt/owneros-sandbox-tmux-20261027.sock kill-server` → verify absent from that socket
and from `claude agents --json`; `rm -f` the socket; archive
`/opt/owneros-sandbox-worker-20261027` (`git bundle` + notes) into the state dir, remove the
workspace after owner confirmation. Nothing live to revert.

## Results

_(after the run)_
