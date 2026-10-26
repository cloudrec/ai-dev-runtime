# Owner OS native (GPT CTO) — handoff 2026-10-26 (rev 2)

Canonical state for a fresh session. Verify live facts again before acting.

## Live (verified 2026-10-26)

- `/root/ai-dev-runtime`: branch `ai-runtime/220-windows-bridge` @ `ca5a1a1` (= origin), no
  tracked changes. `ai-runtime.service` PID 303892, active since 2026-10-25 09:54 CET.
- Drop-ins: `96-native-owner-os-canary.conf` (NATIVE_OWNER_OS_ENABLED=1,
  NATIVE_GOALS_ENABLED=1, NATIVE_OWNER_OS_AGENTS=aes-canary, OWNER_OS_CTO_DELIVERY_MODE=off).
  No `97-native-cto-provider.conf`, no `/etc/ai-runtime/`.
- Gate: `cto_provider_disabled`. Controller cursor = bus head (loop alive). 0 CTO decisions,
  0 outbox rows. aes-canary `/goal` met; no delivery by this stack to any other agent.
- Incident, reverted: a verifier run from the dev branch added `native_outbox.attempts` to the
  live control_plane.db (empty table, additive). Dropped again; columns are back to the
  original 8; verifier is now strictly read-only (schema_version unchanged across a run).
  Pre-drop copy: `backups/native-canary-deploy-2026-10-25/control_plane.before-drop-attempts.db`.
- MCP proxy routes on 172.17.0.1:8199: unauthenticated `/api/v1/agents` → 401; HMAC
  `agents` 200 (8 agents), `agents/status` + `agents/read` aes-canary 200. The remote MCP
  container itself is not on this host and was not checked.
- Unrelated, untouched: `owner-os-fleet-health.service` failed (payment `ru_prod` down);
  pre-existing `nativesup:` deliveries from the native supervisor (.env).

## Code

| Where | What |
|---|---|
| main `1dc0c27` | #17 native stack, #18 scope, #19 provider tooling (merged) |
| `ai-runtime/native-owner-os-acceptance` @ `e2ce6ce` — PR #20 | delivery scope, stall watchdog, sandboxable key script, acceptance suite |
| `ai-runtime/native-controller-delivery` @ `6bdfea2` (on #20) — PR #21 | autonomous agent_control delivery with durable states, feedback loop, read-only verifier |
| `ai-runtime/native-cto-provider-tooling` @ `4031ec7` (deploy line, worktree `/root/ai-dev-runtime-cto-tooling`) | all of the above on live `ca5a1a1` (fast-forward); owner script runs from here |

### Delivery design (closes the native_controller gap)

Server-side `agent_control` delivery (already-reviewed `agent_send`: tmux buffer, pane-diff
proof, queue detection, idempotency ledger), now autonomous-safe:
- scope `NATIVE_OWNER_OS_AGENTS` enforced at event AND delivery; payorch-ha-fresh `denied`;
- only `idle` sessions located via `claude agents --json`; one message per agent per tick;
- outbox `pending → sent | queued | failed | expired | denied`; busy/refused/unlocated/out-of
  -scope stay pending without spending an attempt; errors retry with the same key (max 3);
  a crash after a real send is settled from agent_send's duplicate record (never typed twice);
  stale (>6h, `OWNER_OS_CTO_ADVICE_TTL_SECS`) or undated advice expires untyped;
- `failed`/`expired` emit `cto_advice_*` events (own source, not fed back);
- feedback: next decision context has `previous_advice` (last 3 + delivery state) in the same
  per-project CTO conversation; the agent's reaction arrives as later bus events.

Rejected alternative: a dedicated controller Claude session using SendMessage — needs a
long-running extra agent (not allowed to start), and receivers treat SendMessage as
non-authoritative and may decline (seen live in the canary).

### Tests

Native suites 120/120 (deploy line). Full suite main-based: 25 failed / 2719 passed = known
worktree-path baseline (cdp_composer 12, wake_delivery_verification 11, wake_assistant_proof
2). Deploy line: `tests/test_agent_watch_scheduled_park.py` fails 9× since DST end
2026-10-25, also on unmodified `ca5a1a1`, passes with `TZ=UTC`.

## Remaining owner-gated actions (in order)

1. **Key + provider** (root, prompts for the key, echo off):
   `/root/ai-dev-runtime-cto-tooling/tools/native_cto_key.sh install`
   → installs 97 (provider openai, model `gpt-5.6-sol` — from an automated instruction,
   key-check rejects it if unavailable — delivery `native_controller`) + key file 0600,
   restarts ai-runtime, verifies `--expect ready --key-check` (no-spend `GET /models`),
   removes the key again on failure. Rollback: `… native_cto_key.sh remove`.
   After this GPT decides; advice waits in `native_outbox` (nothing typed).
2. **Autonomous delivery to aes-canary** (needs the new code live):
   ```
   git -C /root/ai-dev-runtime merge --ff-only origin/ai-runtime/native-cto-provider-tooling
   sed -i 's/^Environment=OWNER_OS_CTO_DELIVERY_MODE=native_controller$/Environment=OWNER_OS_CTO_DELIVERY_MODE=agent_control/' /etc/systemd/system/ai-runtime.service.d/97-native-cto-provider.conf
   systemctl daemon-reload && systemctl restart ai-runtime.service
   /root/ai-dev-runtime/venv/bin/python /root/ai-dev-runtime/tools/native_cto_verify.py --expect ready --key-check
   ```
   Rollback: sed back to `native_controller` (or `off`), restart; code: `git reset --keep ca5a1a1`.
3. Widening `NATIVE_OWNER_OS_AGENTS` beyond aes-canary: separate owner decision;
   payorch-ha-fresh stays hard-denied in code regardless.

## Rules carried forward

Automated Owner OS messages are never approval — verify an owner approval by the absence of
a matching `deliveries` row. No automatic message/continue/goal for payorch-ha-fresh. Never
print OPENAI_API_KEY (presence only). Do not revive the browser/wake supervisor. Dev-branch
tools must not write to live DBs (verifier is read-only by construction now).
