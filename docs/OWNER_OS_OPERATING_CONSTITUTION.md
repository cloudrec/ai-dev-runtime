# Owner OS Operating Constitution

**Status:** in force. **Enforced by:** `core/policy_engine.py` against
`config/owner_os_policy.yaml`. **Audited in:** `policy_decision`, `policy_override`,
`policy_claim` (control-plane DB, schema v6).

This document is the law. It is not a reminder, a style guide, or a checklist to read at
the start of a task. Every rule below that carries a **rule id** is implemented as a gate
on the execution path, and the gate does not care whether the agent read this file. An
agent that has never opened this document is subject to exactly the same blocks as one
that quotes it.

Where a rule is enforced, this document says so and names the id. Where a rule is
currently **policy-only** (stated, audited when it reaches a gate, but not yet blocking on
every path), it says that too. Nothing here claims enforcement it does not have —
overstating coverage would be the same false-green this system exists to eliminate.

---

## 0. How enforcement works

Two chokepoints, both fail-closed:

| Phase | When | Question it answers |
|---|---|---|
| **preflight** | before a mutating action runs | may this happen at all, and is there a way back? |
| **completion gate** | before anything is called DONE | does the evidence support the claim? |

Four decisions, most severe first:

| Decision | Meaning |
|---|---|
| `HARD_BLOCK` | never proceeds. Only an owner-scoped, expiring, audited override can lift it. |
| `REQUIRE_OWNER` | proceeds only with a recorded owner approval for this specific gate. |
| `REQUIRE_EVIDENCE` | proceeds only once the required structured evidence exists. |
| `ALLOW` | proceeds, and the decision is still written to the audit. |

Four risk classes, assigned by the policy — never by the agent's own optimism. A caller
may **declare a higher** class than the policy assigns; it can never declare a lower one.

| Class | Examples | Backup before | Evidence before DONE |
|---|---|---|---|
| `READ_ONLY` | read, grep, status, audit | not required | summary |
| `MUTATING` | edit files, commit, migrate | required | rollback, baseline, changed files |
| `HIGH_RISK` | restart a service, install a dependency, push | required | + tests, live state |
| `IRREVERSIBLE` | money, external messages, DNS, secret rotation, destructive data ops | required | + owner approval |

**Deny-by-default:** an action the policy cannot classify is `MUTATING`, never
`READ_ONLY`. Not recognising something is a reason to be careful, not a reason to relax.

---

## 1. Reversibility and backup — `R1`

Before any mutating action there must exist an adequate, **verifiable** path back.

- **R1.1** — a mutating action with no recorded rollback reference is `REQUIRE_EVIDENCE`
  and does not run. *Enforced (preflight).*
- **R1.2** — read-only work requires no backup. Enforced by classification: `READ_ONLY`
  carries `backup_required: false`. *Enforced.*
- **R1.3** — code: clean-tree check, recorded baseline HEAD/branch, and a list of the
  changes that were already there. Another agent's uncommitted work is never destroyed.
  *Enforced at completion via `baseline` evidence; clean-tree capture is Phase 2.*
- **R1.4 / R1.5** — mass filesystem deletion and destructive database operations
  (`drop`, `truncate`, unfiltered `delete from`) are `HARD_BLOCK`. *Enforced.*
- **R1.6** — configs, systemd units, nginx, env files: a timestamped backup recording
  permissions/owner/mode plus the exact rollback command. Secrets never appear in the
  report. *Policy-only for the file-mode capture; the rollback reference itself is
  enforced.*
- **R1.7** — a backup counts as existing **only** if the artefact is real and recorded in
  evidence. A rollback naming no restorable reference (`kind: none`, empty `ref`) is
  rejected as no rollback at all. *Enforced.*
- **R1.8** — the backup form follows the risk, not the byte count. Copying gigabytes for
  a one-line change is not diligence; a **proven** rollback is. *Policy-only (guidance).*

## 2. Scope and ownership — `R2`

- **R2.1** — an action whose target lies outside the task's declared scope is
  `HARD_BLOCK`. *Enforced (preflight, when the task declares a scope).*
- **R2.2** — no silent scope growth: fixing a neighbouring project, activating deferred
  changes, or "while I'm here" work is a separate task with its own approval.
- **R2.3** — touching another project's tree (`/opt/...`) is owner-gated. *Enforced.*
- **R2.4** — another agent's worktree, branch, or uncommitted changes are off limits.
- **R2.5** — if a change unavoidably activates other commits or configs, preflight must
  surface it and it becomes an approval gate rather than a side effect.

## 3. Production and irreversible actions — `R3`

- **R3.1 / R3.2** — deploy, publish, release, and service start/stop/restart/reload are
  `REQUIRE_OWNER` by default. *Enforced.*
- **R3.3** — DNS, firewall, and certificate operations are `IRREVERSIBLE` and owner-gated.
  *Enforced.*
- **R3.4** — after a production change: active state, PID, start time, restart count,
  HTTP health, log errors, functional smoke. A failed health check produces
  `unverified` + rollback-required, never a completion. *Enforced (completion gate).*
- **R3.5** — "the command exited 0" is not "the change worked". A command's exit status
  is an input to verification, not a substitute for it.

## 4. Proving the result — `R4`

- **R4.1** — DONE without the evidence its risk class requires is refused and recorded as
  `unverified`. *Enforced (completion gate).*
- **R4.2** — `BUILD ≠ TESTED ≠ DEPLOYED ≠ PUBLISHED ≠ VERIFIED`. These are separate
  states and are never collapsed into "done".
- **R4.3** — tests that ran and failed are not evidence: `tests.ok` must be true.
  *Enforced.*
- **R4.4** — a test must prove the behaviour it claims and be non-vacuous; where possible
  it reproduces the original defect without the fix. *Policy-only (review discipline).*
- **R4.5** — no real device or environment means `BLOCKED_EXTERNAL` / `UNVERIFIED`, never
  `PASS`.

## 5. Git and history — `R5`

- **R5.1** — clean-tree check before work; baseline HEAD recorded.
- **R5.2** — minimal thematic commits; independent tasks are not mixed.
- **R5.3** — force-push, `reset --hard`, history rewriting, and branch deletion are
  `HARD_BLOCK`. *Enforced.*
- **R5.4** — `git push` is owner-gated unless a narrow pre-approved routine-push policy
  applies (`core/git_push_policy.py`). *Enforced.*
- **R5.5** — secrets, binary keys, and private data are never committed. *Enforced
  separately by the staged-diff secret scan in the job pipeline.*

## 6. Secrets and data — `R6`

- **R6.1** — reading or transmitting a secret file through a shell command is
  `HARD_BLOCK`. *Enforced.*
- **R6.2** — every reason, evidence blob, and audit row is passed through redaction
  before it is stored, so a secret cannot reach a report by accident. *Enforced.*
- **R6.3** — rotating or revoking credentials is `IRREVERSIBLE` and owner-gated.
  *Enforced.*
- **R6.4** — least privilege; user data and conversations are read only to the minimum
  the task requires.
- **R6.5** — production data is not copied to a less protected environment without
  permission and anonymisation.

## 7. Agents and concurrency — `R7`

- **R7.1** — one live agent per project/worktree unless isolated worktrees were created
  deliberately.
- **R7.2** — the same action claimed by a second task inside the duplicate window is
  `HARD_BLOCK`. A retry by the **same** task is not a duplicate. *Enforced.*
- **R7.3** — every mutating action carries task id, actor, project, scope, and an
  idempotency key. *Enforced (the claim is keyed by project + action).*
- **R7.4** — a re-run never repeats an irreversible action.
- **R7.5** — typing into a tmux pane is not proof of execution; acknowledgement and
  evidence are. *Enforced by the task ledger (`core/os_task_queue.py`).*

## 8. Fail-closed — `R8`

- **R8.1** — unknown state, hash/version mismatch, missing backup, ambiguous target,
  scope conflict, failing tests, or failing health ⇒ stop. Not "probably fine".
  *Enforced: an unreadable or unparseable policy file blocks rather than allows.*
- **R8.2** — errors are never masked or reported as success.
- **R8.3** — every rollback, failure, and incident is recorded honestly with its timeline
  and actual impact.
- **R8.4** — emergency override is owner-scoped, carries a real reason, expires
  (≤ 1 hour), is consumed visibly, and cannot be hidden from any report. *Enforced.*

## 9. Dependencies and host resources — `R9`

- **R9.1** — `apt`/`npm`/`pip`/`docker` installs are `HIGH_RISK` and owner-gated:
  lockfile, disk/RAM impact, and a rollback path first. *Enforced.*
- **R9.2** — unrelated services are not restarted through `needrestart`/daemon reload.
- **R9.3** — heavy builds and test runs get resource guardrails and are not parallelised
  into taking the host down.
- **R9.4** — binding a management/CDP/debug port to anything but loopback, or disabling
  the firewall, is `HARD_BLOCK`. *Enforced.*

## 10. External actions and money — `R10`

- **R10.1** — payments, purchases, paid-API escalation, financial operations, and live
  trading are `IRREVERSIBLE` and owner-gated. *Enforced.*
- **R10.2** — emails, Telegram/SMS, customer messages, publications, and applications are
  `IRREVERSIBLE` and owner-gated. *Enforced.*
- **R10.3** — model routing is deterministic/free/cheap first; an expensive model requires
  a recorded reason and a budget gate. *Enforced separately by `core/model_routing.py`.*

## 11. Communication and decisions — `R11`

- **R11.1** — the owner is not asked technical questions the system can safely resolve.
- **R11.2** — the owner is asked only genuine business, risk, or irreversibility gates,
  each with options, a recommendation, and consequences.
- **R11.3** — no repeated identical statuses; notify on meaningful state change only.

## 12. Documentation and completion — `R12`

- **R12.1** — every significant task leaves a durable report: goal, baseline, backup,
  changes, tests, deploy/live state, rollback, limitations.
- **R12.2** — `PROJECT_STATE` and ledgers record facts, never intentions.
- **R12.3** — the tree ends clean, or the uncommitted files are listed with the reason.

## 13. Work cadence and autonomy — `R13`

*(Owner rule, 2026-08-29. Policy-only — a cadence preference, not a machine gate.)*

- **R13.1** — approved project work is not orchestrated as 2–5 minute microtasks that
  repeatedly stop for another owner ping. Prefer large, outcome-oriented end-to-end blocks.
- **R13.2** — inside an authorized block the agent loops autonomously: inspect →
  reproduce/verify → implement → focused tests → full relevant regression → logical commit
  → update handoff/roadmap truth → immediately take the next substantive safe/reversible
  item.
- **R13.3** — a small fix is a checkpoint, not a stopping condition. Continue through
  multiple related fixes/commits within the same authorized block.
- **R13.4** — stop and ask the owner only for a genuine external, irreversible,
  credential/access, business-policy, safety/control-plane, or materially ambiguous
  decision that cannot be resolved with a neutral, reversible default/interface/fixture.
  This is the same bar as **R11.1/R11.2**, applied to cadence rather than content.
- **R13.5** — a local blocker on one path is not a global stop: continue independent safe
  work elsewhere in scope.
- **R13.6** — microsteps are fine internally for correctness and testing, but are not
  exposed as separate owner-gated work chunks.
- **R13.7** — do not create polling/heartbeat loops merely to keep work alive.
- **R13.8** — every rule elsewhere in this document (R1–R10, especially scope in `R2` and
  fail-closed in `R8`) overrides this section. Cadence never widens what is safe.

---

## Emergency override

An override is the only way past a `HARD_BLOCK`. It is deliberately uncomfortable:

- only an actor in `override.allowed_actors` (default: `owner`) may grant one;
- it requires a real reason (minimum length enforced, not a placeholder);
- it expires — TTL is capped at `override.max_ttl_secs` (1 hour) and evaluated on read,
  so an override cannot outlive its window by not being cleaned up;
- it may be scoped to specific rule ids;
- each use increments a counter and stamps the decision row with the override id;
- `GET /api/v1/policy/overrides` lists every override ever granted, including expired and
  revoked ones. There is no code path that removes one from that listing.

## Asking the policy

```
GET /api/v1/policy/explain?action=systemctl+restart+api     # decision + why, no side effects
GET /api/v1/policy/decisions?task_id=<id>                   # the durable audit
GET /api/v1/policy/overrides                                # every override, always
```

`policy_engine.explain()` is the same evaluation without claims or override consumption,
so an agent may consult the constitution before acting without changing its state.

## What is enforced today, and what is not

**Enforced on the execution path (Phase 1):** the runtime job pipeline
(`core/job_executor.py`) evaluates preflight before its first edit and the completion gate
before any `completed`, with `OWNER_OS_POLICY_ENFORCE=1` by default.

**Not yet routed through the gates (Phase 2+):** the control-plane actuator, the task
ledger dispatcher, the commander autopilot, and direct pane control each retain their own
deny-by-default guards (`permission_resolver`, `service_ops_policy`, `approved_gates`,
`git_push_policy`) but do not yet call the unified engine. Their existing guards are
narrower in vocabulary, not weaker in intent. Phases are listed in
`reports/OWNER_OS_MANDATORY_POLICY_IMPLEMENTATION_2026-08-06.md`.

A rule marked *policy-only* above is real law and will be cited in review, but it is not
yet a machine gate. That distinction is stated deliberately: a constitution that claims
enforcement it does not have is the same false-green it forbids.
