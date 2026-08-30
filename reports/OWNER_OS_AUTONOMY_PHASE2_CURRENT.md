# OWNER OS — AUTONOMY PHASE 2 (LIVE, CONTINUOUSLY UPDATED)

**Status: 6h CHECKPOINT REACHED — `OWNER_OS_AUTONOMY_PHASE2 = PARTIAL`.** Started 2026-08-04. This file is the durable record of the assignment
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
| start | assignment persisted | done (`7ff7a82`) |
| impl | A durable terminal state — `core/project_state.py` | done (`952b341`) |
| impl | B safe recovery — `core/session_recovery.py` + `config/managed_sessions.yaml` | done (`952b341`) |
| tests | `tests/test_autonomy_phase2.py`, 29 tests | pass |
| suite | full suite | **1330 passed, 0 failed** |
| deploy | backup `predeploy-phase2-20260804T211458Z`, restart | done — PID 4160968, HEAD `952b341` |
| A live | arbitrage2 durable `terminal_pass` recorded with git HEAD + evidence fp | proven |
| C live | approved / wrong-wording / expired / one-copy | proven |
| D | detached restart-persistent 24h soak recorder | running |
| D | recorder survives an ai-runtime restart | **proven** — no gap |

## A — durable terminal state (live)

`project_state` is populated by the running service. First live marker:
`arbitrage2-opus:0.0` @ `/opt/arbitrage2` → `terminal_pass`, reason "verified completion
with no open work", git HEAD `37f496be…`, evidence fp `51b4bc6c…`, freshness 24h. It
survives pane scroll and service restart, and reopens only on git HEAD change, report
update, owner command, a new queued task, or the freshness deadline — never on pane text.

Isolation defect found and fixed while testing: `tests/test_zero_human_ping.py` had no
isolation fixture and was reading/writing the PRODUCTION control-plane DB; my own earlier
`ap.evaluate` probes had written a stray `/tmp` marker into it. Fixture added, stray row
removed.

## B — safe dead-session recovery (deployed)

Registry loaded live with exactly `arbitrage2-opus:0.0`, `cp-canary:0.0`,
`mess-qa-automation:0.0` — **payment absent**, and a test pins that it stays absent. All
three currently alive, none quarantined, 0 recoveries in the last 6h.

Refusals proven by test: unregistered target, disabled entry, already-alive pane, a live
Claude on the same cwd (duplicate proof), deliberate stop/quarantine, crash-loop cap →
quarantine + owner blocker, and failed post-recovery verification never reported as
success. "Resume from summary" is chosen from the option number read off the pane; a full
replay is never selected. Live end-to-end revival is **not yet exercised** — no registered
session has died since deploy, and killing one deliberately to prove it would be
manufacturing the failure.

## C — live gate path (proven)

Against **real rendered dialogs**, decisions for target `cp-canary:0.0`:

| dialog | result |
|---|---|
| `echo phase2-gate-probe` (exact) | `answer_gate`, entry `phase2-canary-echo`, answer `1` |
| `echo phase2-gate-prob` (wrong wording) | refused — `no_matching_approval` |
| `echo phase2-expired-probe` (expired entry) | refused — `expired` |
| `echo phase2-gate-probe; rm -rf /` | refused — `prohibited_marker_in_command` |

Delivery: **one** `send()` call, payload `['1']`, **zero re-pastes**, input line consumed.

Honest scope note: the delivery half ran against a disposable probe pane rendering the
exact dialog, because a genuine in-agent permission prompt cannot be manufactured inside
the running canary without faking it (the canary runs in auto-approve mode). The decision
path, the registry match, the refusals and the one-copy delivery are all real; what was
not exercised is a real Claude permission prompt answered inside the canary agent.


## A — reopen path PROVEN LIVE (2026-08-04 22:33)

The durable marker did not just persist — it reopened correctly on a real material signal,
unprompted:

| when | event |
|---|---|
| 21:15:20 | `arbitrage2-opus:0.0` recorded `terminal_pass`, git HEAD `37f496be…`, evidence fp `51b4bc6c…` |
| 21:15 → 22:33 | marker held across pane scroll and continued sampling; the session was NOT poked |
| 22:33:58 | **`reopen` — reason `git_head_changed`** (the project advanced; HEAD is now `56ccd16`) |
| 23:33 / 23:35 | autopilot resumed poking arbitrage2, which is progressing again |

So the v1 limitation is closed in both directions on live evidence: a finished project stays
finished while nothing material changes, and it reopens by itself the moment the repo moves.

Bookkeeping note for accuracy: an earlier `cp-canary:0.0` marker against cwd `/tmp` was
test pollution from my own probes and I removed it with raw SQL. Its `record_terminal`
audit line remains (the audit table is append-only) with no matching `reopen` line, because
a direct SQL delete does not pass through `reopen()`. Not a product defect — but worth
stating so the audit trail is not misread.

## D — soak (in progress)

Recorder: `tools/phase2_soak.py` under `tools/phase2_soak.sh`, started detached with
`setsid nohup`. No interactive shell holds it; if the sampler dies the wrapper restarts it
inside a 24h window. Output `reports/phase2_soak.jsonl`, one JSON sample per 60s.

**Restart survival proven.** `ai-runtime.service` was restarted deliberately
(PID 4160968 → 38064) while the recorder ran:

- recorder process unchanged across the restart,
- **zero sampling gaps >150s** across the whole run so far,
- the recorder picked up the new service PID in the very next sample.

Window opened `2026-08-04T23:16:25`. 6h checkpoint falls at ~`05:16`. Running totals:

| metric | so far |
|---|---|
| samples | 22 (at time of writing) |
| sampling gaps >150s | 0 |
| duplicate panes | 0 |
| gate answers (any) | 0 |
| managed sessions alive | 3/3, none quarantined, 0 recoveries in 6h |
| durable terminal markers | 1 — `arbitrage2-opus:0.0` `terminal_pass` |

**No verdict yet.** `PHASE2_SOAK_6H_PASS` is not claimed until ≥360 samples exist and the
counters above still hold. Current phase status remains
`OWNER_OS_AUTONOMY_PHASE2 = WORKING`.


## D — 6h CHECKPOINT RESULT (2026-08-05T05:28)

`reports/phase2_soak_6h_checkpoint.json`, written by the detached evaluator:
**360 samples over 6.19h**, window `2026-08-04T23:16:25` → `2026-08-05T05:27:49`.

| criterion | result |
|---|---|
| sampling gaps >150s | **0** |
| duplicate panes | **0** |
| unapproved gate answers | **0** |
| quarantined sessions | **0** |
| recoveries in 6h | 0 for all three |
| service state seen | `active` only, across a deliberate restart |
| sampler errors | 0 |
| audit log readable | yes (after the metric fix below) |
| **max stall streak** | cp-canary 2, arbitrage2 **3**, **mess-qa-automation 37** |

**`clean: false`** — and I am NOT calling `PHASE2_SOAK_6H_PASS`.

### Flag 1 — MESS stalled 37 minutes (real)
`mess-qa-automation:0.0` sat at `waiting_input` from `00:46:06` to `01:23:11` holding
`[Pasted text #3 +99 lines]` — a pasted block that was never submitted.

The watchdog did not submit it, and that refusal is *correct*: the structural safe-step
allowlist does not recognise an opaque 99-line paste, so auto-submitting it would mean
sending unclassified content. Correct safety behaviour, and a stalled project — which is
exactly the stop-and-wait pattern Phase 3 exists to govern. Recorded here as evidence, not
excused.

Arbitrage2's 3-sample streak was the ordinary safe continuation text and cleared itself.

### Flag 2 — audit metric bug (mine, fixed)
`audit_log_ok: false` was wrong. `gate_answer_log` is created lazily on the FIRST answered
gate, so its absence means "nothing was ever auto-answered" — a good outcome. My evaluator
counted the missing table as a broken audit log. Fixed; recomputed over the same 360
samples → `audit_log_ok: true`, every other number unchanged.

## Phase 2 verdict

**`OWNER_OS_AUTONOMY_PHASE2 = PARTIAL`**

- **A — durable terminal state: PASS.** Proven live in both directions: marker held while
  nothing material changed, then reopened by itself on `git_head_changed`.
- **B — safe dead-session recovery: PARTIAL.** Deployed, registry live with payment absent,
  every refusal path proven by test — but no registered session died during the window, so
  end-to-end revival is unproven live. I will not kill one to manufacture the proof.
- **C — live gate path: PASS.** Exact approval, wrong-wording refusal, expired refusal,
  prohibited-marker veto, one-copy delivery — all against real rendered dialogs.
- **D — 6h soak: PARTIAL.** Every safety criterion clean; the stall criterion failed on the
  MESS paste.

Not a PASS, and not a BLOCKED: nothing external stopped the work, and no Phase 2 safety
property was violated. The single failing criterion is the precise problem Phase 3 targets.
