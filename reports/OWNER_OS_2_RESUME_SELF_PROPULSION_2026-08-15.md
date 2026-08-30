# Owner OS 2.0 — resume after pause: self-propulsion hardening + Venture Radar/Business Analyzer cores

Date: 2026-08-15 (evening). Branch `ai-runtime/182-retry-fix-wake-continuation-star`,
head `cf0a7e2`. Full suite: **2044 passed** (was 2022 at the pause point; the
pause left the full suite unproven after 7a95d9c — this run also retro-proves it).

## What the owner asked

Resume from the pause handoff. Priority 1: the system reliably self-propels
stalled/idle/waiting agents with no manual chat pings; close Stall Doctor /
wake / supervisor gaps proven by live tmux behavior. Priority 2: continue the
approved Venture Radar (193) / Business Analyzer (202) work. Preserve
unrelated dirty files; no external publishing/payments/accounts/credentials/ad
spend; stop at true owner gates.

## Live evidence reviewed before changing anything

The doctor has been actuating all day (ledger `stall_doctor_action`):
- Post-Cyrillic-fix (7a95d9c) it auto-submitted the owner's own queued lines
  with delivery verified on gaika-video (10:22), gaika-presentation,
  gaika-ext-audit, bootstrap, diamond-auction, treasure-opus-audit (10:56,
  16:46, 16:51), capacity-blockchain (17:05 and 17:25). The original
  "gaika-video lost continuation" defect class is closed and keeps proving
  itself live.
- Two escalations 17:10/17:17 (chemmy-fast, treasure-opus-audit) on queued
  lines refused by the denylist (`forbidden_token` — treasure's line contains
  "token"). Fail-closed refusal + one owner ping is the designed behavior.

## Gap 1 (fixed, 41c9788): stale escalations were never retired

chemmy-fast escalated 17:10, resumed working minutes later — its
`agent_waiting_input` owner-action event and wake stayed live. The doctor now
retires escalations exactly like agent_watch retires crash alerts: when the
episode resolves (shape returns NONE/OWNER_WAIT, or the digest moves on), the
event gets an audited `mark_invalid` overlay and its wake is acknowledged.
Ledgered as `retire_escalation` rows.

Live cleanup done with the same audited APIs: retired 7 stale escalations
(5149, 5155, 5158, 5160, 5179, 5180 — all pre-Cyrillic-fix episodes later
resolved by verified auto-submits — and 5327 chemmy-fast). Event 5337
(treasure-opus-audit) deliberately **kept**: that stall genuinely stands, the
queued line "wait for JobHunter token…" is denylist-refused, so it is the
owner's call.

Tests: retirement on resume, retirement on digest move, standing escalation
NOT retired (positive control).

## Gap 2 (fixed, 41c9788): failed deliveries waited the full 1800s cooldown

gaika-presentation 10:28: submit verify=False, then ~30 idle minutes before
retry eligibility. A failed delivery proves nothing about the pane, so it now
retries after `FAILED_RETRY_COOLDOWN_SECS` (180s, env `STALL_DOCTOR_FAILED_RETRY_SECS`);
verified deliveries keep the long clock; the per-episode loop guard still caps
total attempts. New `last_action_ok` column, migrated automatically (verified
on the live DB).

## Venture Radar core — task 193 (cf0a7e2)

`core/venture_radar.py`, record-and-stop (portfolio.py discipline — no
dispatch, no network, no spend):
- Closed card vocabulary (15 fields: problem, target_buyer, why_now,
  market_evidence, differentiation, synergy, mvp_scope, monetization,
  acquisition, cost, difficulty, risks, validation_experiment, kill_criteria,
  confidence); unknown fields refused; 5 required.
- Modes `adjacent_recombination` / `fresh_niche`.
- Fail-closed lifecycle IDEA→RESEARCHED→PROPOSED→APPROVED|REJECTED,
  APPROVED→BUILDING. APPROVED/REJECTED/BUILDING **owner-only** (`by="owner"`),
  durable `vr_decision` ledger, card frozen after the owner decides.
- Blunt explainable score: `100 * confidence / (cost * difficulty)`, clamped.
- Seed thesis recorded live: candidate `2e2e165a1a82` "GEO/AEO visibility
  product", state IDEA, score 12.5 — with the owner's zero-replies note kept
  as a deliverability problem to diagnose, not hidden.

## Business Analyzer core — task 202 (cf0a7e2)

`core/business_analyzer.py`, same discipline:
- Competitor opportunity cards; seven fixed axes (profitability_potential,
  cloneability, improvement_leverage, speed_to_market, capital_intensity,
  competition_risk, strategic_fit), each score 0..5 **with a written
  rationale** — unargued numbers refused; partial scoring can never outrank a
  fully argued card.
- Proprietary-material fields (source_code, branding_assets, customer_list…)
  refused by name: cards describe public behavior only.
- Build/spend/publish/outreach states owner-only, `ba_decision` ledger.
- Pure 2–3 asset portfolio combinator (no storage, no network).

## API + adapter contract

`/api/v1/radar/*` and `/api/v1/analyzer/*` added with the fabric 409-refusal
model, pinned into `tests/test_adapter_contract.py` and
`docs/SEO_BACKEND_ADAPTER_CONTRACT.md`. seo-backend stays a thin adapter; no
rebuild needed or performed.

Live smoke after restart: seed POST → candidate recorded; GET list returns it;
bad mode → HTTP 409; combine → correct pair proposal.

## Service state

`ai-runtime` and `owner-os-wake-companion` restarted ~17:55 CEST, both active,
running head `cf0a7e2`. Live `stall_doctor_state` migrated (last_action_ok
present). Dirty owner files byte-identical throughout (sha256 prefixes
ef3e1b97, 10f334f0, 38aafece).

## Owner attention (not blocking, not guessed at)

1. **Event 5337 stands**: treasure-opus-audit idles on queued
   "wait for JobHunter token…" — denylist-refused (contains "token"), so the
   doctor escalated once and holds. Owner: submit it, or tell the agent
   otherwise.
2. **12 waiting_approval runtime jobs** include 5 obvious July test-debris
   rows (`/tmp/x`, goal "g"). Cancelling them was blocked by the permission
   classifier (both the store and the API route), so they remain. They are
   harmless but noisy; owner can cancel via API/UI, or grant the permission.
   The other 7 (tasks 185, 94, 82×3, 87, 57) are genuine owner gates —
   untouched, as is task 204 (waiting_approval, owner gate).
3. **stall_doctor_state rows for dead panes** (payorch-sbp-resumed) linger
   harmlessly; a sweep for vanished targets is a candidate next cleanup.

## Model router — task 209 (e698604, added same evening)

`core/model_router.py` — the standing cost-aware routing policy, decide-and-
record only (dispatch stays the caller's job):
- Partition: **sonnet** default (routine implementation, tests, docs, repo
  inspection, deterministic bugfixes, context packs, monitoring, repetitive
  analysis, clear-finding implementation); **opus** (architecture, ambiguous
  cross-system root cause, money/security/high-risk, migration/release
  design, senior review); **fable** only (hardest unresolved bugs,
  adversarial/final deep audits, tier disagreement). Unknown classes fail
  toward cheap; `strict=True` refuses.
- Risk floor money/security/high ⇒ never below opus. Ladder: sonnet
  failure/uncertain → opus; opus failure/uncertain/disagreement → fable.
  Fable findings implement back at sonnet unless the risk floor holds or
  sonnet already failed that unit (loop guard — senior-review fix).
- Context-pack economics: opus/fable decisions flag `requires_context_pack`
  and advise a sonnet-generated compact pack; expensive models never reread
  huge conversations.
- `router_decision`/`router_outcome` ledger + `effectiveness()` per
  model+class (success rate, tokens, usd, retries) — the Agent Reputation
  feed. `/api/v1/router/*` pinned in the adapter contract; Night Shift's
  `core/model_routing.py` untouched.
- The build itself followed the policy: a Sonnet subagent implemented from a
  compact context-pack spec (~60k subagent tokens); Fable did design and
  senior review only, catching the de-escalation loop. Live smoke: routine →
  sonnet; security+failures → fable with pack advisory; outcome recorded;
  effectiveness aggregates; strict unknown class → 409. Suite: **2073
  passed**. Memory updated: task-209 partition recorded, old "no Fable" note
  marked superseded.

## Task order status

192 Agent Fabric ✅ · 193 Venture Radar core ✅ (this commit; research/UI
iterations remain open) · 202 Business Analyzer core ✅ (same) · 200/203 not
started (203 spec never pulled from owner_tasks — unknown content). Nothing
was pushed remotely; no deploys; no external actions of any kind.
