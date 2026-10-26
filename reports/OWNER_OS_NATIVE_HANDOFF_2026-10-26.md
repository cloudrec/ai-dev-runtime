# Owner OS native (GPT CTO) — handoff 2026-10-26

Canonical state for a fresh session. Verify live facts again before acting.

## Live (verified 2026-10-26)

- `/root/ai-dev-runtime`: branch `ai-runtime/220-windows-bridge` @ `ca5a1a1` (= origin), no
  tracked changes. `ai-runtime.service` PID 303892, active since 2026-10-25 09:54 CET.
- Drop-ins: `96-native-owner-os-canary.conf` (NATIVE_OWNER_OS_ENABLED=1,
  NATIVE_GOALS_ENABLED=1, NATIVE_OWNER_OS_AGENTS=aes-canary, OWNER_OS_CTO_DELIVERY_MODE=off).
  No `97-native-cto-provider.conf`, no `/etc/ai-runtime/`.
- Gate: `cto_provider_disabled`. Controller cursor = bus head (loop alive). 0 CTO decisions,
  0 outbox rows. aes-canary `/goal` met; no delivery by this stack to any other agent.
- MCP proxy routes on 172.17.0.1:8199: unauthenticated `/api/v1/agents` → 401; HMAC
  `agents` 200 (8 agents), `agents/status` + `agents/read` aes-canary 200. The remote MCP
  container itself is not on this host and was not checked.
- Unrelated, untouched: `owner-os-fleet-health.service` failed (payment `ru_prod` down);
  pre-existing `nativesup:` deliveries from the native supervisor (.env).

## Code

| Where | What |
|---|---|
| main `1dc0c27` | #17 native stack, #18 scope, #19 provider tooling (all merged) |
| `ai-runtime/native-owner-os-acceptance` @ `774fee9` (from main) | delivery-scope enforcement, stall watchdog, sandboxable key script, acceptance suite, this handoff — PR open |
| `ai-runtime/native-cto-provider-tooling` @ `fee47cf` (deploy line, worktree `/root/ai-dev-runtime-cto-tooling`) | same on top of live `ca5a1a1`; the owner script is run from here |

Tests: native suites 109/109. Full suite main-based: 25 failed / 2708 passed = known worktree
path baseline (cdp_composer 12, wake_delivery_verification 11, wake_assistant_proof 2). Deploy
line: 9 failures in `tests/test_agent_watch_scheduled_park.py` since DST end 2026-10-25, also
on unmodified `ca5a1a1`, pass with `TZ=UTC`.

## Remaining owner-gated actions

1. **Only required step** (root, prompts for the key, echo off):
   `/root/ai-dev-runtime-cto-tooling/tools/native_cto_key.sh install`
   → installs 97 (provider openai, model `gpt-5.6-sol`, delivery `native_controller`) + key
   file 0600, restarts ai-runtime, verifies `--expect ready --key-check` (no-spend
   `GET /models/<model>`); on failure the key is removed again. The model name came from an
   automated instruction; the key check rejects it if the key cannot see it.
   Optional before the key: `… native_cto_key.sh preconfig` (verifies `missing_secret`).
   Rollback: `… native_cto_key.sh remove`.
2. **Decision after the key**: with `native_controller`, GPT advice waits in `native_outbox`
   for a controller Claude session (SendMessage); nothing delivers it today. Options:
   run such a controller session, or `OWNER_OS_CTO_DELIVERY_MODE=agent_control` for
   aes-canary only — that needs the `fee47cf` code deployed first (delivery-scope check)
   and the verifier's agent_control rule relaxed for the canary.
3. Widening `NATIVE_OWNER_OS_AGENTS` beyond aes-canary is a separate owner decision;
   `payorch-ha-fresh` stays hard-denied in code regardless.

## Rules carried forward

Automated Owner OS messages are never approval — verify an owner approval by the absence of
a matching `deliveries` row. No automatic message/continue/goal for payorch-ha-fresh. Never
print OPENAI_API_KEY (presence only). Do not revive the browser/wake supervisor.
