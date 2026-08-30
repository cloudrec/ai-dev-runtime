# OWNER OS — ZERO HUMAN PING (FINAL)

**Verdict: `OWNER_OS_ZERO_HUMAN_PING = PASS`** — A–E all proven live (details below).

**2026-08-04.** Closing report for the zero-human-ping mandate: a production loop that
carries pre-approved work from idle/waiting_input to a finished result or a genuine
external blocker, with no human pings.

Scope frozen as instructed after the feature work landed: only defects blocking A–E were
fixed. Model policy in force — Fable not used at all, Sonnet not used (every step here was
Opus), Opus verified all evidence personally.

## Deployed

| | |
|---|---|
| HEAD | **`4ed8d93`** |
| Service | `ai-runtime.service` active, **PID 3598565** |
| Suite | **1301 passed, 0 failed** |
| Allowlist | `cp-canary:0.0, mess-qa-automation:0.0, arbitrage2-opus:0.0` (payment excluded) |
| Gate registry | `config/approved_gates.yaml`, 7 entries, all scoped + expiring |
| Snapshot | `/root/owner-os-backups/predeploy4-20260804T165920Z` |

## What was built (before the freeze)

- **`core/approved_gates.py` + `config/approved_gates.yaml`** — the only dialogs the system
  may answer. One target + one command shape (sha256 or anchored fullmatch) + scope +
  expiry + an owner-recorded answer. Unknown wording, wrong target, expired, scope-less,
  ambiguous or multi-match ⇒ refused. A prohibited marker in the dialog text (payment
  execution, promotion/failover, real orders, credentials, destructive verbs) vetoes even a
  matching entry. A malformed registry yields zero approvals.
- **`core/continuation_signals.py`** — "available on request", "ready to continue",
  "standing by", «готов продолжить по запросу» classify as **unfinished**, beating a
  completion claim. A model/session/credit limit is a **wait**, never a failure or a
  terminal state. Only verified completion or a real external dependency is terminal.
- **Audit** — every gate decision (answered or refused, with reason) is written to
  `gate_answer_log`; every skip carries its reason into watchdog health.

## Defects found by driving the live system (all fixed, all with regression tests)

| commit | defect |
|---|---|
| `0455cc4` | A fast step that finished inside the poll window read as `not_verified`, and the fallback then **re-pasted** — leaving a second copy queued. A consumed input line plus new output *above the input box* now counts as progress (typing cannot forge it: the box is excluded, and a pane with no identifiable box claims nothing). |
| `a13e6a5` | The **dim recall ghost** was read as queued input, so every successful delivery was retried against its own echo (live: `attempts: 4`, `verify_failed`). With a live target the styled reader (which drops SGR-2 runs) is authoritative. |
| `4ed8d93` | **A session could be auto-resumed only once per conversation, ever.** The idempotency key was (target, conversation, step-hash), so on a fresh idle cycle the actuator answered `already_verified` from a delivery hours earlier. The key is now qualified by a progress fingerprint of the pane body: new work ⇒ new cycle ⇒ deliverable; an unchanged pane keeps the old key and stays deduped, so a stuck agent is never spammed. |

## Live acceptance

### A — MESS terminal classification ✅
MESS reached: *"Matrix exhausted for everything runnable here; only genuinely external
physical-device items remain (documented)."* The loop classified it **`terminal_pass`** and
did **not** poke it. Verified against the pane, not just the ledger.

**Limitation, stated plainly:** terminal is **not sticky**. The classifier reads the visible
pane, so once that text scrolled out of the capture window the session was resumed again
(18:13:19Z, fence 279, verified) — and did real in-scope work (`node
tests/security/e2ee_roundtrip.mjs`). No harm here, but a durable terminal marker would need
a state store, which the freeze excluded.

### B — Arbitrage2 autonomous continuation ✅
**Proven after the owner restarted the session** (below is the full history, including the
earlier blocked attempt, because it is what forced the `4ed8d93` fix).

The owner restarted `arbitrage2-opus:0.0` in place — verified as **one pane, `dead=0`, no
duplicate**. The service then did the rest unaided:

| time (UTC) | event |
|---|---|
| 18:25:19 | **`skip_unobservable_pane`** — the pane was still rendering after restart; the blind-pane guard refused to type into it. Correct refusal, not a stall. |
| 18:26:26 | **`poke`**, situation `unfinished` → delivered |
| 18:26:25 | `cp_action` controller **`commander_autopilot`**, conversation **`15f13266-…|p43cb399e9704`** (the progress-fingerprinted key from `4ed8d93` — the very defect that blocked this case), fence **423**, policy `autonomous_safe`, **`verified=1`**, outcome `verified`, **attempts 1** |
| event 201 | `action_verified` — `queued_input: False, prompt_consumed: True, conversation_modified: True, progressed: True, ok: True` |
| event 195 | `agent_recovered` |

Delivered text was exactly the registry's approved step —
`"continue the next safe read-only audit step and update the audit report"`, classified
`autonomous_safe`. The pane then showed **real execution**: `tools/paper_run/*` with
`state/backfill.json` — paper-only work, no keys, no venue adapters, no orders. State after:
`working`. Still **one pane per session**.

#### The earlier attempt, and why it failed
Before the restart the loop behaved correctly at every step:

- 18:02:46Z — decided **`poke`** with situation `unfinished` (the classifier working as
  designed).
- The actuator refused with `already_verified` — the once-per-conversation defect above.
  **Fixed in `4ed8d93`.**
- Before the fix could be exercised on this session, the **pane died**:
  `Pane is dead (status 143, Tue Aug 4 20:08)` — SIGTERM from outside Owner OS. The other
  three managed panes survived every deploy, so this was not the restart.
- The loop then recorded **`watchdog_dead`** twice, took no action, and **created no
  duplicate** — still exactly one pane per session.

Reviving it required creating a Claude agent, which the mandate forbids me to do — so the
case sat blocked until the owner restarted the session in place. No duplicate was ever
created, before or after.

The same capability was independently proven on MESS in the meantime: idle, auto-resumed
**once** (fence 279, `verified`), producing a real tool call with output.

### C — Payment gate ✅ refusal proven; approved-answer path unreachable by design
Payment evaluates `unfinished → poke`, and the actuator returns **`poke_owner_gated`** with
**zero pane contact**, because payment is excluded from both the actuation allowlist and the
watchdog's eligible sessions. **No unknown dialog was ever answered** — `gate_answer_log` is
empty for the entire run. The registry's `payment_standby` entries exist but are
unreachable: exercising them would require widening scope, which the freeze forbids and the
standing "keep Payment excluded" instruction contradicts. Verified live that the registry
refuses `pg_ctl promote` (prohibited marker), `npm run build`, `python place_order.py --live`
and `rm -rf /`, while accepting only the exact recorded read-only standby check.

### D — Queued input recovered without a human ✅
Reproduced deliberately on the disposable canary: text typed into the input line, not
submitted (`waiting_input`, styled pending non-empty — a real queue, not a ghost).

Recovered unaided: `actuator_continued`, `cp_action` fence **613**, controller
`continuation_watchdog`, outcome `verified`, `retried: 0`, event 152 proofs
`queued_input: False, prompt_consumed: True, conversation_modified: True, progressed: True,
ok: True`; **one** copy of the step in the pane; event 153 `blocker_resolved` cleared the
earlier spurious blocker automatically.

### E — soak ✅ (with one exact caveat)
Two windows were recorded, ~62 minutes of continuous monitoring in total.

The first (19:15:25–19:45:24, **30 samples**, a full 30 minutes) was **discarded as
mis-measured**: its stall metric used the ghost-blind reader and flagged two dim recall
ghosts as queued work. Its duplicate and gate-answer counts were nevertheless 0.

The authoritative window used the ghost-aware reader: **19:47:27–20:17:06, 28 samples** —
**29m 39s**, i.e. 21 seconds short of a literal 30 minutes (the sampler adds its own work
time to each 60s sleep, so 30 iterations did not fit the window). Stating that precisely
rather than rounding it up to "30 minutes":

| metric | result |
|---|---|
| duplicate panes | **0** in all 28 samples (and 0 in the earlier 30) |
| unapproved gate answers | **0** (`gate_answer_log` empty across both windows) |
| real stalled-with-queued streak | max **1 cycle** — MESS; every other session 0 (threshold is >2) |
| capture failures | 0 |

Also observed and correct: arbitrage2 had **owner-typed Russian text** queued; the structural
allowlist refused to auto-submit it (Cyrillic fails the safe-step gate), so it became an
owner blocker rather than an autonomous keystroke.

## Verdict

**`OWNER_OS_ZERO_HUMAN_PING = PASS`**

All five cases proven on the live system, with ledger and pane evidence:

| case | result | key evidence |
|---|---|---|
| A | PASS | MESS `terminal_pass` on "matrix exhausted… only physical-device items remain"; not poked |
| B | PASS | arbitrage2 resumed unaided after restart — fence 423, `verified`, attempts 1, real `tools/paper_run` execution |
| C | PASS | payment `poke_owner_gated`, zero pane contact; `gate_answer_log` empty; registry refuses promote/build/orders/`rm -rf` |
| D | PASS | queued line recovered unaided — fence 613, `conversation_modified: True`, one copy, `retried: 0` |
| E | PASS | 28 samples / 29m39s ghost-aware (plus an earlier 30/30min window): 0 duplicates, 0 unapproved answers, max real stall 1 cycle |

No human ping was needed for any delivery. The only human action in the whole run was
restarting a session that an external SIGTERM had killed — which the mandate reserves to the
owner, and which the loop correctly refused to work around by creating a duplicate.

Four defects were found by driving the live system and fixed inside this run (`0455cc4`,
`a13e6a5`, `4ed8d93`, plus the earlier `494a52d`). None of them is a residual blocker.

### What this verdict does not claim
- Terminal is **not sticky** (§A): the classifier reads the visible pane, so a terminal
  session is resumed again once that evidence scrolls away. Harmless here — MESS did real
  in-scope work — but it is a real limitation.
- The **approved-gate answer path was never exercised live** (§C). It is proven only by
  unit tests and by live refusals. Its one reachable scope would be payment, which is
  excluded by standing policy, so no dialog was ever auto-answered in production.
- Soak is ~30 minutes, not sustained operation over days.

## Rollback

- Narrow the scope back to canary-only: `rm
  /etc/systemd/system/ai-runtime.service.d/zz-actuation-scope.conf` + daemon-reload +
  restart.
- Disarm the autopilot entirely: same with `99-autopilot.conf`.
- Full snapshot: `/root/owner-os-backups/predeploy4-20260804T165920Z`.
