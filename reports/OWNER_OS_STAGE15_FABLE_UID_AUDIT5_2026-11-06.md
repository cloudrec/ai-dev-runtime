# Stage 15 Fable audit of the uid fixes, 2026-11-06

Audit of `de5ee45..abf4ac9` on `owneros/stage15-agent-uid`, requested by the owner in this session. Run as a subagent through the `Agent` tool with `model: "fable"`.
Model: self-reported Claude Fable 5.1 (`claude-fable-5-1`); the client gives no independent proof. Billing was not checked.
**Verdict: H1 closed for root exec and root write, NOT closed for root read and root wedge. Sandbox uid run: yes, supervised, with 5 conditions. PROD: NO-GO. Nothing deployed.**

Filed by the Opus 5.5 session. Editor's check, code read only, repros not rerun by the filer: the A1 sites (`core/agent_control.py:662`, `core/os_task_queue.py:103`, `core/context_budget.py:232`), the A3 default (`core/agent_privilege.py:50-52`) and A8 (`core/wake_bridge.py:1961-1964`) match the code at `abf4ac9`.

Auditor's evidence: read-only on the repo; repros and mutants in the session scratchpad. `tests/test_agent_privilege.py` and `tests/test_wake_routes.py`: 136 passed, as root with real `setpriv` to `nobody`. Mutants on a scratch copy: 15 of 15 valid ones killed; one was malformed, inconclusive. Full suite not run by the auditor; the filer's run at this tree: 4831 passed, 4 skipped, 0 failed.

## Conditions for a sandbox uid run
1. `AGENT_RUN_AS` set in every process of the instance (API, worker, companion, recovery timer), or the pin written by hand before the first start (A4).
2. `AGENT_CONTROL_DB` set in every shell or tool that carries `AGENT_RUN_AS`; pin file removed in cleanup (A3).
3. `PROJECT_SUPERVISOR_PROJECTS` unset and no governor pointer inside the agent tree (A1, A5).
4. Project is a full clone chowned to the agent with a real `.git` directory; a `git worktree` gitfile checkout is refused.
5. Accept that the agent can OOM the sandbox API through A1 until fixed, so do not share the instance with live work.

## Findings

### MEDIUM-HIGH
**A1: unbounded root reads of agent-owned files. CONFIRMED under a memory cap.** A sparse file costs the agent no blocks and costs root its memory.
- Sites: `core/agent_control.py:662` full-file fallback (reached from `agent_list` and `agent_status`, the most-called path; the agent's newest transcript only needs no user line in the tail); `core/os_task_queue.py:103` (one huge line); `core/project_supervisor.py:149` and `:239`; `core/context_budget.py:232` (rotation loop; the agent owns `CONTEXT_CHECKPOINT.md`); `core/continuation_governor.py:238,255,346,413`; `core/work_evidence.py:405` (size checked at listing time only).
- Repro: 6 GB sparse `PROJECT_PLAN.md` owned by `nobody`, `RLIMIT_AS` 1.5 GB. `parse_plan` and `verify_checkpoint` raise `MemoryError`, which escapes; the governor swallows it as `queue_unreadable`.
- Reasoned, not run: uncapped, a 100 GB file leads to OOM kill, systemd restart, repeat.
- Fix: `open_regular(path, *, max_bytes=...)` checks `os.fstat(fd).st_size` on the same fd and raises `OSError` over the cap; bound the transcript fallback and the line length; catch `MemoryError` in `parse_plan` and `verify_checkpoint`.

### MEDIUM
**A2: `open_regular` guards the last path component only. CONFIRMED.** An agent-owned directory symlink redirects root into a root-only tree when a file of the expected name is there.
- Repro: `project/.owner-os` to a root-only 0700 dir: `read_handoff` returned root's JSON. `project/.claude` to the same dir: `settings_api_overrides` reported key names from a root-only `settings.json`. The agent itself cannot read that file.
- Same pattern: `reports/`, `docs/`, `~agent/.claude/projects/<slug>`. Audit 4's F5 is a special case of this.
- Content goes to the evidence DB, notifications and refusal reasons, not straight back to the agent.
- Fix: containment root for `open_regular`, `transcripts()` and `_iter_reports`: `realpath` under the root's `realpath`, or walk with `openat(O_NOFOLLOW|O_DIRECTORY)`.

**A3: stray `AGENT_RUN_AS` can flip the live instance. PLAUSIBLE, code read, deliberately not executed. Audit 4's G2, made worse.** `_pin_path()` defaults to `/root/ai-dev-runtime/agent_control.db.run_as`. Since `de7bd42` every drop pins. A hand-run tool in a shell with `AGENT_RUN_AS` but without `AGENT_CONTROL_DB` writes the live pin. Live services then obey it: root tooling refused, git evidence unknown, `agent_resume` drops to the sandbox user, every send refused with `pane_uid_mismatch`. The test suite is safe (`tests/conftest.py:36`).
- Fix: with `AGENT_RUN_AS` set and `AGENT_CONTROL_DB` unset, refuse (`pin_dir_unset`). Preflight prints the pin path. Trial cleanup removes `<db>.run_as`.

**A4: G1 is half closed. PLAUSIBLE.** A process of the instance without the variable that acts first after a restart sees no setting and no pin, and starts a root `claude` in the agent-owned tree. Preflight does not pin, and `test_preflight_checks_without_pinning` locks that in.
- Fix: `--pin` in `tools/agent_uid_preflight.py`, written when all checks pass, or the variable in every unit plus a runbook line.

### LOW-MEDIUM
**A5: functional cost under the setting. CONFIRMED by code.** `project_supervisor.validate_handoff` always fails, so the loop waits forever: `project_supervisor` cannot work on a uid instance. Fails closed, acceptable: push always denied, commit evidence empty, `stage_done` and hooks dark. The fullcycle engine needs none of these.

### LOW
**A6: `agent_report` walk. PLAUSIBLE.** `core/agent_control.py:2547` walks through a symlinked `reports/`. Names are filtered, nothing leaks, but root walks any readable tree per MCP call. Fix: refuse a symlinked base.

**A7: G3 and G4 unchanged.** Pin dir not in the preflight deny set. Recovery hands tmux one string; prefix `exec`.

**A8: wake reporting. PLAUSIBLE.** With the head candidate in backoff and the only other candidate unrouted, `pending_wake` returns `no_route_bound` without the retry detail. Reporting only.

## Verdicts per question
1. H1: closed for root exec and root write. Open for A1 (wedge) and A2 (read). Other root-on-agent-content sites found are guarded or stat-only, apart from A6.
2. TOCTOU: `open_regular` is sound for the last component and FIFOs. Parent symlinks bypass it (A2). Hard links are moot. `_owns_tree` races only change where the agent's own pane starts. `pin` holds against symlinks and concurrent writers.
3. Pin: holds for processes that carry the variable; not for those without it before the first pin (A4). The agent cannot reach it.
4. `AGENT_RUN_AS` unset: every gated change is a no-op. The cost is one `lstat` per guarded call. The only ungated change is the wake queue (`2c7f4c3`). Residual hazard: A3.
5. Loop under the setting: fullcycle engine and recovery work; `project_supervisor` cannot (A5). The rest fails closed, acceptable.
6. Wake queue: no starvation, no wrong-route delivery, no new reporting loop; minor reporting loss (A8).
7. Tests: none in the range passes with its fix removed. Weak spots: `test_a_new_root_git_site_has_to_be_guarded` works per file and scans only `core/` and `runtime/`; `tools/fullcycle_demo.py:74` is unseen. No test for a size bound or directory containment.
8. F5 is the general pattern A2. G2 is worse (A3). G3 and G4 unchanged. F1-F4 not made worse.

## Not checked by the auditor
Live unit environments and live pin absence. A real OOM. The `work_evidence` stat/read race. tmux multi-arg exec beyond the existing test. `hooks/owneros_hook.py` and `tools/stage_done.py` under the setting, code read only. `api/` and `core/control_plane/` beyond greps. cdp and companion code. The full suite.
