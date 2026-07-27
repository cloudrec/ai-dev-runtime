# Vanished-session reaper + alert auto-resolution — 2026-07-27

Continuation. Internal only. No running ACAP/Mess process touched; no
destructive/external-credential actions.

## Commits (deployed)
- **ai-runtime** (`main`): `d35a058..9b8a837`
  - `9b8a837` feat(reaper): reconcile vanished supervised sessions
- **Owner OS** (`feat/social-stage4-telegram-wordpress-20260720`): `abe877d..483ccac`
  - `483ccac` feat(alerts): auto-resolve worker_down on recovery + resolved-out-of-current

## 1 — Reaper for vanished supervised sessions — DONE, live
`agent_orchestrator.reap_vanished(live_sessions, emit)`:
- Reconciles records whose tmux session VANISHED (no live pane, stale record).
- **Atomic + race/restart-safe:** `UPDATE … SET state='vanished' WHERE state NOT IN
  ('vanished','ended')` + rowcount check — only the winning writer proceeds, so a
  concurrent sweep / restart never double-processes or double-emits.
- Emits ONE deduped owner event (`agent_vanished_unfinished`,
  dedup_key=`vanished:<session>`) ONLY when the record carried approved unfinished
  work (goal/task and not completed/failed).
- Never recreates an agent, never touches a live pane. Derives live sessions from
  the actual pane inventory; skips entirely on an empty/failed read (never
  mass-reaps). A restarted agent's new pane re-upserts a fresh state.
- **Live:** the stale `job` session (pane gone, record `idle`, goal "JobHunter
  Monetization V1") → record `state=vanished`; exactly ONE `agent_vanished_unfinished`
  (dedup_key `vanished:job`); a second sweep did not re-emit.
- Tests (6): live-not-reaped, approved-work→one event, no-work→no event,
  completed≠unfinished, idempotent/race, restart-recovery.

## 2 — Stale current alerts auto-resolve — DONE, live
- `health.check_and_alert`, on worker RECOVERY, calls
  `notifications.resolve_current(...)` to mark that worker's still-current
  `worker_down`/`worker_stale` alerts RESOLVED. Status → `resolved` keeps the row +
  `external_ref` (telegram msg id / delivery proof) → **audit trail intact**.
- `cto_snapshot._notifications` now EXCLUDES `status='resolved'` from current alerts
  and reports them in a `resolved` bucket → a recovered/retracted alert leaves
  current and moves to resolved history.
- The evidence-verified false `agent_process_failed` (job:0) was moved current →
  resolved via the same `resolve_current` path (audit row kept).
- **Live:** canary worker down→recover → its `worker_down` notification `status=sent`
  → `resolved`; false `process_failed` → resolved; `cto_snapshot.notifications`
  reported `resolved=2`, excluded from `current_alerts`.
- Tests: resolver affected-count; resolved excluded from current + counted.

## Tests / deploy
- Runtime full suite **769 passed** (reaper +6; the 5 orchestrator tests that broke
  on the first wiring — reaper mass-reaped when the test mock lacked a `sessions`
  key — were fixed by deriving live sessions from the agents list + skipping empty
  inventories, then all green).
- Owner OS `test_health` / `test_notifications` / `test_cto_snapshot` green.
- ai-runtime restarted (loop only — panes untouched); backend rebuilt healthy.

## Rollback
- Reaper: `git revert 9b8a837 && systemctl restart ai-runtime.service`. Records
  already marked `vanished` stay so (correct reconciliation); a real restart
  re-upserts a live state. No data lost.
- Alerts: `cd /opt/seo && git revert 483ccac && docker compose build backend &&
  docker compose up -d backend`. Rows marked `resolved` keep their delivery proof.

## Next safe notification/orchestration defect (continuing)
- `resolve_current` matches by title/body substring; make it exact by carrying a
  stable `subject_key` in the OwnerEvent payload → Notification so resolution can
  never match the wrong alert (belt-and-suspenders for the substring match).
