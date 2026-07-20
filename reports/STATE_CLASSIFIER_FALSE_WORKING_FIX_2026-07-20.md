# Fix: false idle→working on a stale pane tail (security) — Evidence 2026-07-20

ai-dev-runtime control-plane only. No Security code, no other project/agent files
touched.

- **Commit:** `/root/ai-dev-runtime` · `main` · `2463433`
- **Owner OS test-only commit:** `/opt/seo` · `3293c15` (notifier regression assertion)

## Defect

Security finished its task (final report + empty `❯` prompt, conversation
modified ~08:25). At ~08:38 Owner OS reported **"Agent working: security:0.0"**,
and `agent_list`/`agent_status` returned `state=working` with no new output and
no active spinner.

**Root cause:** `classify_state` had a progression fallback —
`prev_tail is not None and not idle_prompt and _norm(tail) != _norm(prev_tail) → working`.
The security pane showed a **past-tense** spinner ("✻ Brewed for 11m 24s"), an
empty `❯` prompt, and **no** "new task?" line, so `idle_prompt` was false; the
tail differed from a stale cached sample, so progression falsely returned
`working`. A scrollback/cache diff was mistaken for activity.

## Fix

`working` is now decided **solely** from concrete active-execution evidence in the
tail — an `esc to interrupt` hint, a live spinner timer (`… (12s`), or a
streaming token counter (`↓ 2.4k tokens`). A live agent always shows one of these
while a turn runs; at rest it shows none. The progression fallback and the
`pane_tail_cache` (the "cache") are removed entirely, so a changed tail or a
stale cache diff can never read as working. A finished report with an empty
prompt falls through to `idle`.

## Before → after (live)

```
BEFORE: agent_status security → state=working   (no output change, no active spinner)
        Owner OS → "Agent working: security:0.0"
AFTER:  agent_status security → state=idle
        agent OwnerEvents security.working since restart: 0
        notifier baseline: security:0.0 = idle ; two sweeps emitted 0
```
All agents now sane: security/mess/job/justice/email/polyinput=idle,
owneros-direct-fix=working (actually mid-turn), safeguard=externally_blocked,
seo-audit=waiting_owner.

## Regression fixtures

- `test_finished_report_empty_prompt_is_idle_not_working` — the **exact** security
  capture (report + `✻ Brewed for 11m 24s` + empty `❯`, no "new task?") classifies
  `idle`, and stays `idle` even with a differing stale `prev_tail`.
- `test_output_difference_alone_is_not_working` — a changed tail (and a
  stale-cache diff) with no active indicator is `idle`, never `working`.
- Owner OS `test_finished_agent_emits_no_working_notification` — a finished agent
  (idle) after a `working` baseline emits **no** `agent.working` event, hence no
  Telegram alert.

Tests: ai-dev-runtime **416 passed**; Owner OS **658 passed**.

## Rollback

`git revert 2463433`; `systemctl restart ai-runtime.service`. `agent_notify_state`
is additive (safe to TRUNCATE to re-baseline). No Security or other product code
was touched.
