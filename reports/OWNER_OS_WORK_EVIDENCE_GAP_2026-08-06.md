# Owner OS could not see finished work — the MESS 2026-08-06 blind spot

**Scope:** `/root/ai-dev-runtime` only. `/opt/mess` was read (reports + `git log`), never
written to. No product deploy, no Telegram, no CDP or wake-code changes beyond the event
that now reaches the existing bridge.

## What happened

The MESS agent finished goal 2 (responsive navigation), wrote
`/opt/mess/reports/MESS_AUTO_UPDATE_AND_MOBILE_MENU_2026-08-06.md` (9695 bytes, 23:12)
declaring goal 1 **AUDIT COMPLETE / IMPLEMENTATION NOT STARTED**, committed the work, and
went `working → idle`.

Owner OS said nothing. Not the report, not the half-delivered task, not the agent's
decision to stop short of what was asked. The monitor kept printing
`pointer=stage_09_android_test_apk … decision=skip:nothing_queued_and_stage_incomplete`.

## Why — three independent blind spots, not one

| Layer | What it watches | Why it stayed silent |
|---|---|---|
| `core/control_plane/pinger_shadow.py` | `STATE_TO_KIND`: completed / waiting_owner / externally_blocked / dead | `idle` is not in the map. `working → idle` is "not a significant state", so the loop `continue`s |
| same, `_shadow_agents()` | canary allowlist (`CONTROL_PLANE_PINGER_SHADOW_AGENTS`, else `cp-canary:0.0`) | `mess-qa-automation:0.0` is out of scope: counted in `out_of_scope_significant`, never emitted |
| `core/continuation_governor.py` | `authoritative_pointer` → `/opt/mess/design/v1/REDESIGN_EXECUTION_QUEUE.md` | a new report and new commits move no pointer, so the queue looks unchanged |

Underneath all three: **nothing in Owner OS observed work at all.** It observed agent
*state*, stage *pointers* and the task *ledger*. A report, a commit and an artifact were
not inputs to any decision. Work that completed — or stopped half-done — between pointer
moves was structurally invisible.

## The fix

`core/work_evidence.py` — an observer that correlates on evidence rather than on a pointer:

* **reports** under `reports/` and `docs/` of each governed project, content-fingerprinted;
* **commits** since a durable per-project HEAD cursor;
* **the report's own words**: `DONE` alongside `NOT STARTED` / `AUDIT COMPLETE` /
  `BLOCKED` is a *partial completion*, which is exactly the case nobody was told about;
* cross-checked against the **task ledger** (`os_task_queue.active_task`) and the agent's
  state, so an agent that went idle with its task still open is reported as having stopped,
  not as having finished.

Events (all into the CTO inbox):

| Event | Severity | Owner action |
|---|---|---|
| `work_report_published` | info | no |
| `work_partial_completion` | high | yes |
| `work_stopped_incomplete` | high | yes |
| `work_commits_without_stage_progress` | info | no |

Wired into `control_plane/engine.tick_once()` on its own 5-minute cadence (filesystem +
git work does not belong on the 30s tick), best-effort so an observer failure can never
break discovery, health or the outbox.

### Not a file watcher

Three bounds, because an observer that shouts is as useless as one that sleeps:

* **fingerprint dedupe** — the same bytes are never announced twice; a materially changed
  report is news again;
* **cold-start backfill** — a project seen for the first time records its back catalogue as
  seen *without* events, and only the newest few recent reports may speak
  (`COLD_START_MAX_REPORTS`, default 3);
* **one wake per sweep** — every finding reaches the inbox, but only the first
  owner-action event may push. The rest are inbox-only (`push=False`).

Suppression is always counted and returned (`backfilled`, `skipped.not_read`), never
silent.

## Two defects found by running it against real data

Both existed in my first version and were caught only because it was pointed at `/opt/mess`
rather than at fixtures:

1. **It announced history.** The first pass emitted **43** events for reports weeks old.
   Fixed by the backfill window + cold-start cap.
2. **A cap hid today's work.** `MAX_REPORTS_PER_SCAN = 40` was applied to an
   *alphabetical* listing, and `MESS_AUTO_UPDATE_AND_MOBILE_MENU_2026-08-06.md` sorted
   last — the one report that mattered was the one dropped. Candidates are now ordered
   **newest first** and the number not read is reported.

Both are pinned by tests (`test_first_sight_of_a_project_does_not_announce_its_back_catalogue`,
`test_the_newest_report_is_read_first_so_a_cap_cannot_hide_it`).

## Proof on the real report

```
COLD START emitted: 6   backfilled: 37   notifications (wakes): 1
  work_partial_completion  reports/PROJECT_STATE.md
  work_stopped_incomplete  reports/PROJECT_STATE.md
  work_partial_completion  reports/MESS_AUTO_UPDATE_AND_MOBILE_MENU_2026-08-06.md
  work_stopped_incomplete  reports/MESS_AUTO_UPDATE_AND_MOBILE_MENU_2026-08-06.md
  work_report_published    reports/MESS_ANDROID_AND_WINDOWS_BUILDS_2026-08-06.md
  work_stopped_incomplete  reports/MESS_ANDROID_AND_WINDOWS_BUILDS_2026-08-06.md
rescan: 0 emitted
```

The stage pointer was `stage_09_android_test_apk` throughout — unchanged, exactly as during
the incident. The partial completion carries
`markers.not_started = true`, `payload.stage_pointer_moved = false`, and the reason
*"its own report says the requested implementation was not started"*.

## Tests

`tests/test_work_evidence.py` (16): the MESS scenario end to end · classification of a
`DONE` + `NOT STARTED` report · announce-once / re-announce-on-material-change · a working
agent's plain report is info-only · idle with an open ledger task is reported stopped · new
commits with no pointer move, announced once · activation does not replay history · busy
first scan capped and counted · newest-first ordering · one wake per sweep, verified both
through a stub and through the real notification table · unreadable project recorded, not
treated as quiet · the observer never modifies the project it observes.

## Limitations (stated, not hidden)

* Report detection is limited to `reports/` and `docs/`, `.md`, ≤ 512 KB, 40 newest per
  scan. A project that puts durable reports elsewhere is not covered.
* Marker matching is textual. A report that describes completion in prose without any of
  the recognised markers raises `work_report_published` (info), not a partial completion.
* "Scope silently reduced" is detected only when the report itself says so. Comparing the
  delivered result against the originally requested scope needs the task text, and that
  correlation is the next phase, not this one.
* `work_commits_without_stage_progress` reports commits; it does not judge whether they
  match the task.
