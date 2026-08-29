# Where the full-suite test command actually comes from (2026-08-29)

Read-only analysis over all 82 planned jobs in `runtime_jobs.db`. Prompted by the
first live job after the 16:26Z restart.

## Two corrections to earlier statements in this session

1. I wrote that `ai_planner.default_test_commands()` "returns the whole suite for
   every job". Imprecise — that function only supplies commands when the plan does
   not carry its own, which in practice means **fallback** plans.
2. When job 230 came back with narrow commands, I said that "materially narrows"
   the cap problem. **Also wrong**, in the opposite direction. The scan below
   shows job 230 is the exception, not the rule.

## Measured

| Plans whose `test_commands` run the full suite | 73 of 82 (89%) |
| --- | --- |
| of those, fallback plans | 56 |
| of those, **AI-authored** plans that chose it themselves | 17 |
| plans with scoped commands | 9 |

## Why AI-authored plans choose the full suite

`_build_prompt` (core/ai_planner.py:82) shows the model the required JSON shape,
and the example it hands over is:

```
  "test_commands": ["python3 -m pytest -q"],
```

The planner is being *taught* the full-suite command by the example in its own
prompt. That is the source of the 17, entirely separate from
`default_test_commands()`.

## Consequence for the pending policy decision

Scoping `default_test_commands()` — the fix recorded as "the only non-degrading
option" — addresses the **56 fallback** cases but **not the 17 AI-authored ones**.
Those keep choosing the full suite until the prompt example changes too.

A complete fix is therefore two edits, not one:

1. `default_test_commands()` — derive something scoped rather than the whole suite.
2. `_build_prompt`'s example — stop presenting `python3 -m pytest -q` as the shape
   to imitate.

Both change what "validated" means for a job, so both remain an owner policy
decision. Neither is taken here.

## Live evidence — job 230 (`decb1b3d`, task 230)

First runtime job after the 16:26Z restart. Goal "Read-only arbitrage watchdog
health", kind `deployment`, autonomy `execute_safe`. It self-approved at 21:21:41Z;
nothing in this session approved or triggered it.

* Planner returned in **~40s**, far under the 180s deadline -> **no salvage**.
  `salvaged_after_timeout` is absent, `fallback` absent. Salvage remains
  unexercised in production.
* It chose scoped commands — `py_compile` and `--help` — so it never touched the
  600s cap. One of the 9.
* Terminal state `blocked`, outcome `deployment_prepared`, error
  `"completion gate: DONE refused for risk HIGH_RISK: missing evidence live
  (BUILD != TESTED != DEPLOYED != VERIFIED)"`. That is the truthfulness gate
  working correctly: a deployment was prepared, not verified live, so DONE was
  refused. **Not a defect**, and not related to any staged fix.

## Status of the salvage question

Still unanswered by production. One job, one fast plan. The 180s watcher stays
armed; `feat/salvage-observability` (`80d66d5`) would make the answer direct
rather than inferred, and remains staged pending the deploy gate.
