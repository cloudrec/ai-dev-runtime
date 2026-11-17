# Owner OS practical acceptance, 2026-11-17

Trigger: an automated instruction (Owner OS API, tech lead) was received. It is not owner approval. Nothing live was changed for this stage: the work is on a branch, and the live system was only read.

## 1. Live release, verified read-only
Evidence: `reports/loop/acceptance_2026-11-17/live_readonly.txt`.

- **SEO** is at `97cb001` on the deployed branch. `seo-backend-1` is healthy.
  - 97cb001 is the undismiss button cherry-picked onto `ae60962`.
  - The release ran 52 focused tests: owner release page plus MCP agent tools.
- **Queue contract on SEO is intact.** `agent_send` advertises `queue_if_busy` and `queue_ttl_secs`. The tools `agent_send_queue_status`, `agent_send_queue_cancel` and `agent_send_queue_reconcile` are listed.
- **Runtime** is at `d052832`, health 200. `SEND_QUEUE_ENABLED=1` is set by drop-in.
- **Live queue probe, run earlier today:**
  - a busy disposable non-payment pane got `queued:true`, and the message was delivered once when the pane went idle;
  - a repeated key returned `duplicate:true`.

## 2. Findings from the real HU/Balt and WEST stops
The live events, read-only:
- **WEST 87144**: re-woken (87176 `no_progress`) and escalated (87219 `stalled`). It was the same terminal wait as 87094.
- **WEST 87495**: re-woken and escalated again (87524, 87555).
- **HU 87893**: re-woken and escalated (87896, 87904). It said "Next: wait for owner captures", a wording #338 already knows. That chain ran at 14:01-14:40Z, before d052832 went live at 14:44Z.

Defects found:
| # | Defect | Evidence |
|---|---|---|
| A | WEST's wordings "only genuine owner captures remain, so I'm stopping" (87094) and "Standing by until captures arrive" (87495) were not recognised as dependency waits, so #338 would still re-wake and escalate WEST every SLO window | test `test_without_the_new_wordings_the_same_stop_would_have_been_rewoken` |
| B | Changed task identity: when a tech-lead task is finished between two sweeps, the pane is never observed `working`. It then classifies `idle` (no event), or, if it re-states its wait, stays parked. Either way the result of the new task never reaches the chat | the tests below failed before the fix |

Cooldown, the last 7 days live: 110 events were skipped `actionable_cooldown_active`. All 110 were woken later by `_redecide_cooldown_skips`. 57 of them were also retired as superseded by a newer event of the same agent. None was lost. No defect.

## 3. Fix (branch `owneros/autonomy-loop-20261117`)
Source diff: `reports/loop/acceptance_2026-11-17/source.diff` (2 files, +59 −4).

- **`core/agent_watch.py`**
  - `_DEP_WAIT_RE` gains the two WEST wordings. They are narrow and explicit, and an error line still never counts.
  - New `note_task_delivered(target)`. For that session's panes only (`LIKE` is escaped, so `_` is literal), it:
    - clears the declared wait;
    - clears the notification;
    - records the pane as `working`.
  - Effect: the agent's next stop is news and is classified `quiescent` even when the turn was shorter than one sweep.
- **`core/agent_control.py`**
  - `agent_send` calls `_note_task_delivered` after delivery, but only when:
    - the message was really delivered (`delivered is True`, so a duplicate does not count);
    - the caller class is `api`, meaning the tech lead or owner, directly or from the send queue.
  - Internal nudges (watchdogs, orchestrator) never count, so the #338 suppression of re-wakes stands.
  - A failure in the watch store never fails the send.

## 4. Tests
New file `tests/test_dependency_wait_real_cases.py`, 19 tests on synthetic panes and DBs:
- **Phrases**: the real WEST and HU wordings, and near misses: the HU handoff that needs coordination, an owner gate, "only 3 captures remain…", an error line.
- **Announced once, then parked**, for all three wordings over 30 sweeps.
- **Repeated watchdog**: four SLO windows after WEST 87094 bring no re-wake and no escalation. A control test without the new wordings is re-woken, as happened live.
- **New evidence**: a different stop, long new work, and the TTL each re-announce.
- **Changed task identity**:
  - a short tech-lead task is announced even if the pane was never seen working;
  - a watchdog nudge alone stays parked;
  - a plain stop answered within one sweep is announced;
  - only the target session is touched (`gaika-country-hu` does not reach `gaika-country-hu_x`);
  - only a delivered API send re-arms;
  - a store failure never fails the send.
- **Cooldown**:
  - HU and WEST in the same second are both delivered;
  - a new completion of the same agent inside the cooldown is delivered;
  - a burst ends on the newest completion, and each older one is delivered or retired with an audit row.

Mutation check, on a scratch copy and never near the live tree. Every mutant is killed:
- WEST phrases removed: 2 failed;
- no `working` mark: 3 failed;
- unescaped `LIKE`: 1 failed;
- any caller re-arms: 1 failed.

Focused suites (new file, #338, closed loop, wake bridge, send queue): 287 passed. Log: `reports/loop/acceptance_2026-11-17/focused_tests.txt`. Full suite: see the handoff.

## 5. Why the chat's `agent_send` schema lacks `queue_if_busy`
The live server does advertise it (section 1). The chat is working from a cached tool list:
- The server announces `tools.listChanged`. It sends `notifications/tools/list_changed` only on the connector's server stream (GET).
- Since the backend was recreated at 15:01Z, ChatGPT has made 5 POST calls, 0 GET stream connections and 0 `tools/list` requests.
- So ChatGPT never received the notification and never re-listed. It keeps the catalog it fetched when the connector was last connected or refreshed.

Supported remedy: refresh the connector in ChatGPT, or reconnect it. ChatGPT then calls `tools/list` and the new fields appear. This is an action in ChatGPT's own settings UI. It is not done by automation and needs no workaround. Rotating the MCP token (section 7) changes the connector URL, so it does the same refresh as a side effect.

## 6. Expired ChatGPT session UX (investigation only)
- **Today:**
  - `core/chatgpt_session_watch.py` detects `logged_out`/`browser_down` and alerts Telegram once, then every 6 h, then on recovery.
  - The wake browser runs under Xvfb. noVNC is bound to `127.0.0.1:6080`, so it cannot be reached from the phone.
  - Logging in again needs a person at that browser.
- **Proposal for a user-visible login link and status:**
  1. Add a session status line to the owner page: ok / logged out since HH:MM / browser down. Read-only, from the existing state file. No credential and no token is shown.
  2. Make the Telegram alert carry a link to a noVNC view of the wake browser. The owner logs in there by hand, as on any website.
- Step 2 needs noVNC reachable from the phone, for example tailnet-only (`tailscale serve`) with a VNC password. That is a new access path, so it is an owner decision (permissions). Not implemented.
- Out of scope by design: no automated authentication, no credential or cookie capture, no session or token access.

## 7. MCP token rotation, owner-only checklist (no values)
Reason: the connector URL embeds the token. It appeared in agent output twice today, the second time when a log redaction pattern did not match the `/api/mcp/c/` path. The backend access log also stores that URL.

1. **Mint a new token** in the SEO admin MCP token page, with the same label and scopes as the current ChatGPT connector token. The raw value is shown once: copy it straight into step 2 and nowhere else.
2. **Update ChatGPT**: in its settings, open the Owner OS connector and replace the URL with the new one. Saving reconnects it and re-lists the tools; this also fixes section 5.
3. **Verify the new connector**: ask the tech lead chat to call `agent_list`. The token list then shows `last_used_at` moving on the new token.
4. **Revoke the old token** by its id in the same admin page. It shows `enabled=false`.
5. **Recommended follow-up (code, the tech lead's call)**: redact `/api/mcp/c/<token>` in the backend access log, so a rotated token is not written to logs again.

No agent performs any of these steps. Nothing here reveals or reads a token value.

## 8. Remaining blockers and next safe steps
| Item | Owner of the decision | Next step |
|---|---|---|
| Release this fix (runtime, `agent_watch` + `agent_control`) | tech lead (variant 1: non-payment Owner OS) | `TARGET_SHA=<sha> deploy/adaptive_offer/install.sh --check` then `--apply`, after the full suite |
| Chat tool list is stale | owner, in the ChatGPT UI | Refresh or reconnect the connector, or rotate the token (section 7), which does both |
| Session login link | owner (new access path) | Choose: status line only, or status line plus tailnet-only noVNC link |
| MCP token rotation | owner | Section 7 |
| Access-log redaction of the connector path | tech lead | Small SEO change, with tests |
