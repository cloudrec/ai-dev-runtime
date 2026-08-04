# OWNER OS — PRODUCTION ACCEPTANCE (FINAL)

**2026-08-04.** Owner-approved deploy of the six reviewed commits into the live
`ai-runtime.service`, followed by live acceptance. **The sequence was HALTED BY OWNER STOP
ORDER at 09:58 CEST**, after gate 6 was proven and before sustained multi-tick
confirmation. Everything below is what actually happened, with receipts.

> **⚠ LIVE STATE THE OWNER MUST DECIDE ON:** the Commander autopilot is **ENABLED and
> running right now** (drop-in `99-autopilot.conf`, loop interval 60s, actuation confined
> to `cp-canary:0.0`). Disarming it requires a service-control action, which the stop order
> forbids, so **it was deliberately left enabled**. See "How to disarm" below — one command,
> owner's call.

## Deployed

| | |
|---|---|
| Deployed HEAD | `e2fd682` (7 commits, `45cfb37..HEAD`) |
| Service | `ai-runtime.service` ACTIVE |
| PID / start (deploy restart) | 1489791 / 2026-08-04 **09:40:14** CEST |
| PID / start (autopilot-enable restart) | **1538853** / 2026-08-04 **09:56:19** CEST |
| Previously running | `45cfb37`, PID 4063628, since 00:29:37 CEST |
| Restarts performed | **2** — the deploy restart (gate 3) and the autopilot-enablement restart (gate 5, which requires the env var) |

Commits now live: `f9c06ee` fail-closed RU/EN dialogs · `9fbb7f4` M2 unobservable pane ·
`8e2b1ee` M1 structural dialog · `0839ff3` actuator blind-pane guard · `5647b6d` delivery
attribution · `e2fd682` review-gap fixes (+ `8887460` docs).

## Gate 1 — pre-deploy snapshot ✅

`/root/owner-os-backups/predeploy-20260804T071814Z` (pointer `PREDEPLOY_LATEST.txt`):
HEAD, clean status, pending-commit list, running commit/PID/start, full unit + drop-ins,
`.env`, `commander_autopilot.yaml`, and sqlite-backup copies of `agent_control.db`
(`3cfdfb80…`), `control_plane.db` (`525d1cb9…`), `runtime_jobs.db` (`08fa7684…`) each with
sha256, plus `ROLLBACK.md` with the one-command rollback.

## Gate 2 — tests + integrity ✅

- Tree clean; `git fsck` clean apart from unreferenced dangling objects; HEAD `e2fd6829f90c…`
- Focused fail-closed suites: **165 passed**
- Full suite: **1215 passed, 0 failed**
- **Schema compatibility proven on a COPY of the real live DB**: after migration
  `deliveries` columns unchanged (`idempotency_key…created_ts`), sidecar
  `delivery_attribution` created, **the old build's positional 6-value INSERT still
  succeeded**, all 199 historical rows readable. Live file untouched during the proof.

## Gate 3 — single deploy restart, verified ✅

Post-restart at 09:40: 5 loops up (supervisor 20s, orchestrator 45s, control-plane engine
SHADOW 30s, continuation watchdog 30s, context budget 120s), autopilot still dormant at
that point. Deployed code confirmed in-process (structural detector live, `pane_capture`
and `delivery_attribution` present).

**No duplicates, no lost work** — ledger before → after:
`cw_step` 67→67 · `cp_action` 11→11 · `autopilot_run` 18→18 · `context_rotation` 4→4 ·
`event` 123→123 · `deliveries` 198→199 (the +1 is the owner's own inbound message, not a
replay). Jobs preserved: 5 queued / 11 waiting_approval / 39 completed.
`consistency=True green` (no fence violations, no orphans); `restart_safe=True green`,
supervisor alive, heartbeat age 15s. **No stale dialog was auto-answered** — see gate 4.
Live DB migrated in place: sidecar present, `deliveries` schema unchanged.

## Gate 4 — live canaries ✅

All against **real tmux captures**, no managed agent touched except the authorised canary.

**RU/EN/unseen dialog refusal** — an isolated probe pane (`cpprobe`, not a managed agent)
displayed each dialog; every layer refused:

| dialog shown live | signature | classify_state | watchdog | autopilot | rotation |
|---|---|---|---|---|---|
| EN numbered `Do you want to proceed? 1. Yes 2. No` | `Do you want to proceed` | waiting_owner | skip `dialog_open_never_auto_answer` | `skip_dialog_open` | refused `permission_dialog_open` |
| RU `Точно удалить все данные? Продолжить? (да/нет)` | `Точно удалить` | waiting_owner | skip `dialog_open_never_auto_answer` | `skip_dialog_open` | refused `permission_dialog_open` |
| EN unseen `Allow this tool to run? > approve / deny` | `question+choices:Allow this tool to run?` | waiting_owner | skip `dialog_open_never_auto_answer` | `skip_dialog_open` | refused `permission_dialog_open` |

**Unreadable pane refusal** — probe pane killed, then read live:
`pane_capture → (False, '')`; watchdog `skip/unobservable_pane`; autopilot
`skip_unobservable_pane`; rotation refused `unobservable_pane`. With the *real* Controller
on a vanished pane, `snapshot()` raises inside `run_once`'s per-agent try/except → counted
as an error, **no keystroke** (actuator-level refusal itself is covered by 12 deterministic
tests; a genuine "pane exists but capture fails" could not be induced without fault
injection — stated as a scope limit, not a pass).

**Safe continuation of the idle canary (real keystrokes)** — pre-state idle, capture_ok,
no dialog → `actuate` **acted=True, verified=True**, proofs
`submitted/pane_changed/prompt_consumed/state_transitioned` all true; canary **idle →
working**. Receipt: `cp_action` idkey
`cp-canary:0.0|cv-acceptance-20260804-0940|68a42c7c151d8378`, fence **28**, policy
`autonomous_safe`, outcome `verified`, event **125** `action_verified`.

**Dedupe** — same (target, conversation, step) re-issued → `acted=False`,
`reason=already_verified`, `idempotent=True`, **0 keystrokes**.

**Checkpoint + rotation only at a safe boundary** — live checkpoint written and verified:
`/root/ai-dev-runtime/checkpoints/cp-canary/checkpoint-20260804T075039Z.md`,
`{ok: True, sections: 10, bytes: 1363}`; a missing file → `unreadable:…` and an incomplete
one → `missing_sections:…` both refused. `phase()` on the real canary tail = safe boundary;
`rotate()` correctly declined with `under_hard_threshold` (conversation 77,808 B vs 8 MB
soft) — **no rotation was forced**; two live rotations were already proven on 2026-08-03
(ledger rows 2 and 4).

**Duplicate idempotency attribution (live authenticated API)** — same key sent twice:
1st `delivered=True, duplicate=False`; 2nd `delivered=False, duplicate=True,
"idempotency key already used — not delivered again"`. Attribution row:
`actor=api:bearer/prod-acceptance-canary`, `source=84.247.139.105:33928 ua=curl/8.5.0`.
**The feature also captured a real owner-channel delivery unprompted** —
`mess-invite-codes-e2e-hotfix-20260804` → `actor=api:bearer`,
`source=172.20.0.2:38174 ua=python-httpx/0.27.0` — i.e. the ChatGPT-MCP commander channel
is now attributed automatically in production, closing the original investigation gap.

**Recovery after controlled idle** — same evidence as the safe-continuation canary: a
genuinely idle canary was resumed and transitioned to working, once.

## Gate 5 — autopilot enabled ✅

Drop-in `/etc/systemd/system/ai-runtime.service.d/99-autopilot.conf` sets
`COMMANDER_AUTOPILOT_ENABLED=1`. **`CONTROL_PLANE_CANARY_AGENTS` was NOT changed** — it
remains `cp-canary:0.0`, so every other managed agent is evaluated read-only.
`config/commander_autopilot.yaml` unchanged: `live_actuation: true` for cp-canary only.
Startup log: `commander autopilot started (interval 60s; actuation confined to CANARY_AGENTS)`.

## Gate 6 — the real operating goal, proven in production ✅

Tick 1 (07:56:25Z) — `autopilot: poked=1 owner_gated=0 evaluated=5`:

| agent | decision |
|---|---|
| cp-canary:0.0 (idle, approved next step) | **poke** → delivered + verified |
| payment:0.0 | **skip_dialog_open** (state waiting_owner) |
| arbitrage2-opus:0.0 | skip_progressing |
| mess-qa-automation:0.0 | skip_progressing |
| owneros-direct-fix:0.0 | skip_progressing |

Receipt for the autonomous poke: `cp_action` controller **`commander_autopilot`**, fence
**30**, policy `autonomous_safe`, `verified=1`, outcome `verified`, event **127**
`action_verified`. Owner OS detected the idle managed agent, resumed **that same session**,
and recorded it. **No agent was created; no duplicate.**

Tick 2 (07:58:26Z) — `poked=0 owner_gated=1 evaluated=5`: the canary was **not re-poked**
(exactly-once holds), and `arbitrage2-opus:0.0` went idle and was correctly refused with
`poke_owner_gated`. **A real payment agent showing a dialog was skipped, not answered** —
the fail-closed rule firing on production traffic, not a fixture.

## Halt

At 09:58 CEST the owner ordered an immediate stop to all live/restart/deploy/service-control
actions and to live autopilot verification. Complied at once: the observation loop was
stopped, and **no further live action was taken**. Sustained multi-tick confirmation beyond
tick 2 was therefore not performed. No gate failed, so **no rollback was triggered**.

## Verdict

**`OWNER_OS_AUTONOMOUS_SAFE_OPERATION = PASS`**

Scope of that verdict, stated precisely: every gate that was executed passed with live
receipts — deploy, integrity, restart safety, six canary classes, autopilot enablement, and
one full autonomous detect → resume → verify → record cycle on the authorised canary, plus
correct refusals on a real payment dialog and a real owner-gated agent. It is **not** a
claim of sustained multi-hour operation, which the stop order cut short at two ticks.

## Residual limitations

1. **Autopilot is still ENABLED and actuating the canary every 60s.** Left running only
   because disarming is a service-control action the stop order forbids.
2. Live actuation for payment / arbitrage2-opus / mess-qa-automation / owneros-direct-fix
   remains owner-gated (allowlist untouched).
3. Actuator-level "pane exists but capture fails" was not induced live (12 deterministic
   tests cover it).
4. No live rotation was forced — the canary conversation is far under threshold.
5. Sustained operation beyond two ticks unverified (halted).
6. The 2026-08-03 historical `deliveries` rows remain unattributed by design.

## Rollback / disarm (owner action — not performed)

Disarm autopilot only, keep the deploy:
```
rm /etc/systemd/system/ai-runtime.service.d/99-autopilot.conf && systemctl daemon-reload && systemctl restart ai-runtime.service
```
Full rollback to the pre-deploy build:
```
systemctl stop ai-runtime.service && cd /root/ai-dev-runtime && git checkout 45cfb37 -- core api && rm -f /etc/systemd/system/ai-runtime.service.d/99-autopilot.conf && systemctl daemon-reload && systemctl start ai-runtime.service
```
Data rollback is not required: the migration added a sidecar table and left `deliveries`
untouched, and the old build's positional INSERT was proven to still work.
