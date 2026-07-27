# Daily-brief: current-vs-historical alerts, dedup, internal-work — 2026-07-27

Internal only. Existing agents only — no new agents, no unrelated projects, no
external publish, no old-runtime-job approvals. One service rebuilt (backend).

## Commit (deployed)
- **Owner OS** (`/opt/seo`, `feat/social-stage4-telegram-wordpress-20260720` = remote default): `859d584..4e7bd65`
  - `4e7bd65` fix(brief): current-vs-historical alerts, dedup duplicate blockers, INTERNAL WORK IN PROGRESS
- ai-runtime: unchanged this round (defects were Owner-OS brief only).

## Defects → fixes (by requirement)
1/2/5. **`daily_briefing` headlined 4 alerts** = four historical duplicate
   `agent.externally_blocked` rows for one blocker. `briefing.classify_alerts()`
   (new pure helper) splits CURRENT (>= 120m window) vs HISTORICAL; `count` is
   CURRENT-only, so historical never headlines as active. Duplicates collapse by
   **blocker identity `(event_type, source, title)`** — the per-row `dedup_key`
   hash is unique and was the reason they never collapsed. One blocker = one entry.
3. **INTERNAL WORK IN PROGRESS** (new `daily_brief.py` section): existing agents
   doing internal work, shown independently of runtime jobs (=0 is normal — the
   tmux agents ARE the work). Includes `working` agents AND `idle`-but-freshly-
   active ones, so email:0.0 stays visible between turns.
4. **email phase accurate, no stale crash**: phase from STRUCTURED record fields
   only (`current_task`/`approved_goal`/`state`), never pane scrollback → the old
   "Claude exited code 1" scrollback can't leak. Shows `email [working]: executing`.
6. **Backup stale** stays in the briefing headline flag (`⚠ backup stale`).
7. No unrelated projects / external publish / job approvals / agent creation.

## Tests (focused)
- `test_briefing`: four distinct-hash duplicates for one blocker collapse to 1 and
  are not current; a genuine current alert is deduped + counted.
- `test_daily_brief`: internal-work shows working + idle-recent, excludes idle-stale,
  renders with runtime jobs=0 and no scrollback.
- Green: `test_briefing` + `test_daily_brief` + `test_cto_snapshot` + `test_mcp`
  (31 + 15 passed in the two focused runs).

## Deploy + live verification
- `seo-backend-1` rebuilt → **healthy** (only service touched).
- daily_briefing (forced regen): `alerts.count(current)=0`, `historical_count=1`
  (four `agent.externally_blocked: payment:0.0` collapsed), headline
  `1 pending decision(s), 0 alert(s) — ⚠ backup stale`.
- notifications: `current_alerts=0, historical_24h=4, delivery_failed=0`.
- daily brief: `▓ LIVE STATE … Internal work in progress: 1 (runtime jobs need not
  be >0) · email [working]: executing`.
- email agent status: state idle/working between turns, exactly ONE Claude process
  (pid 2941291, cwd /opt/email) — no duplicate.

## Rollback
`cd /opt/seo && git revert 4e7bd65 && docker compose build backend && docker compose up -d backend`.
Both changes are additive/pure (classify_alerts + a read-only brief section); revert
cannot strand an agent or a job.
