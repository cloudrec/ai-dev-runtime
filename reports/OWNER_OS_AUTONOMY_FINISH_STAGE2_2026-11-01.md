# Owner OS autonomy finish — Stage 2 canonical report (2026-11-01)

Instruction source: automated Owner OS API messages from the tech lead. **Not** owner sign-off.

| | |
|---|---|
| Branch | `owneros/autonomy-finish` (worktree `/opt/owneros-autonomy`) |
| Base | `9557004` (= live HEAD of `/root/ai-dev-runtime`) |
| Commits | `9557004..<stage-2 head>` — listed in §1 |
| PROD state | **Untouched.** No deploy, push, merge, restart, live DB/config/grant/route change, no tmux interaction with live panes. Live stays at 9557004. |
| Approval | Stage 1 accepted by tech lead for sandbox only. Nothing here is approved for PROD/merge. |

## 1. Commits

| sha | what | live effect if promoted |
|---|---|---|
| `2fe3756` | Tools, hook and tests derive repo root from `__file__`; tests load code/config from their own checkout. Guard `tests/test_no_live_tree_imports.py`. | None (entrypoints run in place, no symlinks — verified `readlink -f`). |
| `e63e9ac` | Planner JSON extraction: object with `files` wins; tool-call-shaped answer → `planner emitted a tool call instead of a plan`. | Better plan recovery; new failure reason string. |
| `4a83161` | Executor: that planner answer → job `blocked` (`planner_tool_call`), raw answer as artifact, nothing written, one attempt. | Such jobs end `blocked` (actionable event) instead of a no-op fallback + full suite run. |
| `2d5acf2` | `resolve_job_base()` + `config/project_base_refs.yaml` (empty) + `RUNTIME_STRICT_BASE_REF` (default off); log `base branch resolved: <ref> @ <sha> (source: …)`. | Log line text only. Resolution identical until a project entry or the flag is set. |
| `191519c` | `tools/stage_done.py` REPO from `__file__`; guard extended to live-path constants on sys.path. | None (tool runs in place). |
| (this stage) | `tests/test_autonomy_cycle_e2e.py` sandbox E2E (7 tests) + this report. | Tests/docs only. |

Diff: 9557004..HEAD before this stage 37 files, +676/−48 (mostly tests).

## 2. Base-branch selection (tech-lead item 1)

**Issue.** `resolve_base_branch()` for project 28 (`/root/ai-dev-runtime`):
explicit `master` (the API/supervisor default placeholder, `api/v1.py:207`, `core/runtime_supervisor.py:97`)
does not exist → no `origin/HEAD` → checked-out live branch `ai-runtime/220-windows-bridge`
is rejected by `_WORK_BRANCH_RE ^(ai-runtime|repair)/` → `main` 0fd3353 (2026-08-30),
449 commits behind live and **not an ancestor** (8 commits only on main). No error, no log of the SHA.

**Design implemented (`core/git_write.py: resolve_job_base`)**
1. Explicit base (anything except the `master`/empty placeholder) → used if it resolves.
2. Project's authorized `base_ref` in `config/project_base_refs.yaml` (branch, tag or full SHA; SHA recommended = immutable). Unresolvable → **`BaseRefBlocked`** → job `blocked` with `base_ref: …` error (never a fallback).
3. Legacy guess (`resolve_base_branch`, unchanged).
With `RUNTIME_STRICT_BASE_REF=1`: unresolvable explicit base → blocked; a guess that is not an
ancestor of the checked-out HEAD or more than `RUNTIME_BASE_STALE_MAX` (50) commits behind → blocked
with the fix named in the message. Always returns `{ref, sha, source}`; the executor logs all three.

**Backwards compatibility.** Shipped config is `projects: {}` and the flag defaults off → for every
project the chosen ref is byte-identical to before (test `test_without_config_or_flag_the_legacy_guess_is_unchanged`,
`test_the_shipped_config_names_no_project`). `_WORK_BRANCH_RE` untouched (OWNER-111..120 stacking guard).
Only visible change: the log line now carries `@ <sha> (source: …)`; no code parses it (grepped).

**Affected projects.** Any project whose primary checkout sits on an `ai-runtime/`/`repair/` branch
with no `origin/HEAD` — confirmed for project 28. Others (origin/HEAD present) unaffected.

**Needs tech-lead/owner decision to take effect (not done):** add
`/root/ai-dev-runtime: {base_ref: <sha>}` to the config, and/or enable the strict flag in the
runtime unit's environment. Both are live policy changes.

Tests: `tests/test_job_base_ref.py` (12) + 2 executor tests in `tests/test_planner_fallback.py`.

## 3. Planning: "investigate first" tasks (tech-lead item 2)

Root cause (job 703f780d raw answer): with `--tools ""` the model wrote `**Tool: bash**` + JSON args;
the first JSON object was taken as the plan. Now: tool-call JSON is detected and the job is
`blocked` (explicit, actionable, one `action_blocked` event — `blocked` is terminal, no retry loop
exists for it), with no fallback plan, no repo write, exactly one planner call. Genuine one-plan
rule and all other fallback paths unchanged. A real plan following an example object is no longer
shadowed. Tests: 4 in `tests/test_planner_fallback.py`.
Not done (design choice for tech lead): an automatic re-plan with a "JSON only" footer — rejected
for now to keep the one-attempt rule.

## 4. Sandbox E2E (tech-lead item 3) — `tests/test_autonomy_cycle_e2e.py`

Chain exercised with production code: owner spec → engine dispatch to a fake agent on a private
tmux socket → checks → review event → wake decision → **wake_routes** resolution (project route vs
owner-os) → **wake_companion.tick** → **cdp_composer.submit_phrase** (claim, latch, verify user turn +
assistant start/settle, record) → acknowledge → reviewer decision (only after verified delivery) →
next task → project acceptance → GPT `add_tasks` → done.

| test | proves |
|---|---|
| spec_to_done_every_review_reaches_the_project_chat_verified | 3 tasks + project review; every wake to the project chat only; each event delivered once, verified, acked; one prompt per task |
| a_failed_delivery_is_retried_and_the_review_waits_for_it | pre-send failure (browser degraded) ×2 → wake stays pending, reviewer not acting, 3rd attempt delivers; rows `[0,0,1]` |
| a_restarted_companion_never_redelivers_an_acknowledged_wake | fresh companion module: 3 ticks act on nothing; duplicate/contradicting reviewer decisions harmless |
| a_taken_but_unverified_wake_is_never_resent_and_its_watch_is_bounded | latched-unverified: never retyped; SLO scans over 24h → exactly 1 `wake_loop_no_progress` + 1 `wake_loop_stalled`, then silence |
| stage_done_wakes_the_tech_lead_once_and_is_delivered_verified | real `tools/stage_done.main` → `task_completed` → wake → verified delivery; rerun deduped, no re-wake |
| stage_done_refuses_an_agent_without_autonomy | rc 3 `agent_not_permitted_for_autonomy`, no wake |
| a_finished_project_leaves_the_watchdog_quiet | after done: SLO scan 7 days later emits nothing; engine+companion ticks deliver nothing |

**Isolation incident (found and fixed during this stage).** The first full-suite run of the new
E2E failed 6/7: other test files replace `sys.modules["cdp_composer"]` at collection, so the
companion imported an UNPATCHED composer copy, whose `browser_degraded()`/`find_target()` reached
the live browser's CDP port (127.0.0.1:9222). Fix: the fixture pins the patched module
(`monkeypatch.setitem(sys.modules, "cdp_composer", cdp)`) and adds a process-wide wall
(urlopen to `:9222` and websocket connects raise) whose attempts fail the test at teardown
(verified: unpinning makes teardown fail). Live check afterwards (read-only `/json/list`): no tab
on any `sandbox-*` URL. The unpatched path could also navigate a reusable live ChatGPT tab or open
one (`find_chatgpt_page`/`open_chatgpt_page`), so this cannot be fully excluded for that one run;
a live tab now sits on `https://chatgpt.com/c/a` ("New chat") — a URL used only by test fixtures
(test_wake_companion, test_cdp_composer, test_composer_tab_isolation), i.e. a pre-existing suite
leak candidate, not this E2E's URLs. See §9.

**Isolation.** Temp `CONTROL_PLANE_DB`/`AGENT_CONTROL_DB` (+ conftest's runtime DBs), private tmux
socket, `TELEGRAM_*` unset, composer `_http` and `websocket.create_connection` replaced by functions
that raise (so no path can reach the live Chrome on 127.0.0.1:9222), routes bound only in the temp DB.
Runtime: ~7 s.

**Gaps (stated, not hidden).**
- The ChatGPT page is a fake CDP session; real DOM/selector drift is covered only by the live composer canaries.
- The reviewer is code. That ChatGPT actually reads the phrase and calls the MCP review tools is not proven offline.
- The re-wake/escalation events are stubbed at `emit_fn`; their own delivery is the same path as any wake.
- After an escalation for a latched-unverified review, the review waits for a human (by design: owner gate).
- `stage_done` identity in sandbox comes from `OWNEROS_AGENT_TARGET`; tmux-pane identity resolution not exercised.
- `hooks/owneros_hook.py` still reads `/root/ai-dev-runtime/configs/.env` in its handler path (runtime config, not code); not reached by these tests.

## 5. Tests

| run | result |
|---|---|
| Baseline 9557004 (this worktree) | 4211 passed, 4 skipped |
| Job-136 replica (`git archive main`), 4 files | as-is 26–29 failed → with fix 88 passed |
| After 2fe3756 | 4214 passed |
| After e63e9ac | 4217 passed |
| Focused after 4a83161 / 2d5acf2 | 210 / 468 passed (planner, executor, git_write, workspace suites) |
| Stage 2 full suite (all commits + E2E) | **4240 passed, 4 skipped, 0 failed** (489 s) |

## 6. Safety / rollback

- Branch-only commits; `git reset --keep 9557004` (or revert individually). Nothing live to roll back.
- On promotion: companion restart only needed for 2fe3756/191519c (path derivation; behaviour-neutral);
  runtime service restart for e63e9ac/4a83161/2d5acf2. Base-ref policy stays inert until config/flag set.
- Independent review is **required before promotion** and was not performed (subagents forbidden by tech lead).

## 7. Independent review package

Review `git diff 9557004..HEAD -- core tools hooks config` (production files, small):
- `core/ai_planner.py` `_json_objects`/`_extract_json`/`_looks_like_tool_call` — false positives? (`summary` or `files` present ⇒ never tool call)
- `core/job_executor.py` tool-call → `blocked` branch; `BaseRefBlocked` → `blocked` branch; log line.
- `core/git_write.py` `resolve_job_base`, `configured_base_ref` (yaml, realpath key), `_staleness` (ancestry + count).
- `tools/*.py`, `hooks/owneros_hook.py` path derivation (`realpath(__file__)`).
Questions for reviewer: should a configured `base_ref` also be required to be an ancestor of the live HEAD? Should strict mode be the default for project 28 only (via config) rather than a global flag?

## 8. Next exact step

1. Tech lead: review package §7; decide base-ref policy activation (§2) and re-plan option (§3).
2. Me (safe, running now): full suite under a blocking CDP wall plugin to list every test that
   touches the live browser (§9), then a conftest-level wall so the suite can never reach
   127.0.0.1:9222 again.

## 9. Open safety finding — test suite vs live browser

Live Chrome holds a tab on `https://chatgpt.com/c/a` (title "New chat"); that URL appears only in
test fixtures. The full suite has been run on this host many times (incl. 5× today), so some test
path likely reached the live CDP port. Attribution in progress (§8.2). Owner-visible impact: a
ChatGPT tab may have been navigated away from a real conversation; the companion re-opens bound
conversations on demand, but this must be closed at the conftest level.
