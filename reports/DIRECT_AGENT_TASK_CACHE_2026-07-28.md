# DIRECT AGENTS: transcript task + bounded cache — 2026-07-28

Continuation of DIRECT_AGENT_TRUTH. Internal only; read-only agent evidence; no new
agents; no external/destructive actions. ACAP/Mess untouched.

## Commits (deployed)
- **ai-runtime** (`main`): `a0313c9` (transcript task + snapshot cache)
- **Owner OS** (`feat/social-stage4-telegram-wordpress-20260720`): `a145b4b` (brief staleness marker)

## Item 1 — richer current-task for direct/non-orchestrator agents
`transcript_current_task(cwd)` reads the agent's NEWEST transcript (tail-only, bounded
I/O) and returns the LAST REAL user instruction = its current task. **Truth, not a
guess**: tool-result blocks, `isMeta`/`isSidechain`, and `<command>`/`[` wrappers are
skipped; single line; secret-redacted. `build_direct_agents` gained `task_lookup`
(invoked ONLY when the stored record has no task) + a `task_source`
(record|transcript|None) so the owner knows the provenance.
- **Live:** 5 previously-taskless agents now show real tasks (e.g. `capacity`:
  "Не открывай новый большой этап…"; `security`: "…MIGRATION SELF-CHECK PACKAGE").

## Item 2 — bounded snapshot cache (no brief/portfolio slowdown)
`direct_agents_snapshot()` caches the expensive live read (agent_list per-pane captures
+ transcript reads) for `DIRECT_AGENTS_TTL_SECS` (15s). Within TTL → cached (no new
captures); beyond → one fresh read. **FAIL-OPEN**: on a live-read error it returns the
last cached snapshot marked `stale` (while younger than `DIRECT_AGENTS_HARD_STALE_SECS`
=120s), never an empty/wrong view. `status()` exposes `direct_agents_meta {cached,
age_s, stale, ttl_s}`. The daily brief renders `⚠ STALE snapshot` / `(cached Ns)`.
- **Live:** first `status()` cached=False; second within TTL cached=True (age 0.6s);
  brief saw cached meta (age 8.5s) — no repeated pane captures.

## Accuracy preserved
working / idle / waiting_input / owner-gated (externally_blocked, waiting_owner) /
duplicate-cwd classification unchanged (existing DIRECT_AGENT_TRUTH tests still green).

## Tests
- Runtime `test_direct_agents` **16**: task_lookup only-when-missing + source; transcript
  last-real-instruction, skip tool-result/meta/wrapper, missing→None, redaction; cache
  serves-within-TTL / force-live / fail-open-stale. Full runtime suite **789 passed**.
- Owner OS `test_daily_brief` **19** incl. the staleness marker.

## Deploy / health / rollback
- ai-runtime restarted (read-only reads; panes untouched); backend rebuilt healthy.
- Rollback: `git revert a0313c9 && systemctl restart ai-runtime.service`;
  `git revert a145b4b && docker compose build backend && docker compose up -d backend`.
  Additive/read-only; revert drops the transcript task + cache, snapshot still works live.

## Blind-spot scan
No new functional blind spot found. DIRECT AGENTS block is now truthful (state/cwd/task/
blocker/age/queued/owner-action/duplicate), transcript-enriched, redacted, and cached
fail-open. Remaining ideas are marginal (e.g. per-cwd transcript-read cache across
snapshots) — not a defect. No external/credential/destructive gate touched.
