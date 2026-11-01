# Owner OS autonomy finish — Stage 3 canonical report & successor handoff (2026-11-01)

Direction came as automated Owner OS API messages from the ChatGPT tech lead. **None of it is
owner sign-off.** Previous stages: `reports/OWNER_OS_AUTONOMY_FINISH_STAGE1_2026-11-01.md`,
`reports/OWNER_OS_AUTONOMY_FINISH_STAGE2_2026-11-01.md` (read §2, §4, §9 there for detail).

## 0. State in one screen

| | |
|---|---|
| Worktree / branch | `/opt/owneros-autonomy` / `owneros/autonomy-finish` |
| Base → head | `9557004` (= live) → see `git log 9557004..HEAD` (9 commits incl. this report) |
| PROD | **Untouched.** Live `/root/ai-dev-runtime` still at 9557004. No deploy/push/merge/restart; no live config, flag, grant, route, DB, tmux or browser change. |
| Approved for PROD | **Nothing.** Independent review not yet done (subagents forbidden). |
| Manual cleanup for owner | Live Chrome tab on `https://chatgpt.com/c/a` ("New chat", CDP id 20C8121A…) was opened by the test suite (Stage 2 §9). Left untouched by direction. Close it by hand. |
| Do NOT | run the full test suite from the LIVE tree until f27d538 is there — it opens a real tab each run. Runtime jobs run the suite too. |

## 1. Commits (9557004..HEAD)

| sha | kind | summary | live effect on promotion |
|---|---|---|---|
| 2fe3756 | fix (tools+tests) | repo root from `__file__`, no live-tree imports; guard test | none (in-place entrypoints, no symlinks) |
| e63e9ac | fix | planner: `files` object wins; tool-call reply named | new failure reason; better plan pick |
| 4a83161 | fix | tool-call reply → job `blocked`, no fallback | such jobs block instead of no-op fallback |
| 2d5acf2 | feat | `resolve_job_base` + empty `config/project_base_refs.yaml` + `RUNTIME_STRICT_BASE_REF` | log line only (config empty, flag off) |
| 191519c | fix | `stage_done.py` REPO from `__file__` | none |
| aacad1b | test | sandbox E2E (7) | tests only |
| f27d538 | fix (tests) | conftest walls 127.0.0.1:9222; composer fixture | tests only — stops suite opening live tabs |
| ccbcb25 | fix | two different plans in one reply refused (fallback path, never completed) | plan pick stricter |
| ff93b10 | feat | ≤1 bounded replan after tool-call reply, `RUNTIME_PLANNER_REPLAN` | none (flag off) |
| d813e48 | tool+docs | `tools/base_ref_report.py` (read-only), this report, evidence, diffs | none |

## 2. Tech-lead decisions applied

1. **Base ref** — design kept opt-in; nothing set live. Migration/validation/rollback in §3.
2. **Replan** — implemented (ff93b10), default **off**. Bounds: one extra provider call max; `planner_replan`
   artifact written *before* the call (a resumed/re-executed job never replans again); result must pass the
   planner's typed validation (`files` list, allowed ops, relative non-`..` paths, non-empty for code kinds);
   second tool call / prose / empty files / any planner error → `blocked`, never fallback, never a 3rd call.
   Tests: 5 in `tests/test_planner_fallback.py`.
3. **Test-only commits** — not fast-tracked. Stray tab untouched (manual cleanup above).
4. **Stage 3 evidence** — §4, §5.

## 3. Base ref: migration, per-project validation, rollback

**Evidence** (`tools/base_ref_report.py`, read-only: rev-parse/rev-list/merge-base, `GIT_OPTIONAL_LOCKS=0`, no fetch),
`reports/stage3/base_ref_report_2026-11-01.jsonl`, for every project that ever had a runtime job:

| project | checked-out HEAD | legacy guess | behind | strict verdict |
|---|---|---|---|---|
| `/root/ai-dev-runtime` | `ai-runtime/220-windows-bridge` @9557004 | `main` @0fd3353 | 449 | **block** — not an ancestor |
| `/opt/seo` | `feat/social-stage4-telegram-wordpress-20260720` @7039d6c | `master` @00480f5 | 154 | **block** — >50 behind |
| `/opt/e2e-proj` | `ai-runtime/75-…` @9d87633 | `master` @edb61c9 | 1 | ok |

So the stale-base problem is **two** projects, not one. Enabling strict mode globally before both
have a `base_ref` would block every job for both.

**Migration (each step a reviewed change; owner/tech-lead gate at 3 and 5):**
1. Promote 2d5acf2 (+ the rest after review). Behaviour unchanged; check the new log line
   `base branch resolved: <ref> @ <sha> (source: guess)` appears on the next job.
2. For each project run `tools/base_ref_report.py <path>`; choose the deployed SHA (full 40 hex).
   Validate: `git -C <path> cat-file -e <sha>^{commit}` and `git -C <path> merge-base --is-ancestor <sha> HEAD`.
3. Add `<abs path>: {base_ref: <sha>}` to `config/project_base_refs.yaml` in a reviewed commit. Pin a SHA,
   not a branch (immutable; a branch moves silently).
4. Validate with one low-risk job per project (autonomy `prepare`): log must say `source: config` and the SHA.
5. Only after every active project shows `source: config` or `strict_verdict: ok`, consider
   `RUNTIME_STRICT_BASE_REF=1` in the runtime unit environment.
6. Re-pin after each deploy of that project (the pin is the deployed line; it does not follow).
**Rollback:** remove the entry (config commit revert) and/or unset the flag → resolution is the legacy guess again;
no data migration, no schema change. Code rollback: `git reset --keep 9557004` on the promoted tree + service restart.
**Failure mode:** a configured SHA that disappears (gc after force-push) → job `blocked` with
`base_ref: configured base_ref … does not resolve` — visible, never a silent fallback.

## 4. Sandbox E2E — what is real and what is simulated

Reproduce (from the worktree; never from the live tree):
```
cd /opt/owneros-autonomy
python3 -m pytest -q -p no:cacheprovider tests/test_autonomy_cycle_e2e.py      # 7 tests, ~7 s
python3 -m pytest -v -p no:cacheprovider tests/test_autonomy_cycle_e2e.py tests/test_fullcycle_e2e.py \
  tests/test_stop_wake_closed_loop.py tests/test_fullcycle_settled.py tests/test_wake_delivery_verification.py \
  tests/test_job_base_ref.py tests/test_no_live_tree_imports.py tests/test_planner_fallback.py
```
Recorded: `reports/stage3/evidence_focused.txt` (140 passed, 170 s).

| step | in sandbox | real code? |
|---|---|---|
| owner spec → project/spec rows | `specs.create_project/put_spec`, temp DB | real |
| dispatch → agent | engine + private-socket tmux pane + **fake agent** script | real engine/tmux; agent simulated |
| checks, task contracts, git commit | real, in a temp fixture repo | real |
| review/stage_done event → wake decision | `cto.emit` / engine → `wake_bridge` | real |
| route resolution | `wake_routes.resolve` (project route vs owner-os), temp DB bindings | real |
| companion tick | `tools/wake_companion.tick` | real |
| delivery + verification | `tools/cdp_composer.submit_phrase` (claim, latch, user-turn + assistant-start/settle proof, ledger) against a **fake CDP page** | real logic; **page simulated** |
| ChatGPT reads the wake, calls review tools | **reviewer stub** answering only verified-delivered reviews via `fe.submit_decision/ack_review` | **simulated** |
| decision → next task (continuation) | engine applies decision, dispatches next task | real |
| failure/retry, restart, latched-unverified, duplicate wake/decision | dedicated tests | real logic |
| SLO watchdog (re-wake/escalation) | `closed_loop_wake.slo_scan` with `emit_fn` stub | real decision logic; emission stubbed |
| `stage_done` doorbell | `tools/stage_done.main` in-process, identity via `OWNEROS_AGENT_TARGET` | real (pane identity path not exercised) |

**Not proven offline (needs a live canary, owner-approved):** real ChatGPT DOM/selectors, that the
tech-lead model reads the phrase and calls the MCP review tools, real Telegram, real pane identity.
Live evidence that does exist: this very session's `stage_done` calls (events 78155, 78167, 78237)
got `wake` decisions on route owner-os, and the tech lead replied — i.e. the real delivery leg worked
end-to-end for the stage_done path, on live 9557004 code.

Isolation guarantees (sandbox): temp control-plane/agent-control/runtime DBs (conftest), private tmux
socket, `TELEGRAM_*` unset, composer pinned to the patched module, process-wide and suite-wide walls on
127.0.0.1:9222 that fail the test on any attempt.

## 5. Independent review package

Files: `reports/stage3/diff_production_9557004..ccbcb25.patch` (core/tools/hooks/config; includes tools/base_ref_report.py),
`reports/stage3/diff_tests_9557004..ccbcb25.patch` (tests). The callouts below are the **author's own** — they do not replace an independent review.

**Highest blast radius first**
1. `hooks/owneros_hook.py:39` — runs in EVERY Claude session on the host. One-line change
   (`sys.path` root from `realpath(__file__)`). Safe only while the hook is invoked from
   `/root/ai-dev-runtime/hooks/` (settings.json) with no symlink. Verify after promotion with one hook run.
2. `tools/wake_companion.py`, `tools/cdp_composer.py` (3 lazy sites) — live delivery path. Same derivation;
   systemd ExecStart is the in-place path. Restart of the companion needed on promotion.
3. `core/job_executor.py` — (a) tool-call reply → `blocked` (behaviour change for every runtime job that would
   have run a no-op fallback); (b) `BaseRefBlocked` → `blocked`; (c) bounded replan (flag off).
   Check: no other caller depends on such jobs reaching `fallback_plan_only`/`failed`.
4. `core/ai_planner.py` `_extract_json` — the object carrying `files` wins over earlier non-plan objects;
   **two different plan objects are refused** (`planner returned several different plans` → normal
   planner-failure path → deterministic fallback → `fallback_plan_only`, never completed). Identical
   repeats (fence + prose scan) count once. `_looks_like_tool_call` false-positive risk: only objects with
   no `files`/`summary` and a tool-arg key.
5. `core/git_write.py` — `configured_base_ref` loads YAML (`safe_load`) from the repo's own config: the
   authorization boundary is code review of that file. Key matched by `realpath` and raw path.
   `_staleness` shells out to `git merge-base --is-ancestor` (read-only). `BaseRefBlocked` subclasses
   `GitWriteError`, so existing `except GitWriteError` handlers still catch it.
6. Secrets: no new secret handling. Replan stores the sanitized (redacted, capped) raw planner reply in
   artifacts, like the existing fallback path.
7. Tests' conftest monkeypatches `socket.socket.connect` process-wide for port 9222 only — test-only.

**Questions for the reviewer**: should a configured `base_ref` also have to be
an ancestor of the live HEAD; per-project strict mode instead of a global flag.

## 6. Tests

| run | result |
|---|---|
| Baseline 9557004 (worktree) | 4211 passed, 4 skipped |
| f27d538 full suite with live-CDP wall | 4240 passed, 4 skipped, 0 failed |
| ff93b10 full suite | 4245 passed, 4 skipped, 0 failed (496 s) |
| Focused evidence set (8 files) | 140 passed |
| Planner/executor suites after ff93b10 | 217 passed |

## 7. Successor handoff (clean session)

1. `cd /opt/owneros-autonomy && git status && git log --oneline 9557004..HEAD`
2. Read this file, then Stage 2 §2/§4/§9 only if needed. Do not reread old conversations.
3. Never run the suite from `/root/ai-dev-runtime`. Never touch live config, flags, grants, routes, tmux, browser.
4. Report each stage via `/root/ai-dev-runtime/venv/bin/python /root/ai-dev-runtime/tools/stage_done.py --summary … --ref … --tests … --report … --next …`.
5. Open items, in order: (a) independent review of §5 — needs a reviewer other than the author;
   (b) owner-approved promotion plan (installer + backup + rollback, companion + runtime restarts);
   (c) post-promotion validation (§3 step 1, hook run, one wake delivered); (d) base_ref pins for
   `/root/ai-dev-runtime` and `/opt/seo` (§3).

## 8. Final suite at this head

ccbcb25 (planner ambiguity fix on top of d813e48): **4248 passed, 4 skipped, 0 failed** (596 s), worktree, live-CDP wall active.
