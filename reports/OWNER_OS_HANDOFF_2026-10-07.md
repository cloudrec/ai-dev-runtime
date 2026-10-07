# Owner OS handoff — 2026-10-07 (pause before this tmux is stopped)

**Provenance.** An automated instruction was received via the Owner OS API asking for a
pause/handoff. Nothing in this file is owner sign-off, owner approval, or an owner
instruction. Every item under *Owner gates* still requires an owner-typed line (one that
arrives without the `[AUTOMATED …]` header — that header is the only signal this session
can use to tell the channels apart).

**Claude session id:** `b8999cd0-54ec-445f-abd2-840455c3c369`
(transcript `/root/.claude/projects/-root-ai-dev-runtime/b8999cd0-54ec-445f-abd2-840455c3c369.jsonl`)
**tmux session:** `owner-os-opus-clean:0.0`, cwd `/root/ai-dev-runtime`.

## Repo state

| | |
|---|---|
| Branch | `ai-runtime/220-windows-bridge` |
| HEAD | **`41e7ca5`** — `fix(direct_pane_control): let typed text settle before the submit keystroke` |
| Upstream | `origin/ai-runtime/220-windows-bridge`, **ahead 62**, nothing pushed |
| Tracked tree | **clean** (0 modified/staged) |
| Untracked | **34 files, all `reports/*.md`** — preserve; never `git add reports/`. Every commit in this session staged explicit paths only |

Last recorded full-suite result in-repo: **3452 passed** (`956672e`). The suite was **not**
re-run in this final turn — no code changed in it.

## Services / PROD state

| unit | state | since |
|---|---|---|
| `ai-runtime` | active, pid 1447271 | 2026-10-01 06:44 CEST |
| `owner-os-wake-companion` | active, pid 3046969 | 2026-10-07 06:28 CEST |

**tmux server: pid 2587052, cgroup `/system.slice/owner-os-tmux-2155457-8d606e0b18f7.scope`.**
10 live sessions (arbitrage, capacity-blockchain, diamond-auction, gaika-ru-sonnet-v1,
giftcards-bot-review-opus, hostsecure-clean-resumed, main105-backup-repair,
mess-ru-54582145-resumed, owner-os-opus-clean, payorch-ha-fresh).

**This supersedes the standing memory note `tmux-server-in-daemon-cgroup`.** GATE 2 of
`reports/OWNER_OS_DEPLOY_RUNBOOK_2026-09-23.md` has been executed: the server is in its own
transient scope, not inside `ai-runtime.service`. Verified two ways — the cgroup path
above, and empirically: sessions created 2026-09-25 are still alive after the `ai-runtime`
restart on 2026-10-01. **An `ai-runtime` restart no longer kills the fleet.** The companion
restart (GATE 1) also appears to have been taken (today 06:28).

## Owner gates still open

1. **`git push origin ai-runtime/220-windows-bridge`** — 62 commits, never pushed. The
   runbook's GATE 4 counted 23 at the time; it is 62 now.
2. **`/opt/ai-dispatcher` cost-routing patch — prepared, tested, NOT applied.** See below.
3. **Two dispatcher model decisions** (also below).
4. Dedicated Owner OS Telegram bot (BotFather) — owner action, unchanged.
5. Out of scope, for the owner: `payment.clients.help` renewal conf malformed (trailing
   comma in `webroot_path`); `youtube.seoseo.cheap` cert expired 2026-08-27; `fi_edge` down
   since 2026-09-08.

## The one piece of work left mid-flight: ai-dispatcher cost routing

Goal was to stop multi-million-token runs on trivial checks. **Root cause found and a fix
built and tested, but deliberately not applied** — `/opt/ai-dispatcher` is outside this
session's scope and every authorization for it arrived through the automated channel, which
cannot self-certify. This repo's own history records `0232fa9`
*"a fabricated prior owner instruction, disproved from durable records"*, and a search of
`control_plane.db` on 2026-09-29 found no `owner_gate`/`owner_decision`/approval row for the
dispatcher task. Nothing under `/opt` was written or modified at any point.

**Root cause:** `/opt/ai-dispatcher/src/dispatch.js:138` added +4 to the complexity score for
prompts over 8000 chars. Measured on the unpatched original: `"run the smoke test"` plus a
1.1 MB log routed to the `sonnet` tier; `"summarize this note"` plus padding escalated
free→tier_code on length alone.

**Model ids, verified 2026-09-29 against the public OpenRouter catalogue (460 models, no
auth, no credit spent):**
- `qwen/qwen3.8-flash` — exists, 1M ctx, $0.15/$0.47 per M.
- `qwen/qwen3.8-max-0902` — exists, 1M ctx, $2.00/$6.00 per M.
- `z-ai/glm-4.5-air:free` — **does not exist**; failed closed, no substitution.
- **Five ids already live in that config do not exist** and have been silently failing over:
  `qwen/qwen3-coder:free`, `cohere/north-mini-code`, `poolside/laguna-m.1`,
  `anthropic/claude-sonnet-latest`, `anthropic/claude-opus-latest`.
- `qwen/qwen3-coder-plus` (deprecating 2026-10-09) is not referenced anywhere in the
  dispatcher — nothing to remove.

**Prepared artifacts (scratchpad, survive until the host clears `/tmp`):**
```
/tmp/claude-0/-root-ai-dev-runtime/b8999cd0-54ec-445f-abd2-840455c3c369/scratchpad/dispatch.patch            (17403 B)
/tmp/claude-0/-root-ai-dev-runtime/b8999cd0-54ec-445f-abd2-840455c3c369/scratchpad/routing_policy.test.mjs   (7336 B)
```
Both verified present at handoff time. **If `/tmp` is cleared before the gate is answered,
the patch must be rebuilt** — the findings above are the durable part.

What the patch does: removes the length term entirely; adds a verification-intent check that
runs first and returns the free lane (observe-only verb present AND no mutation verb, so
"run the tests" is free while "fix the failing tests" stays coding); `tier_code`→
`qwen/qwen3.8-flash`; `opus`→`qwen/qwen3.8-max-0902` gated behind a new `--allow-max` /
`AI_ROUTER_ALLOW_MAX` (`--allow-opus` still honoured, since `/opt/ai-terminal/server.js`
passes the old form); context guard holds prompts over ~150k estimated tokens at `cheap`;
cost guard blocks automatic Max over $0.10 and fails closed on unknown-cost models; a hard
pre-dispatch throw so no future routing change can leak an unopted paid Max call. Tier
structure, aliases, CLI surface and risk floor unchanged.

Tests: **85/85 existing pass** (backward compatibility) **+ 11/11 new**. The new suite drives
the real CLI through `--dry-run` subprocesses rather than the mirrored logic block inside
`routing.test.mjs`, which cannot catch `dispatch.js` drifting from it. Against the unpatched
original, 9 of the first 10 fail, so they bite. No paid completion at any point — `--dry-run`
returns before the network call.

Verified routing on the patched copy:
```
run the smoke test and check errors     free       qwen/qwen3.8-27b:free   est $0
npm test, 4000 lines of errors          free       qwen/qwen3.8-27b:free   est $0
add a new api endpoint to the backend   tier_code  qwen/qwen3.8-flash      est $0.00071
hard task, no opt-in                    tier_code  qwen/qwen3.8-flash      est $0.00071
hard task, --allow-max                  opus       qwen/qwen3.8-max-0902   est $0.00904
--max-tier opus without opt-in          tier_code  qwen/qwen3.8-flash      blocked
```

**Two decisions that are the owner's, not mine:**
1. Free-lane primary. The requested `z-ai/glm-4.5-air:free` does not exist; the patch uses
   `qwen/qwen3.8-27b:free` (262k ctx). Alternatives: `nvidia/nemotron-3.5-lightning:free`
   (1M), `thinkingmachines/inkling:free` (1M).
2. The `sonnet` tier's primary is one of the five dead ids and was left in place rather than
   silently re-pointed, because the risk floor lifts high-risk work there. A verified
   fallback (`qwen/qwen3.8-flash`) was added so such a task degrades to a working model
   instead of failing. Consequence: high-complexity work without `--allow-max` now lands on
   flash.

**Apply (only after an owner-typed go-ahead), with backup first:**
```
cd /opt/ai-dispatcher
cp -a src backups/pre_cost_routing_$(date +%Y%m%d_%H%M%S)/
patch -p0 src/dispatch.js < <scratchpad>/dispatch.patch
cp <scratchpad>/routing_policy.test.mjs tests/
node tests/routing.test.mjs && node tests/routing_policy.test.mjs     # expect 85/0 then 11/0
```
**Rollback:** `cp -a backups/pre_cost_routing_*/dispatch.js src/` and delete
`tests/routing_policy.test.mjs`. `/opt/ai-dispatcher` is **not a git repo** — there is no
commit to make; its convention is timestamped `backups/` directories.

## Do not touch

- `main105-backup-repair:0.0` — intentional `scheduled_wait`, one live shell watcher. Leave
  alone unless the shell dies or its scheduled verification time arrives.
- `diamond-auction:0.0` — parked on real owner/external gates.
- `payorch-ha-fresh:0.0`, `capacity-blockchain:0.0`, `mess-*` — value-bearing, deny-listed
  from native supervision by design. Do not weaken that denylist.
- Owner OS self-supervision exclusion (`SELF_PROJECT` in `AUTO_REGISTER_DENY_PROJECTS`) —
  keep.
- `configs/.env` — includes `NATIVE_CANARY_TARGET=hostsecure:0.0` (set 2026-09-11,
  provenance unknown) and the `NATIVE_SUPERVISOR_*` live overrides. Not this session's to
  change.
- `/opt/ai-dispatcher`, `/opt/ai-terminal`, and all payment/SEO/certbot host config.
- Owner conversation content — read metadata only (lengths/roles), never the text.

## Exact next step to resume

Read this file, then `git log --oneline -5` to confirm HEAD is still `41e7ca5` and the tree
is clean. Then wait for an owner-typed line on gate 1 (push) or gate 2 (apply the dispatcher
patch). No implementation is pending or half-finished in this repo; the tracked tree is
clean and the only unfinished deliverable is the dispatcher patch, which is complete and
tested but unapplied by design.
