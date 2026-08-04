# OWNER OS — AUTONOMY PHASE 2 (LIVE, CONTINUOUSLY UPDATED)

**Status: WORKING.** Started 2026-08-04. This file is the durable record of the assignment
and its progress — written before any coding so context compaction cannot lose it.

## Assignment (owner, verbatim intent)

Extend the existing narrow autopilot for **allowlisted non-Payment development agents
only**. Remove the three honest limitations of ZERO_HUMAN_PING v1:
1. terminal state is not sticky,
2. an externally killed allowlisted session needs a human restart,
3. evidence exists for only ~30 minutes.

**Hard boundaries (unchanged from v1, all preserved):** no payment execution, no real
orders, no credentials, no external publishing, no destructive operations, no duplicate
project agents. **Fable prohibited.** Opus owns architecture, deploy, live acceptance and
the final verdict; at most ONE Sonnet subagent for bounded tests/log review, reviewed by
Opus.

### A) Durable terminal/work state
Per-target store keyed by target + project cwd + conversation id. Persist
`terminal_pass` / `terminal_blocked` only after verified report/evidence, with timestamp,
evidence fingerprint, git HEAD / report mtime, and reason. Sticky across pane scroll AND
service restart. Reopen ONLY on a material signal: git HEAD change, declared
current-task/report fingerprint change, owner command, explicit new queued task, or a
configured freshness deadline. **Pane text scrolling alone must never reopen it.**
CLI/API readout + audit trail. Fail closed on corrupt state.

### B) Safe dead-session recovery
Explicit managed-session registry for ONLY `cp-canary:0.0`, `mess-qa-automation:0.0`,
`arbitrage2-opus:0.0`. **Payment excluded.** Registry stores exact session name, cwd,
approved conversation id, resume command shape, enabled flag. No discovery-based creation.
On a dead pane / absent Claude process: prove no live Claude for the same cwd and no
duplicate target; distinguish deliberate stop/quarantine from unexpected death; revive the
exact tmux session/pane and resume the exact approved conversation. **Never a second live
pane for the same cwd.** If Claude offers the large-session choice, always pick
**"Resume from summary"**, never a costly full replay. Verify recovery by PID, cwd,
one-pane invariant, prompt readiness and conversation modification BEFORE sending work.
Crash-loop protection: exponential backoff, max 3 recoveries per target per 6h, then
quarantine + a genuine owner blocker. Recovery itself authorises no new work; the existing
safe-step allowlist stands.

### C) Live gate path on the safe canary
Exercise ONE approved-gate answer end to end on `cp-canary` only, with a harmless exact
command created for this acceptance. Prove: exact-match approval, wrong-wording refusal,
expired-approval refusal, one-copy delivery. **Payment must not be used for this test.**

### D) Long soak
Minimum **6-hour** live recorder started immediately after deploy, designed to reach 24h
without an agent holding an interactive shell; restart-persistent. Samples: managed
sessions, duplicates, recovery counts, terminal stickiness, queue stalls, unapproved
answers, service health, audit-log integrity. 6h checkpoint → `PHASE2_SOAK_6H_PASS`;
24h → `PHASE2_SOAK_24H_PASS`. The phase is NOT PASS before ≥6h plus all A–C live.

### E) Tests / deploy
Unit + integration for: state corruption, stale fingerprints, material-change reopening,
duplicate detection, exact recovery, summary-choice handling, deliberate stop, crash-loop
quarantine, service-restart persistence, safe-canary gate path. Backup before deploy; no
push unless standing policy already permits; restart only the required ai-runtime service.

**Verdicts:** `OWNER_OS_AUTONOMY_PHASE2 = PASS / PARTIAL / BLOCKED`, evidence-based. While
the soak runs: **WORKING** with completed A–C evidence. Never fake a PASS.

## Starting point (v1, verified)

- HEAD at assignment: `0a8074e`; deployed code `4ed8d93`; suite 1301 passed.
- Service `ai-runtime.service`, PID 3598565.
- v1 verdict `OWNER_OS_ZERO_HUMAN_PING = PASS` — see
  `reports/OWNER_OS_ZERO_HUMAN_PING_FINAL_2026-08-04.md`.
- Allowlist `cp-canary:0.0, mess-qa-automation:0.0, arbitrage2-opus:0.0`; payment excluded.
- Gate registry `config/approved_gates.yaml` (7 entries, scoped + expiring).

## Progress log

| when | item | status |
|---|---|---|
| start | assignment persisted | done |
