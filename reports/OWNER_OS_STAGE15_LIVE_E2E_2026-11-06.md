# Stage 15 live sandbox E2E, 2026-11-06

**Result: second run `sandbox-calc2` ran the full loop with no human on any link. First run `sandbox-calc` needed one human dialog, and is where the 403 and replay checks were made; they were not repeated in the second run. PROD: NO-GO. Nothing deployed.**

Scope: sandbox only. Agent ran under root (`AGENT_RUN_AS` unset), so this says nothing about whether an agent can cheat, and audit 4's H1 stays open. Sandbox API at `de5ee45`; seo MCP from unmerged `5253d7c`. Sandbox processes used the live venv interpreter, with sandbox data; no live file was changed.

## Checklist (`OWNER_OS_STAGE15_E2E_REQUIREMENTS_2026-11-04.md`, section B)
| # | item | result |
|---|---|---|
| 1 | sandbox API, own DBs, tmux, repo, routes | yes, 127.0.0.1:18080 |
| 2 | wake route on dedicated chat | yes, route `sandbox-calc` |
| 3 | dedicated profile and CDP port | yes, 9223 |
| 4 | real agent, reviews appear | yes, claude on subscription; reviews 2, 3, 4; spec had 3 tasks, not 1 |
| 5 | wake delivered as a real turn | yes, events 8, 17, 19: `submitted_and_assistant_started_generating` |
| 6 | list, ack, decision via MCP; audit | yes, 6 rows, all `api:client/sandbox-gpt`, `client_token` |
| 7 | shared token 403 | yes, on ack and decision |
| 8 | engine applies, project ends, replay refused | yes, see below |
| - | no human on any link | first run NO, second run **yes**: see below |

## Evidence
- Project `sandbox-calc`: add, mul, cli all `VERIFIED_DONE`, 1 attempt each. `fc_project_done` event 19, both final checks rc 0.
- Commits in throwaway repo: `2aef277`, `56183df`, `7f1ea7e`.
- `decision_applied` for reviews 2, 3, 4 by the engine; `project_completed` 2026-11-06 04:26 UTC.
- Replay of spent key, 3 calls: same body, other decision, other review. Each returned `duplicate: true` and the original decision `approve`; the third `ok: false`. Review 4 unchanged, review 1 stayed pending, no new audit row. HTTP 200, not an error.

## The human gate
ChatGPT stopped on review 4 with its own dialog "Allow ChatGPT to use sandbox-owneros?". The decision came about 13.5 hours after the ack (audit seq 5 to 6). This session did not click it. So someone did, or the tool call was retried after an allow; who and which option is not known. Reviews 2 and 3 went through in seconds; why they had no visible dialog is not verified.

## Findings
1. ChatGPT tool confirmation is a human gate in the review path. Connector-side, not fixable in Owner OS.
2. Wake queue blocks head-of-line on an unbound route (`no_route_bound`). Event 6 stopped delivery until acknowledged by hand in the sandbox.
3. Companion without `websocket` reports `renderer_unresponsive` / `no_chatgpt_page_open` instead of the cause.
4. Replay answers carry no error status and leave no audit row, so spent-key attempts are not visible in `fc_decision_audit`.
5. Spec token `{python}` is substituted only by the demo harness. The first project failed on it, honestly, as `blocked`; that was an operator error here, but real specs can hit it.

## Second run, zero touch
Project `sandbox-calc2`, same chat and agent, tasks sub, div, cli2, about 3 minutes. Operator started it; after that no click or message.
- Reviews 5, 6, 7 approved; audit seq 7 to 12, all `api:client/sandbox-gpt`, `client_token`; event 29 project done, first-try on all tasks.
- Ack to decide took 1.1 to 1.4 s, leaving no room for a human click. A poll every 10 s saw no dialog.
- Why no dialog appeared is inferred, not proven: the likely cause is an "Always allow" chosen in the first run. A new connector or account should expect the dialog again until allowed once.
- Commits `1298a4c`, `de74779`, `7a82017`. Checked outside the engine: `sub 7 2` prints 5, `add 2 3` prints 5, `div(8,2)` 4.0, `div(1,0)` raises ValueError.
- Evidence: `reports/stage15/e2e_2026-11-06/`.

## Not proven
- Cheating by the agent: not tested, agent under root.
- Failure paths with a real agent and reviewer (reject, rework, recovery).
- Hard tasks: both specs were trivial and a reviewer that approves everything would have looked the same here.
- ChatGPT reached the MCP through a temporary public host; PROD MCP still lacks the unmerged seo branch.

## Cleanup, done 2026-11-06
Removed: nginx site, `sandbox-mcp`, sandbox API, worker, companion, tmux session, browser units and profile, `/opt/owneros-sandbox-run`, worktree `/opt/owneros-sandbox`. Live services active, 10 live tmux sessions unchanged, seo branch untouched.
Left for the owner: Cloudflare record `mcp.clients.help`, connector in ChatGPT account, ChatGPT password change.
