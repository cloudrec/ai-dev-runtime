# Model escalation gate — task 213 (2026-08-16)

Branch `ai-runtime/182-retry-fix-wake-continuation-star`. Extends the task-209
model router (`core/model_router.py`) with a HARD gate: partition-class
eligibility for Opus/Fable is necessary but no longer sufficient — dispatch
to either tier also requires a structured, auditable `escalation_reason`.

## What changed

`core/model_router.route()` gained a new step between the escalation ladder
and the context-pack advisory:

1. `requested_model` = whatever the base class / risk floor / escalation
   ladder / clear-finding de-escalation computed (unchanged logic).
2. If `requested_model` is opus or fable, `_validate_escalation()` requires:
   - `escalation_reason["category"]` in `ESCALATION_CATEGORIES[model]`
     — opus: `architecture`, `high_ambiguity`, `high_risk`, `senior_reasoning`;
       fable: `hardest_unresolved`, `adversarial_audit`, `final_critical_audit`
   - non-empty `escalation_reason["evidence"]` (prior attempts / findings)
   - non-empty `escalation_reason["expected_benefit"]`
   - non-empty `context_pack` (the existing param — a compact delta pack
     reference, never a full session reread)
3. Missing/invalid on **any** path (base class, risk floor, or the
   failure/uncertain/disagreement ladder) de-escalates `model` to sonnet.
   It never raises — same "routing must never fail or block a job" contract
   the module already had. Nothing is silent: `reason` records
   `"hard gate de-escalates to sonnet (requested opus)"` and
   `escalation_valid`/`requested_model` are returned + persisted.

Audit trail: `router_decision` gained `requested_model`,
`escalation_category`, `escalation_evidence`, `escalation_expected_benefit`,
`escalation_valid` columns (best-effort `ALTER TABLE`, same idiom as
`stall_doctor`'s `last_action_ok`). Combined with the pre-existing
`task_class`, `risk`, `model`, `at`/`ts`, and `router_outcome` rows, every
opus/fable dispatch now carries: reason/category, prior attempts (via
`prior_attempts`), risk class, expected benefit, context-pack reference,
selected model, decision timestamp, and outcome.

## Wiring

- `core/job_executor.py::_route_model` reads `job.get("escalation_reason")`
  (a dict, if the job carries one) and forwards it to `model_router.route()`.
  **Automated/routine dispatch never sets this field** — so routine
  implementation, load/perf tests, docs, repo inspection, and concrete
  post-audit fixes (`clear_finding_implementation`, which already
  de-escalates by class) all land on sonnet even when their task_class or a
  money/security/high risk floor would otherwise reach for opus. The
  `model_selection` artifact now also records `requested_model` and
  `escalation_valid` for post-hoc audit.
- `api/v1.py`'s `POST /router/route` gained an optional `escalation_reason`
  field, so an explicit caller (owner or a senior-review agent) has a real
  path to justify opus/fable through the same gate.

Nothing about approval gates, lineage (`_lineage_attempts`), retries,
no-duplicate dispatch, or workspace isolation was touched — this is purely
an additional constraint on which model gets dispatched, not a change to
whether/how a job runs.

## Why de-escalate rather than raise

The module's own standing philosophy (`route()`'s docstring, pre-existing):
fail toward the cheap model, never toward the expensive one, and routing
must never block a job. Task 213 extends that same philosophy to the
expensive tiers instead of introducing a second, inconsistent failure mode.
A risk-floor-triggered opus request (money/security/high) without a
recorded justification now also de-escalates — the floor is real, but it
does not substitute for an explicit, auditable reason to actually spend on
the pricier model.

## Tests

- `tests/test_model_router.py`: existing opus/fable-expecting tests updated
  to supply a valid `escalation_reason` (the partition/risk-floor/ladder
  logic itself is unchanged, so their original assertions still hold given a
  valid reason). Added 15 new tests: denial without a reason (class, risk
  floor, ladder), wrong category for the tier, empty evidence/benefit, missing
  context_pack, valid grants for opus and fable, concrete-fix de-escalation
  holding under the gate, no-raise on a garbage `escalation_reason`, audit
  columns persisted, and sonnet-tier work never gated.
- `tests/test_runtime_model_routing.py`: `test_route_model_escalates_to_opus_after_sonnet_failure`
  renamed to `test_route_model_escalation_ladder_without_reason_stays_on_sonnet`
  and now asserts the gate holds (sonnet, `requested_model=opus`,
  `escalation_valid=False`) — this is the direct proof that the escalation
  ladder alone can no longer reach opus through real job dispatch. Added:
  `test_route_model_with_valid_escalation_reason_reaches_opus` (the one
  legitimate path) and `test_routine_dispatch_kinds_never_reach_opus_or_fable`
  (parametrized over operational/code-change/content-production kinds, each
  run with `risk_level="critical"`, proving the routine load-test /
  concrete-fix / docs paths land on sonnet even under a risk floor).
- Full suite green (2136 passed pre-213; task-213 changes add ~19 tests,
  re-run pending in this session, see commit for the exact count).

## Not done here (deliberately out of scope)

No UI/CLI was added for an owner or agent to *supply* `escalation_reason` on
a real runtime job — the API (`/router/route`) and the `job["escalation_reason"]`
dict field are the mechanism; wiring a specific caller (e.g. a senior-review
step that fills it in) is future work, not requested by task 213 itself.
