# Owner OS loop health, 2026-11-17 (step 1, read-only)

Live tree /root/ai-dev-runtime at be3cbd3. Branch owneros/autonomy-loop-20261117. Nothing changed on live.

## Alive
- Units ai-runtime, owner-os-fullcycle-worker, owner-os-fullcycle-intake, owner-os-wake-companion, owner-os-chromium: active since 2026-11-13 06:29 CET, 0 restarts.
- loop-watch: 72 passes in 24 h, all `ok`.
- ChatGPT session: session watch streak 0 (06:53 today); wakes reach the chat (last 03:34–04:14 CET, `submitted_and_assistant_started_generating`).
- Telegram: channel owner_push healthy, last ok 2026-11-17 05:56 UTC. Project-finish notices were sent (e.g. receipts 8029, 8032, 8044).

## Full-cycle projects
- 102 projects, 86 done. The last project started 2026-11-10 13:06 UTC. The owner-os chat has had no wake since 2026-11-10 14:38 UTC, because no project started since then.
- Slots: one fcagent agent is running (fc-gaika-published-risk-test), and one creation entry (gaika-pwa-baskets-slice).

## Open items
1. **Isolation is proven.** 81 projects reached done after isolation went live (2026-11-09 09:46 UTC). Their agents ran as fcagent (uid 1001) on tmux -L owneros-fc, and the dirs are owned by fcagent.
2. **Extra wakes after done.** I found the types:
   - 81592/81603/81615/81729 are `agent_dead` from discovery, raised for an agent the worker stopped on purpose. None have been raised since isolation (last was 81729). Discovery does not see the owneros-fc socket.
   - 81728 is a `task_completed` event with `fullcycle: project_completed`. This one still wakes: 85 times in total. It comes right after the lead's own approve, and the owner already gets the Telegram notice.
3. **Session.** Alive now. The alert exists, but there is no one-button action yet. Not started.
4. **False crash alerts.** None of the 81 projects had a crash alert. The reason is that agent-watch does not watch the owneros-fc socket at all. That also means it cannot see a stuck fc agent (see the bug below).
5. **undismiss** — not started.
6. **blagopay** — not started.

## New findings
- **BUG: reminders are dropped.** In `core/wake_bridge.py` `_fullcycle_review_closed`, a review counts as closed when `fc_review.event_id != event_id`. A reminder is a newer event, so the review looks closed and its wake expires as `fullcycle_review_already_decided`. 11 reminders were dropped this way, and only 2 got through. The check must be "re-announced by a newer event" (`fc_review.event_id > event_id`), not `!=`.
- **Stuck project gaika-published-risk-test since 2026-11-10.** The agent pane shows "API Error: Connection lost mid-response" and the agent sits idle at the prompt. Review 137 (stalled, blocked) is still pending, and its reminders were dropped by the bug above. The agent has held a slot for 6 days.
- **dbus-daemon leak.** There are 214 dbus-daemon processes owned by fcagent, about 2 per agent start, and none are reaped.
- Side note: the fleet-health unit failed because payment servers ru_prod and fi_edge are down. I did not touch them.

# Step 2, 2026-11-17 (branch work)

All of this is on the branch. Live is not changed until the owner says "деплой".

## What changed

| Commit | What |
|---|---|
| aeeee38 | **ChatGPT may send to any agent except payment work.** It gets `agent_send`/`agent_answer` to every agent once `AGENT_AUTOMATION_OPEN_CALLERS=api` is set. A single `payment_marked()` rule closes payorch, blagopay, payment, cloudpayments and any standalone "pay" word. It applies to send, stop, grants, creation and fullcycle repos, checked by session name and by directory. Before this change, only payorch-ha-fresh and /opt/payment-orchestrator were closed. |
| 2dc3888 | **Reminders of a pending review are delivered.** The check was `!=`; it is now "a newer event re-announced it". A regression test is added and was proven to fail without the fix. |
| c2982a0 | Installer `deploy/open_callers/install.sh --check / --apply` and `rollback.sh`. `stage_done` now reads the flag. |
| 136bcf0 | Sandboxed agents run with `DBUS_SESSION_BUS_ADDRESS=unix:path=/dev/null`, so no bus is autolaunched. This could not be reproduced on the stand; check it after deploy by counting new fcagent dbus-daemons. |
| 8de400d (+ seo 2cc6051, branch owneros/undismiss-20261117) | **undismiss.** It is available as a core function, an API route, the CLI command `tools/owner_page.py undismiss <pid>`, and a "Вернуть в список" button on the page. |

## Item status

1. Isolation: proven (step 1).
2. Extra wakes: `agent_dead` is gone under isolation. `project_completed` is kept on purpose: it is the "project handed in, plan the next step" signal.
3. Session expiry: needs an owner decision (see the report).
4. Crash alerts: none were seen, because agent-watch does not see the fc socket. A stall is caught by the engine (a blocked review after 2 h), and now that reminders reach ChatGPT, that path works.
5. undismiss: done.
6. blagopay: closed by name and by directory everywhere. It was never on the allowed-repos list.

## Known red test
`tests/test_stage15_f2_spawn_env.py::test_r1_nothing_this_repo_sets_for_the_cli_is_dropped` fails because of NODE_ENV in `deploy/gaika_services/install.sh` (branch commit 78633db, not from this step). Narrowing the test was refused as weakening a security check. Fix it at the source or decide separately.

# Deploy, 2026-11-17 ~07:32 CET (owner: "деплой, обновляй сам")
- Live went from be3cbd3 to 3423f4d through `deploy/open_callers/install.sh --apply`. `AGENT_AUTOMATION_OPEN_CALLERS=api` is set in configs/.env. ai-runtime, the companion and both fullcycle workers were restarted: all are active with 0 restarts.
- Checked on live with `automation_decision` for the tech lead: serverwarden, owneros-autonomy-loop and gaika-country-hu are open. payorch-ha-fresh, autonomy-pay-sonnet and /opt/blagopay-* are refused as `hard_denied_target`.
- A second block was found: the companion's canary filter `WAKE_BRIDGE_ONLY_AGENTS`. A new drop-in `99-tech-lead-hears.conf` adds `gaika-country-*` and `owneros-*`. Payment agents are still left out. The first wakes from gaika-country-hu (87048) and owneros-fable-audit (87046) were delivered.
- Rollback: `deploy/open_callers/rollback.sh /root/ai-dev-runtime/backups/open-callers-20261117T063150Z`. It also removes the drop-in.
- Not deployed: the seo undismiss button needs an seo image rebuild (branch owneros/undismiss-20261117). The CLI undismiss is live.

# Owner rule, 2026-11-17 ("вариант 1", said by the owner directly in the GAIKA tech-lead session)
The ChatGPT tech lead's word counts as the owner's word for deploying or restarting live Owner OS and GAIKA services. These stay the owner's alone: payment (payorch, payment servers, blagopay), money and paid runs, and permissions. A deploy always goes through an installer with --check and rollback.

# Filter widened to every non-payment agent (owner 2026-11-17 "добавь остальных агентов тоже, кроме платёжных")
- The drop-in `99-tech-lead-hears.conf` now also lists ai-terminal-restore, bitwhite-maxbitwhite, fruit-migration, lootboxes-sonnet, mess-telegram-connector, serverwarden-livefire and vtb. The companion was restarted and is active.
- Delivered after the change: lootboxes-sonnet (87025), fruit-migration (87047) and gaika-country-west (87094) to the owner-os chat, and serverwarden-livefire (87105) to the hostsecure chat. The payment wakes 87120 (autonomy-pay-sonnet) and 87121 (payorch-ha-fresh) were not delivered.
- Limit: the filter is a list of names, so a new non-payment agent session has to be added to the drop-in. The previous version is in backups/open-callers-20261117T063150Z/companion-99-v1.conf.

# Stage 3: identity-based non-payment offering (automated tech-lead instruction, event 87109)
An automated instruction was received from the tech lead through the Owner OS API.

## Receipt (control_plane.db, wake_delivery / wake_audit)
| event | agent | delivered (UTC) | route | ack |
|---|---|---|---|---|
| 87025 | lootboxes-sonnet | 06:58:12 | owner-os | 1 |
| 87047 | fruit-migration | 07:00:29 | owner-os | 1 |
| 87094 | gaika-country-west | 07:04:00 | owner-os | 1 |
| 87109 | owneros-autonomy-loop | 07:05:45 | owner-os | 1 |

## Built (branch only; live is unchanged apart from the vtb deny below)
| Commit | Change |
|---|---|
| 400e23f | `WAKE_BRIDGE_OFFER=nonpayment` offers any agent on authoritative identity (an exact `session:w.p` with a live pane cwd). |
| 8b360ac | Fixes for independent security review findings #1–#10: unknown mode offers nothing; decision-time `offer_identity` stored; coalescing only within the offerable set; a payment event never reaches a non-payment chat (route key or conversation URL); config with no `hard_deny_dirs` fails closed; wider payment words; wider scan; loop_watch uses the same predicate; installer lists commits/files and every pane cwd, and verifies the env. |
| 1a03f47 | NODE_ENV removed at its source (`deploy/gaika_services/install.sh`). The GAIKA daemons import node built-ins only, so this changes nothing at runtime. |
| 3bbb4b4 | `/opt/vtb_old` added to `hard_deny_dirs`. |
| — | Mutants: 8 of 8 killed, plus 1 equivalent (the explicit unknown-mode return is redundant). |

## Live safety fix (restriction only), 2026-11-17 07:34Z
`install.sh --check` printed every pane's cwd, which showed that `vtb` (`/opt/vtb_old`, QR certificate payments via Bakai Bank) is payment work with no payment word in its name or path. Two changes were made on live, both backed up in `backups/vtb-payment-deny-20261117T073405Z`:
- `/opt/vtb_old` was added to the live `config/agent_creation.yaml` `hard_deny_dirs`.
- `vtb` was removed from the companion filter, and the companion was restarted.

Verification:
- `automation_decision(vtb)` now returns `hard_denied_target`.
- The audit for today shows no `agent_send`/`agent_answer` to vtb, only 427 reads.
- No wake from vtb was delivered.

Borderline, left open, owner to judge: bitwhite (crypto exchange backend), mess (messenger with a wallet), lootboxes (prototype storefront). They are products, not payment infrastructure.

## Not done in this stage
- Durable queue for sends to a busy agent. Today a busy agent refuses with "retry", and the message is not kept.
- E2E toy project.

Both are next.

# Stage 4: leak root cause, re-review fixes, #338 (automated tech-lead instructions, events 87109 and 87311)
These are automated instructions received from the tech lead. They are not owner sign-off.

## Order-dependent failures: root cause (c82ee3a)
- `tests/test_owneros_hook.py` ran `hooks/owneros_hook._load_runtime_env()` and `main()` in-process. That loaded the live `/root/ai-dev-runtime/configs/.env` into `os.environ` for the rest of the run, including secrets.
- Once live gained `AGENT_AUTOMATION_OPEN_CALLERS=api`, `autonomy.permitted()` returned true for every agent, so 3 wake-phrase tests flipped.
- Found with a hookwrapper plugin that checks after each test's teardown. The first trigger was `test_owneros_hook::test_the_hook_loads_the_runtime_env...`.
- Fix:
  - `OWNEROS_RUNTIME_ENV` overrides the hook's env path; the production default is unchanged.
  - conftest sets an empty stand-in for the whole suite.
  - The loader tests use their own file and restore `os.environ`.
  - An autouse guard fails any test that leaks `AGENT_AUTOMATION*` or `WAKE_BRIDGE*`.
- Mutant (stand-in removed): killed.

## Reversed-order failures (63b99b7)
- `test_agent_watcher` fixed `NOW` at import time but took task times from the wall clock. Run 16 minutes late, it failed 8 tests. Task times now derive from `NOW`.
- Proven with a plugin that shifts `NOW` by 20 minutes: the original fails exactly those 8 tests; the fixed file passes all 18.
- `test_3_a_root_containing_a_payment_dir_is_refused` was renamed, because pytest truncates its tmp dir name to `..._a_pay0`, which now counts as a payment name. The denied-dir assertion is unchanged.

## Independent Opus security review
| Pass | Result |
|---|---|
| 1 | 10 findings |
| 2 | #1, #3, #4 and #6–#10 closed, #2 and #5 mostly closed; new findings A–D |
| 3 (d87c1e8..c82ee3a) | A–D closed; "release-ready: yes". Low residuals: the `OWNEROS_RUNTIME_ENV` override grants nothing new; the identity is recorded in an agent-controlled process, but the live pane wins at delivery |
| 4 | Covers #338; pending |

## Test runs
| SHA | Order | Result |
|---|---|---|
| c82ee3a | forward | 5301 passed, 0 failed, EXIT 0 |
| c82ee3a | reversed | 8 failed (test_agent_watcher clock), fixed in 63b99b7 |
| 63b99b7 | reversed | 5301 passed, 0 failed, EXIT 0 |
| 0400807 | forward and reversed | pending |

## Backlog #338 (0400807)
- A declared dependency/terminal wait is announced once. While it holds, `agent_watch_state.wait_since` makes later restatements skip with `dependency_wait_parked` (no reminder), and closed_loop resolves the watch as `agent_dependency_wait` (no re-wake, no escalation).
- It re-arms after at least 300 s of real work between stops, or on a different kind of stop. Questions on screen and crashes are never parked.
- Tests: 8 new in `tests/test_agent_watch_dependency_wait.py`, 2 new in `tests/test_closed_loop_wake.py`. Related suites: 536 passed.
- Mutants: 4 of 5 killed; the survivor is equivalent, because emission resets `wait_since` anyway.

## Installer
- `deploy/adaptive_offer/install.sh --check` on f3899b8 is read-only and showed: 13 agents offered; payorch-ha-fresh, autonomy-pay-sonnet and vtb as PAYMENT.
- `rollback.sh` restores the live vtb deny and refuses to finish without it.

## Open
- **Live apply:** not done. Owner authorization is to be verified directly; an API instruction alone is not enough.
- **Stray file:** an untracked file named `"  "` (two spaces) remains in the worktree. It is an agent_control SQLite file created by the 08:57 suite run, probably by the `AGENT_CONTROL_DB="  "` case in `test_agent_privilege.py`. A safety check blocked its removal, so the owner should delete it. Which code opens the DB at a blank path is still to be traced.
- **Backlog:** the busy-send durable queue and the E2E toy project.

# Stage 4 final, fca9ea9
## #338 after reviews 4–6
The fourth review (not release-ready) found 7 issues; the fifth found 2 more (A/B); the sixth found 1 more. The design is now:
- **Which stops can park:** only the `quiescent` class, and only on explicit dependency/new-task phrases in the agent's final message (never on error lines).
- **What still alerts:** blockers keep their hourly reminder; completed stops, questions and crashes are always announced, and each of them re-arms.
- **TTL:** 4 h, honoured by closed_loop on `quiescent` and `idle` panes. A frozen pane is never renewed. Once the TTL lapses, re-wake and escalation return.
- **No `native_external_wait`:** stall_doctor still sees a new task left unsubmitted in the composer.
- **Which watches are resolved:** only stop watches (`task_completed`, `work_stopped_incomplete`, `agent_turn_stopped`). Failure and question watches raised by hooks keep waking.
- **Other agents:** a pane that leaves the inventory loses its wait. Payment rules are untouched.

Evidence:
- Mutants on the new branches: all killed except the lapsed-digest guard, which is equivalent because it is unreachable after an `idle` reclassification and is kept as a defence.
- Review 6 found only the event-type gate (fixed in fca9ea9). The fixed state was not re-reviewed.

## Tests at fca9ea9 (verify worktree)
| Order | Result |
|---|---|
| forward | 5323 passed, 0 failed, EXIT 0 (14:22) |
| reversed | 5323 passed, 0 failed, EXIT 0 (17:57) |

## Installer `--check` at fca9ea9
- Read-only, live 3423f4d → fca9ea9.
- 17 commits; the files are config, policy, agent_watch, closed_loop, loop_watch, wake_bridge, deploy, tests and the report.
- Panes: payorch-ha-fresh, autonomy-pay-sonnet and vtb show PAYMENT. Two new test sessions appeared, `sec45ea83-ws` and `sec7068eb-long`, the second with a cwd containing `$(touch …)`. The check prints that cwd from Python and never evaluates it.
- Rollback: `deploy/adaptive_offer/rollback.sh <backup>` restores the drop-in, `configs/.env`, the old SHA and the live vtb deny. It refuses to finish without the vtb deny.

## Live
Not applied. Live is still 3423f4d, with the vtb deny edit and the widened companion list. The owner's direct word is needed; the tech-lead API instruction was explicitly not counted as approval.

## Release 2026-11-17 (owner's direct word: "деплой d052832, seo и очередь")
- Runtime: `TARGET_SHA=d052832 deploy/adaptive_offer/install.sh --check` rc=0, `--apply` rc=0. Live d052832. Rollback: `deploy/adaptive_offer/rollback.sh /root/ai-dev-runtime/backups/adaptive-offer-20261117T144358Z`.
- SEO: `deploy/seo_send_queue/install.sh --check` then `--apply`; deployed branch fast-forwarded 6114af8 -> ae60962, backend image rebuilt, only the backend container recreated, healthy. Rollback: `deploy/seo_send_queue/rollback.sh` (image `seo-backend:rollback-pre-send-queue`).
- Queue: `/etc/systemd/system/ai-runtime.service.d/99-send-queue.conf` (`SEND_QUEUE_ENABLED=1`), ai-runtime restarted and healthy. Undo: remove the file, daemon-reload, restart ai-runtime.
- Live check through the real seo MCP handlers against the live runtime, with a disposable non-payment tmux pane `sq-probe` (/var/tmp/sq-probe, removed afterwards):
  - unknown target: refused, nothing created;
  - busy target: `queued:true, delivered:false`; delivered once when the pane went idle (attempts 1);
  - the same key again: `duplicate:true`, the text appears once in the pane;
  - `loop_watch.check`: no problems.
- No payment target was called; payment exclusion stays proven by the tests and by `--check`.

## Release 2 (owner: "передай техлиду и делай")
- seo ae60962 -> 97cb001 (owner page "Вернуть в список", cherry-pick of 2cc6051), 52 focused tests passed; `deploy/seo_undismiss/install.sh --check`/`--apply` rc=0, backend healthy, MCP queue contract still present. Rollback: `deploy/seo_undismiss/rollback.sh`.
