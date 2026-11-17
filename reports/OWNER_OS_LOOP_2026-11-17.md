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
