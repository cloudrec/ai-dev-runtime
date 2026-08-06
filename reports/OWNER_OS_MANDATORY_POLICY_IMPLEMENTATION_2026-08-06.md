# Owner OS mandatory policy — implementation report

**Date:** 2026-08-06 · **Scope:** `/root/ai-dev-runtime` only. No product project, no
production deploy, no Telegram, no DNS, no external action.
**Baseline:** `cc26fc8` on `ai-runtime/162-stop-telegram-sync-spam`, tree clean apart from
a pre-existing modification to `reports/phase3_postfix_soak.jsonl`.
**Backup:** `backups/owner_os_constitution_20260806_222454/` — `config/` tree,
`control_plane.db`, `runtime_jobs.db`, `agent_control.db`, and `BASELINE.txt` recording the
HEAD, branch and `git status` at the start.

---

## 1. Audit of what already existed

The runtime was **not** short of guardrails. It was short of **one** guardrail. Nine
independent deny-by-default mechanisms were already in place, each excellent inside its
own vocabulary and blind outside it:

| Mechanism | What it decides | Blind to |
|---|---|---|
| `core/permission_resolver.py` (756 L) | is this shell command provably read-only? | anything not a shell command; says nothing about rollback or evidence |
| `core/service_ops_policy.py` | narrow systemctl/docker restart allowance | file edits, data, money, scope |
| `core/git_push_policy.py` | is this push routine and safe? | everything except pushes |
| `core/approved_gates.py` | may a pane dialog be auto-answered? | non-dialog actions |
| `core/backup_engine.py` | takes and restores snapshots | never asked "was a backup taken?" before a mutation |
| `core/job_kinds.py` + `job_validation.py` | is this outcome truthfully labelled? | a truthful label with no evidence behind it |
| `core/control_plane/provenance.py` | did an owner really answer this gate? | actions that never open a gate |
| `core/control_plane/actuator.py` | lease, fence, idempotency for pane commands | only pane commands |
| `core/os_task_queue.py` | continuation exists because a row exists | only continuations |

### Gap analysis

1. **No shared vocabulary.** Nothing in the codebase used the words `READ_ONLY /
   MUTATING / HIGH_RISK / IRREVERSIBLE` or `ALLOW / REQUIRE_EVIDENCE / REQUIRE_OWNER /
   HARD_BLOCK`. Each mechanism invented its own (`autonomous_safe`, `prohibited`,
   `dangerous`, `risk_level: low|medium|high`), so no rule could be stated once and
   enforced everywhere.
2. **Backup was available, never required.** `BackupEngine` produced snapshots, but no
   gate asked whether a rollback path existed before a mutation. The pipeline happened to
   take one; nothing enforced that it had.
3. **Evidence was prose.** `job_kinds` prevented a *plan* from being labelled an
   implementation — a real and important gate — but a `completed` job needed no structured
   rollback reference, no baseline, no live state.
4. **No unified audit.** There was no way to answer "was this action evaluated at all?"
   from data. Absence of a block looked identical to absence of a check.
5. **No override primitive.** Any bypass was ad-hoc, unbounded and invisible.
6. **`risk_level` was decorative.** The `jobs` table has carried `risk_level`,
   `dangerous` and `approval_required` since early on; `dangerous` gated only the
   auto-approval boolean at creation, and `risk_level` was written by the planner and read
   by nothing.
7. **The rules lived in prose.** Everything above was written in `PROJECT.md`, report
   files and commit messages — i.e. it depended on an agent having read them.

**Conclusion:** do not build a tenth mechanism. Build the missing *layer* — one
machine-readable policy, one engine, two chokepoints — and leave the nine specialist
guards in place underneath it.

---

## 2. What was implemented (Phase 1)

### Artefacts

| File | Role |
|---|---|
| `docs/OWNER_OS_OPERATING_CONSTITUTION.md` | the law, rule ids `R1…R12`, explicit about what is enforced vs policy-only |
| `config/owner_os_policy.yaml` | machine-readable: risk classes, evidence schema, hard-block / owner-gate / mutating / read-only patterns, redaction, concurrency, override limits |
| `core/policy_engine.py` | the mechanism: `preflight()`, `completion_gate()`, `explain()`, override grant/revoke/list, claims, redaction, audit |
| `core/control_plane/store.py` | schema v6: `policy_decision`, `policy_override`, `policy_claim` |
| `core/job_executor.py` | both chokepoints wired into the real runtime job pipeline |
| `api/v1.py` | `GET /policy/explain`, `/policy/decisions`, `/policy/overrides` |
| `tests/conftest.py` | `CONTROL_PLANE_DB` forced to a temp file so a test run cannot leave claims that block live work |

### The two chokepoints, on the execution path

* **Preflight** runs in `_run_pipeline` **after** the backup exists and **before** the
  first edit. A prohibited, owner-gated, out-of-scope or duplicated job stops without
  having touched the workspace. Verified by test: the workspace file does not exist after
  a blocked run.
* **Completion gate** runs inside `_finish`, the single chokepoint every terminal
  transition already passed through. A `completed` claim without the evidence its risk
  class demands is recorded `blocked` with the missing fields named — never green.

Enforcement is ON by default (`OWNER_OS_POLICY_ENFORCE=1`). The kill switch exists for
diagnosing the policy layer itself and is covered by a test asserting the default is
enabled, so an agent cannot reach the unenforced path by doing nothing.

### Evidence is structured, not narrative

`rollback{kind,ref,verified}`, `baseline{head,branch,clean_tree}`, `changed_files`,
`tests{ok}`, `live{service,active}`, `artifacts`, `owner_approval{gate_id,actor}`. A
rollback whose `kind` is `none` or whose `ref` is empty is rejected as no rollback at all
(R1.7) — the direct analogue of the owner_push false-green fixed earlier the same day,
where a *configured* channel was read as a *working* one.

### Override

Owner-scoped, reason-mandatory, TTL-capped at 1 hour and evaluated on read, consumable,
revocable, and listed in `/policy/overrides` including expired and revoked entries. There
is no code path that removes an override from that listing.

---

## 3. Tests

`tests/test_owner_os_policy.py` (35) — engine semantics:
mutating-without-rollback blocked · read-only needs no backup · unclassifiable ⇒ mutating ·
production restart requires owner · approved restart proceeds · six destructive commands
hard-blocked *even with owner approval* · out-of-scope target blocked · duplicate task
blocked, same-task retry allowed, released claim reusable · DONE without tests/live
refused · failed tests are not evidence · failed health ⇒ `unverified` · secret redaction
including nested structures · override works / expires / is listed / non-owner refused /
reason-too-short refused / revocable · every evaluation audited · missing policy file is a
hard error, not a free pass · self-declaration can only raise risk.

`tests/test_policy_enforcement_pipeline.py` (9) — the same rules **through the real
executor**: prohibited job blocked before editing · owner-gated job blocked · approved
owner-gated job runs but still owes live evidence · ordinary job still completes
(non-regression) · audit rows for both phases · `_finish` refuses a rollback-less
completion · accepts an evidenced one · the kill switch defaults to enabled.

### What the first full run caught

The first full-suite run with enforcement on was **not** green: 1678 passed, 2 failed. Both
failures were real, and both were defects in this change rather than tests to adjust away:

1. `test_timeout_falls_back_and_reaches_coding_stage` — the preflight hook appended its
   backup artifact to the artifacts snapshot taken when the pipeline *started*, silently
   dropping the `fallback_planning` artifact recorded by an earlier stage. Fixed by
   re-reading the job before appending.
2. `test_finish_leaves_a_real_implementation_completed` — a fully-mocked store meant the
   gate read no job record and correctly refused DONE. The unit test's invariant is the
   outcome mapping, so it now presents a job that has a rollback path; a **new** test
   asserts the fail-closed direction explicitly: an unreadable job record cannot be
   completed. Fixing this also surfaced that a failure inside the gate's own logging could
   propagate out of `_finish` — bookkeeping must never undo a decision, so it is contained.

**Full suite after the fixes: 1681 passed, 0 failed** (`pytest tests/ -q -p no:randomly`,
17m07s) — 1636 before this work, +45 new. No commit or restart was made against the red run.

---

## 4. Live status

Enforcement is code on the job path, active for every job the running service starts after
the restart. Schema v6 tables are additive; the migration is forward-only and reversible by
dropping the three tables (`policy_decision`, `policy_override`, `policy_claim`) — no
existing column or row is altered.

During the first run of the job-pipeline tests, 13 decision rows and 8 claims were written
into the **live** control-plane DB (project paths under `/tmp/pytest-…`), because
`conftest.py` pinned `RUNTIME_DB` but not `CONTROL_PLANE_DB`. Both were removed
(`DELETE … WHERE project LIKE '/tmp/pytest%'`, verified 0 remaining) and the conftest gap
was closed so it cannot recur.

## 5. Rollback

* **Code:** `git revert <commit>` — the change is additive; reverting restores the previous
  pipeline exactly.
* **Enforcement only:** set `OWNER_OS_POLICY_ENFORCE=0` in `configs/.env` and restart
  `ai-runtime.service`. The engine still records decisions; it stops blocking.
* **Schema:** `DROP TABLE policy_decision; DROP TABLE policy_override; DROP TABLE
  policy_claim;` — nothing else references them.
* **Data:** `backups/owner_os_constitution_20260806_222454/` restores `config/` and all
  three databases as they were at `cc26fc8`.

## 6. Remaining phases (not claimed as done)

| Phase | Work | Why it is not in Phase 1 |
|---|---|---|
| **2** | Route `control_plane/actuator.py`, `os_task_queue.py`, `commander_autopilot.py` and `direct_pane_control.py` through `policy_engine.preflight` | each has its own live guard and its own idempotency ledger; folding them in needs their tests rewritten around the shared vocabulary, which is a larger change than one safe commit |
| **3** | Clean-tree + baseline HEAD capture as enforced preflight evidence (R1.3, R5.1) | needs a git-state collector on every mutating path, not just the job pipeline |
| **4** | File-mode/owner capture for config backups (R1.6) and dry-run restore verification for high-risk changes (R1.7) | requires a restore-rehearsal harness |
| **5** | Owner-gate wiring: `REQUIRE_OWNER` opens a control-plane gate and consumes a provenance-checked `owner_decision` (R3, R11.2) | the gate/provenance machinery exists; connecting it changes owner notification flow, which is deliberately untouched today |
| **6** | Resource guardrails for heavy builds (R9.3) and model-routing budget gate integration (R10.3) | separate subsystems with their own budgets |

Phase 1 blocks the cases the owner named as mandatory: destructive actions, missing
rollback, unapproved production changes, duplicate work, secret leakage into reports, and
DONE without evidence. The phases above extend coverage to more call sites; they are not
required for those blocks to be real today.
