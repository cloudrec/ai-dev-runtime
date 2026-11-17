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
