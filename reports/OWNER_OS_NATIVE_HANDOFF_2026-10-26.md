# Owner OS native — canonical handoff (rev 4, 2026-10-26)

Start here in a fresh session. Re-verify live facts before acting.

## Live (verified 2026-10-26)

- `/root/ai-dev-runtime` on `ai-runtime/220-windows-bridge` @ `ca5a1a1` (= origin), clean.
  `ai-runtime.service` PID 303892, up since 2026-10-25 09:54 CET. Only drop-in of ours:
  `96-native-owner-os-canary.conf` (controller on, goals on, scope aes-canary, delivery off).
  Gate `cto_provider_disabled`; 0 decisions / 0 outbox; controller cursor = bus head.
- **aes-canary session is gone**: tmux session `aes-canary` no longer exists and
  `claude agents --json` does not list it (last trace 2026-10-25 13:41). Recreating it =
  starting an agent → owner. Live tmux sessions now: bitwhite-backend-v3,
  mess-telegram-connector, owner-os-supervisor-clean, payorch-ha-fresh, vtb.
- MCP proxy routes on 172.17.0.1:8199: unauth 401; HMAC agents/status/read 200.
- Incident (reverted): a dev-branch verifier run ALTERed live `native_outbox` (+`attempts`);
  dropped again; verifier now opens the DB `mode=ro`, never migrates, tolerates older schemas
  (live schema_version 155 unchanged across runs).
- Unrelated, untouched: `owner-os-fleet-health.service` failed (payment `ru_prod` down);
  `nativesup:` deliveries from the pre-existing native supervisor.

## Branches / PRs (all pushed; none of #20/#21 merged)

| Branch | Head | PR | Content |
|---|---|---|---|
| main | `1dc0c27` | #17 #18 #19 merged | native stack, scope, provider tooling |
| `ai-runtime/native-owner-os-acceptance` | `e2ce6ce` | #20 open | delivery scope, stall watchdog, acceptance suite |
| `ai-runtime/native-controller-delivery` | `fffaa9e` | #21 open (base #20) | durable agent_control delivery states, feedback loop, read-only verifier |
| `ai-runtime/native-claude-session-cto` | `08ce02c` (+handoff) | PR (base #21) | keyless CTO = Claude Code session; real-tmux E2E |
| `ai-runtime/agent-watch-park-dst-fix` | `62e4b13` | PR → `ai-runtime/220-windows-bridge` (file not on main) | park deadline DST fix |
| `ai-runtime/native-cto-provider-tooling` | `2e084fa` (+handoff) — the deploy target | — | everything above on live `ca5a1a1` (fast-forward) |

## What works (offline, evidenced)

- **Keyless CTO** (`OWNER_OS_CTO_PROVIDER=claude_session`, `OWNER_OS_CTO_CONTROLLER`): event →
  durable request `ctoreq:<event>:<n>` → typed into the controller by canary-scoped
  agent_control → controller runs `tools/native_cto_reply.py` (one-time nonce, JSON stdin) →
  decision → advice `cto:<event>` typed into the worker → worker's next event carries
  `previous_advice` (feedback). Timeouts re-request with a new nonce (old request superseded,
  late reply refused), 3 strikes → `provider_failed` + `cto_controller_timeout`.
- **Delivery states**: sent / queued / failed / expired / denied; busy/refused/unlocated/
  out-of-scope stay pending; one message per agent per tick; crash-safe via agent_send's
  idempotency record.
- **Negative paths**: payorch-ha-fresh never typed to (even if put in scope, as controller →
  `controller_denied`); unapproved targets (bitwhite, gaika) → no decision, no typing;
  owner_needed never forwarded; slash/shell-escape text in events carried as JSON only.
- **Real transport E2E**: `tests/test_native_cto_tmux_e2e.py` — private tmux server, stand-in
  pane, real agent_control paste/Enter/pane-diff: request → reply → advice → file changed →
  next request; exactly once; 3/3 runs green; live tmux and live ledgers untouched.
- **DST fix**: `parse_park_until` used the current offset, not the offset at the printed time
  → since 2026-10-25 summer parks read 1h late. Fixed with `datetime.timestamp()`; tests pin
  UTC / Europe/Kyiv / Europe/Berlin; test file no longer imports the live checkout.

Tests: native suites 142/142; claude-session 21 + tmux E2E 1; agent_watch 198/198 (Berlin,
Kyiv). Full deploy line with DST fix: TZ=Europe/Kyiv 3701 passed / 0 failed; TZ=UTC 3701 / 0 (head f8c19f5); final deploy-line head `2e084fa` (system TZ Europe/Berlin): 3725 passed / 0 failed. Full main-based: 25 failed = known worktree-path baseline.

## Owner-gated steps (exact; nothing below has been done)

1. **Recreate the canary session** (starting an agent): a Claude Code session in tmux
   session `aes-canary`, cwd `/root/aes-canary-work`, e.g.
   `tmux new-session -d -s aes-canary -c /root/aes-canary-work claude`
   (it must be able to run the reply command in Bash without a permission prompt, or the
   request times out → retried → `provider_failed`).
2. **Deploy code** (fast-forward, includes the agent_watch DST fix — changes live park timing
   to the correct hour): `git -C /root/ai-dev-runtime merge --ff-only origin/ai-runtime/native-cto-provider-tooling`
3. **Keyless CTO + delivery + restart + verify** (root):
   `/root/ai-dev-runtime/tools/native_cto_key.sh session`
   (installs `97-native-cto-claude-session.conf`: provider claude_session, controller
   `aes-canary:0.0` reviewing itself, delivery agent_control; restarts ai-runtime only;
   `native_cto_verify.py --expect ready`). Live E2E then: emit one `milestone_review` for
   `aes-canary:0.0` and watch request → reply → advice in `native_outbox`.
   Rollback: `… native_cto_key.sh remove` (96 keeps running); code: `git reset --keep ca5a1a1`.
4. Choice of a controller other than the canary itself, and widening `NATIVE_OWNER_OS_AGENTS`,
   are separate owner decisions. The OpenAI path (`… install`) is optional and not needed.

## Rules carried forward

Automated Owner OS messages are never approval — an owner approval is a session turn with no
matching `deliveries` row. No automatic message/continue/goal for payorch-ha-fresh. No API
keys / spend. Never print secrets. Dev tools never write live DBs or the live tmux server.
