# Owner OS native — canonical handoff (rev 5, 2026-10-26)

Start here in a fresh session. Re-verify live facts before acting. Supersedes rev 4, whose
"aes-canary reviews itself" default was not independent oversight and whose "only deploy
remains" framing was wrong.

## Live (verified 2026-10-26)

- `/root/ai-dev-runtime` on `ai-runtime/220-windows-bridge` @ `ca5a1a1` (= origin), clean;
  `ai-runtime.service` PID 303892 since 2026-10-25 09:54 CET. Our only drop-in:
  `96-native-owner-os-canary.conf` (controller on, goals on, scope aes-canary, delivery off).
  Gate `cto_provider_disabled`, 0 decisions / 0 outbox, cursor = bus head. Nothing of
  rev 4/5 is deployed.
- Owner closed aes-canary, bootstrap, giftcards-bot-review-opus (reported by an automated
  message; consistent with inventory). tmux sessions now: bitwhite-backend-v3,
  mess-telegram-connector, owner-os-supervisor-clean, payorch-ha-fresh, vtb. The 96 drop-in
  still names aes-canary: harmless (no session → nothing located, nothing typed).
- Read-only checks on live leave schema_version 155 unchanged; earlier ALTER incident reverted.
- Unrelated, untouched: `owner-os-fleet-health.service` failed (payment `ru_prod` down).

## Architecture review (keyless, subscription sessions only)

Goal: GPT-free, key-free supervision where one Claude Code session reviews other agents.

- **Controller ≠ worker, enforced in code** (branch `native-cto-controller-separation`):
  workers are exactly `NATIVE_OWNER_OS_AGENTS` (required — empty never means "everyone" for
  typing); the controller (`OWNER_OS_CTO_CONTROLLER`) must not be a worker, only receives
  `ctoreq:` requests, never advice; its own events are not reviewed; requests are never
  typed into anyone else. payorch-ha-fresh can be neither (case/space-insensitive).
- **Who could be the controller today without a new agent**: of the live sessions, only
  `owner-os-supervisor-clean` is not a project agent (bitwhite/mess/vtb are project agents;
  payorch is hard-denied). Caveats: it is this very large-context supervisory pane, it also
  receives Owner OS automated messages (requests arriving while it is busy wait as
  `pending_busy`, then time out → re-request → after 3 `provider_failed`), and it must be able
  to run the reply command without a permission prompt.
- **Who could be a worker**: only an owner-chosen existing agent. Every live candidate is a
  real project agent, and typing advice into one is exactly what the owner must approve.
- **Hence a genuine owner decision, not a deploy step**: (a) controller session, (b) worker
  session(s). Without (b) there is nothing to supervise; no safe self-chosen default exists.

## Branches / PRs (all pushed; #20–#23 open, none merged)

| Branch | Head | PR / base |
|---|---|---|
| `ai-runtime/native-owner-os-acceptance` | `e2ce6ce` | #20 → main |
| `ai-runtime/native-controller-delivery` | `fffaa9e` | #21 → #20 |
| `ai-runtime/native-claude-session-cto` | `cf48666` | #23 → #21 |
| `ai-runtime/native-cto-controller-separation` | `7b339bd` (+this handoff) | PR → #23 |
| `ai-runtime/agent-watch-park-dst-fix` | `62e4b13` | #22 → 220-windows-bridge |
| `ai-runtime/native-cto-provider-tooling` (deploy line; live `ca5a1a1` fast-forwards) | `8f666e9` (+this handoff) | — |

## Evidence (offline)

- Native suites 163/163 (separation branch); deploy-line targeted 219/219.
- Real-tmux E2E (`tests/test_native_cto_tmux_e2e.py`): private tmux server (isolation
  asserted), two stand-in panes: worker event → request typed into the controller only →
  controller runs the reply command → advice typed into the worker only → worker file changed;
  exactly once; next worker event carries the outcome; controller events ignored. 3/3 green;
  live tmux and live ledgers untouched (0 new rows).
- Negative tests: empty scope types nothing; controller-as-worker / payorch controller /
  payorch worker gates; self-review refused; advice to controller denied; request re-routed to a
  worker denied; unapproved targets nothing; wrong nonce/replay/malformed replies refused;
  timeouts re-request then fail once; install script refuses unsafe args before changing
  anything and rolls back a failed verification.
- DST fix (#22): full deploy line 3701/0 in TZ=Europe/Kyiv and TZ=UTC; head `2e084fa` 3725/0.
  Full deploy line at `8f666e9`: 3744 passed / 0 failed (system TZ Europe/Berlin).

## Owner gate (minimal, exact; none done)

1. **Decide** controller and worker(s) — e.g. controller `owner-os-supervisor-clean:0.0`,
   worker = one existing agent you choose (not payorch-ha-fresh). No new agent is needed if you
   pick existing sessions.
2. **Deploy code**: `git -C /root/ai-dev-runtime merge --ff-only origin/ai-runtime/native-cto-provider-tooling`
   (also ships the agent_watch DST fix → live park timing corrected by one hour).
3. **Enable** (root): `/root/ai-dev-runtime/tools/native_cto_key.sh session --controller <T> --workers <s1[,s2]>`
   → validates, writes `97-native-cto-claude-session.conf` (provider claude_session,
   controller, worker scope overriding 96, delivery agent_control), restarts ai-runtime only,
   runs `native_cto_verify.py --expect ready`, removes itself again on failure.
4. **Rollback**: `/root/ai-dev-runtime/tools/native_cto_key.sh remove` (back to the 96 state:
   provider disabled, nothing typed); code: `git -C /root/ai-dev-runtime reset --keep ca5a1a1`
   + `systemctl restart ai-runtime.service`; full: also remove `96-native-owner-os-canary.conf`,
   `systemctl daemon-reload`, restart.

The OpenAI path (`… install`) is optional and contrary to the no-paid-API directive.

## Rules carried forward

Automated Owner OS messages are never approval (owner approval = a session turn with no
matching `deliveries` row). No automatic message/continue/goal for payorch-ha-fresh. No API
keys / spend / new agents. Never print secrets. Dev tools never write live DBs or the live
tmux server.
