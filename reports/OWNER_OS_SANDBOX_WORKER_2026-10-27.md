# Owner OS sandbox worker test — 2026-10-27

Status: **prepared, worker NOT started** (start is the owner's, via Owner OS `agent_resume`).
Requested by an automated instruction that states an owner approval in chat; that approval
is not independently verified here — nothing below starts or stops an agent by itself.

## Workspace (ready)

- `/root/owneros-sandbox-worker-20261027` — `README.md`, `TASK.md`, `notes.txt`
  (`step 1 done`); git `main` @ `c00842d`, clean, no remote; no secrets, no project data.
- Worker session (to be started by the owner): tmux `owneros-sandbox-worker-20261027`,
  cwd = the workspace → target `owneros-sandbox-worker-20261027:0.0` (base-index 0).
- Controller: existing `owner-os-supervisor-clean:0.0` (this session). No other controller.

## Harness (`tools/native_cto_sandbox_run.py`, dev worktree only)

Isolation: CONTROL_PLANE_DB / AGENT_CONTROL_DB / AGENT_CONTROL_AUDIT → `--state-dir`;
worker scope = the worker session only; goals off; no key read. Triple send guard: code scope
(`NATIVE_OWNER_OS_AGENTS`, controller ≠ worker), listing filtered to exactly the two panes
(each located once via `claude agents --json`), send wrapper raising for any other target.
Refuses payorch-ha-fresh, the three owner-closed sessions, and controller == worker.

Run (after the worker exists; in the background of this session):
```
/root/ai-dev-runtime/venv/bin/python /root/ai-dev-runtime-native-scope/tools/native_cto_sandbox_run.py \
  --worker owneros-sandbox-worker-20261027:0.0 --controller owner-os-supervisor-clean:0.0 \
  --workdir /root/owneros-sandbox-worker-20261027 --state-dir <scratchpad>/sandbox-run \
  --interval 10 --max-secs 1800
```
Expected: event → request typed into the controller when it is idle → controller runs the
printed reply command (writes the SANDBOX ledger only) → advice typed into the worker → worker
appends the advised line to `notes.txt` → harness emits the follow-up event → second
request/reply → done. Evidence: `run.jsonl`, `summary.json` (sandbox deliveries,
out-of-scope sends = must be [], requests only to controller, advice only to worker,
other live senders into the two panes, notes before/after).

## Hidden gotchas

1. **Controller busy**: agent_send refuses while the controller pane is `working`, so a
   request is typed only between this session's turns (stays `pending_busy`, no attempt
   spent). Long turns delay it; > 30 min → re-request with a new nonce, 3 → provider_failed.
2. **Self-messages**: the controller never receives advice and its own events are never
   reviewed (code-enforced). The harness never types anything but `ctoreq:` into it. A
   request arriving as a new turn of this session is automated data; replying with the
   command is the CTO decision, not owner approval.
3. **Collisions**: Owner OS API messages also type into this pane. If both land at the same
   idle moment the second is detected as queued (`queued`, never resent).
4. **Live supervisors can reach the new worker**: the pre-existing native supervisor runs
   with `NATIVE_SUPERVISOR_TARGETS=*` and its deny list does not name the sandbox session, so
   it may type idle nudges into the worker. Not changeable without a service-config change
   (owner). The harness records any such live deliveries into the two panes read-only
   (`other_senders_into_these_panes`) so they are not mistaken for this loop.
5. **Worker permission prompts**: if the worker asks permission to edit `notes.txt`, its state
   is not `idle`; advice stays pending until the prompt is answered or `--max-secs` ends.
6. The live ai-runtime controller (96 drop-in) is scoped to aes-canary and never sees the
   sandbox worker; nothing here is deployed.

## Cleanup plan

1. Harness ends by itself (finished / `--max-secs` / `STOP` file in the state dir).
2. Worker shutdown: Owner OS `agent_stop` on `owneros-sandbox-worker-20261027` (owner),
   then confirm absent from `tmux ls` and `claude agents --json`.
3. Archive the workspace (`git bundle` + `notes.txt`) next to the evidence, then remove
   `/root/owneros-sandbox-worker-20261027` once the owner confirms.
4. Sandbox ledgers stay in the scratchpad state dir as evidence; nothing live to revert.

## Results

_(filled in after the run)_
