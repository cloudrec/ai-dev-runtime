# Stale background-task footer can hold an agent "working" forever (DEV issue, handoff only)

Trigger: an automated instruction was received. No live change, push, deploy or route change. Intake race (handoff 69b6eb4) stays a separate task.

## Live state at writing (2026-11-18 ~16:55Z)
- **Runtime** `/root/ai-dev-runtime`: 758ebe4.
- **seo**: 1065b32 **is deployed**.
  - `/opt/seo` HEAD is 1065b32. The deployed branch was pushed to origin on the owner's word.
  - `seo-backend-1` was created at 16:34:45Z and is healthy.
  - In-container check: `_report_read_payload({'content':'x'})` returns `{'text':'x','chars':1}`.
  - Live proof of an actual tech-lead read is still pending.

## What was observed (ServerWarden, 16:46–16:48Z)
- **16:46Z.** The pane showed `✻ Cogitated for 5m 34s · done 5:46 PM · 1 shell still running`. The footer showed `· 1 shell`.
- **16:47:57Z.** `agent_watch_state.cls` was `working`, and nothing woke the chat.
- **~16:48Z.** The background shell finished, and the agent resumed by itself. No input was sent: the last `agent_send` was at 16:41:19Z.
- **16:48:54Z.** The agent ran stage_done (89263). It was delivered to the new chat at 16:49:50Z.

**Verdict for this instance:** correct behaviour. The shell was genuine, and holding the wake was right.

## The risk
`core/agent_watch.py:643` returns `working` for any inventory state in `_ACTIVE_STATES` (`working`, `shell_running`) **before** the quiescence rule (`QUIESCENT_SECS=300`, line 481).

This is by design: "a long-running shell or monitor stays working no matter how long its output is unchanged" (line 473). So, if the evidence behind an active state is **stale UI text**, nothing ever wakes the chat:
- no `task_completed`;
- no `work_stopped_incomplete`;
- no bound.

**Inventory paths to an active state** (`core/agent_control.py classify_state`, around line 1360):

| # | Path | Evidence | Can it go stale? |
|---|---|---|---|
| 1 | `_FOREGROUND_ACTIVE_RE` in `live_status_region` (line 1143) | spinner, timer, "esc to interrupt" | Low risk: the live timer changes the digest every tick |
| 1a | `_STATE_ACTIVE_RUN_RE` (line 1088) without `_TURN_DONE_RE` (line 1148) | "N shells still running" with no `· done HH:MM` stamp | **Yes.** The footer is UI text; a turn line without a done stamp, or a wrapped or cut stamp, keeps it active |
| 1b | `_pane_shell_running` | tmux foreground command not in `_IDLE_FG_COMMANDS` | Low risk: the process is real (it may be hung, but it is live) |
| 2b | `recently_active` | transcript written within `RECENTLY_ACTIVE_SECS=10` | No: it is a fresh fact |
| 5 | `_STATE_BACKGROUND_MONITOR_RE` (line 1169) in the last 400 chars | footer `· N monitor(s)` | **Yes.** The footer text persists if the CLI's own task bookkeeping is stuck, or the monitor hangs without output |

**Checked by replay.** The exact 16:46 text, classified with no other signal (`classify_state(True, True, tail)`), gives `idle`: the done stamp is seen, and "shell" footers do not match the monitor regex. So the live `working` at 16:47:57 came from a non-text signal (path 1b, 2b or a live descendant), which is consistent with the shell being genuine. **The risk is paths 1a and 5, not this event.**

## Bounded fix (DEV, a clean session)
1. **Liveness fact, not text.** Add `background_live(pane)`, which reports whether the Claude process for the pane has any non-thread descendant process (from `/proc/<pid>/task/*/children`, recursive, cheap).
   - The ServerWarden check at 16:48:27 shows the shape: `pstree -p <claude pid>` lists only `{claude}` threads when nothing runs.
   - It must fail safe: if the read fails, treat the shell as live (unknown means no wake).
2. **Stale-active rule in `agent_watch.classify`.** Before the `_ACTIVE_STATES` early return, demote to `quiescent` only when **all** of these hold:
   - the inventory state is active **only** through a text path (1a or 5), not 1, 1b or 2b; this needs an `active_via` reason returned alongside the state;
   - `background_live` is False;
   - the bottom-region digest has been unchanged for `AGENT_WATCH_STALE_ACTIVE_SECS` (default 1800, well above `QUIESCENT_SECS`);
   - there was no transcript write in that window.

   The result is `work_stopped_incomplete` (never `task_completed`), with reason `stale_active_footer_<n>s`.
3. **Dedup.** The existing `notified_digest` / `notified_cls` gate means one emission per frozen digest. Any change to the region (a new turn, a timer tick) resets `digest_since`. The existing `actionable_cooldown_active` still applies.
4. **Untouched:**
   - genuine work: a ticking spinner changes the digest, and a live child process or a transcript write blocks demotion;
   - `waiting_owner` and menus, which still outrank everything;
   - payment panes (offer filter unchanged);
   - the 300 s rule for at-rest panes.

## Deterministic tests to write (synthetic tails; injected `background_live`, clock and digest; no tmux)
1. Frozen `· 1 monitor` footer, no child, 1800 s unchanged → `quiescent`, reason `stale_active_footer_*`. Fails today: `working`.
2. The same, with `background_live=True` → `working` at any age.
3. The same, with a transcript write inside the window → `working`.
4. The same at 1799 s → `working` (bound).
5. "1 shell still running" without a done stamp, no child, 1800 s → `quiescent`. Fails today.
6. A ticking spinner `✻ Pouncing… (29s ·` that changes each tick → never quiescent (the digest resets).
7. Active via path 1b (a real foreground command) → never demoted, even with a frozen digest.
8. Dedup: two scans on the same frozen digest emit one `work_stopped_incomplete`; a digest change and a re-freeze emit one more.
9. `waiting_owner` or a menu in the same pane still gives `owner_prompt`.
10. `background_live` read error → treated as live → `working`.
11. The 16:46 ServerWarden replay (exact text, child present) → `working`; no event.

**Mutants to kill:**
- ignore `background_live`;
- drop the transcript check;
- `>=` changed to `>` at the bound;
- demote path 1b;
- emit `task_completed` instead;
- no dedup;
- read error treated as not live.

**Files:** `core/agent_control.py` (`active_via` plus `background_live`), `core/agent_watch.py` (classify and scan wiring), and a new `tests/test_agent_watch_stale_footer.py`. Run the full suite before merge-ready; the release goes through the installer on a word.

## Clean-session prompt
"Owner OS DEV task: implement reports/OWNER_OS_STALE_FOOTER_HANDOFF_2026-11-18.md in a new worktree from origin/owneros/autonomy-loop-20261117 (runtime live 758ebe4). Tests first (the 11 above), then the fix and the mutants, then the full suite, then stage_done. No deploy, push, route or permission change; no payment panes. Keep the intake race (reports/OWNER_OS_INTAKE_RACE_HANDOFF_2026-11-18.md) separate."

## Implemented in DEV (this branch; not deployed)
Trigger: an automated instruction was received. No deploy, push, route or permission change.

**Code** (`reports/loop/stale_footer_2026-11-18/runtime.diff`, against live 758ebe4):
- **`core/agent_control.py`**, new pure helpers; `classify_state` is unchanged:
  - `active_via_footer_only(tail)`: is an active reading resting only on paths 1a/5?
  - `background_live(claude_pid)`: any child under any thread of the Claude process; `None` if unreadable.
  - `transcript_idle_secs(cwd)`: `None` if absent or unreadable.
- **`core/agent_watch.py`**:
  - `STALE_ACTIVE_SECS` (env `AGENT_WATCH_STALE_ACTIVE_SECS`, default 1800).
  - `_live_facts` and `_stale_active`.
  - `classify(..., stale_active=False)`: the only exit from an active state, giving `quiescent` with reason `stale_active_footer_<n>s` and emitting `work_stopped_incomplete`.
  - In `scan`, facts are read only once the pane has been frozen for the bound.
- **Fail-safe:** an unknown child, an unknown transcript or a facts exception means live, so no wake.
- **Dedup** is the existing `notified_digest` gate: one wake per frozen episode, and a new one after any change on screen.
- **Never:** it does not stop, restart or type into anything. Owner prompts, the payment offer filter and the 300 s at-rest rule are untouched.

**Tests:**
- New `tests/test_agent_watch_stale_footer.py`, 18 tests (the 11 planned cases, plus the bound edge, three unknown-liveness variants, and no-facts-read before the bound).
- On 758ebe4: 5 fail, 13 pass. The 13 are the "must stay working" cases, which hold on both versions.
- **Mutants: 10 of 10 killed** (scratch copy):
  - ignore `background_live`;
  - drop the transcript check;
  - `>=` changed to `>`;
  - demote a foreground command;
  - emit a completion;
  - a read error treated as not live;
  - an unknown child treated as dead;
  - footer-only ignores a foreground spinner;
  - no dedup;
  - facts read for every pane. This one survived at first; the test was strengthened.

**Release, when authorised:** `TARGET_SHA=e876325 deploy/adaptive_offer/install.sh --check`, then `--apply`. Rollback goes through the backup the installer prints. No schema change.

**Candidate** `e876325`. Full suite: 5556 passed, 4 skipped, EXIT 0 (`reports/loop/stale_footer_2026-11-18/full_suite.txt`).
