# Stage 15 live sandbox E2E with a real rejection, 2026-11-06

**Result: project `sandbox-price` reached done with no human on any link after the operator started it. ChatGPT rejected the agent's first attempt from the code in the review, the agent reworked it, and the project finished, in about 5 minutes. The agent ran under its own uid. PROD: NO-GO. Nothing deployed.**

Code: `47408dd` in a sandbox worktree. MCP: unmerged seo `5253d7c`. Real ChatGPT, real Claude on the owner's subscription under user `fc-agent` (uid 1001), own DBs, private tmux, browser on CDP 9223. Full suite at this code: 4869 passed, 4 skipped, 0 failed.

## The test
The operator told ChatGPT two code rules in the chat: every new `.py` file starts with `# owner: pricing-team`, and money uses `decimal.Decimal`, never float. The agent was not told. The spec's checks do not test either rule.

| review | task | attempt | decision | ack to decide |
|---|---|---|---|---|
| 1 | total | 1 | **reject** | 1.9 s |
| 2 | total | 2 | approve | 7.6 s |
| 3 | fmt | 1 | approve | 1.4 s |

- Attempt 1 used `float()` and had no header, and passed the automatic checks. The reject names `def total` on the first line and `float(price)`, `float(tax_rate)`. The agent's summary did not mention float, so ChatGPT took this from the diff.
- The engine sent the feedback back. Attempt 2 has the header and `Decimal`. Review 2's diff has no `float(`.
- Task fmt came out right on its first attempt, in a task after the feedback.
- Commits `0efdb9c`, `18f9682`. Rejected work was never committed.
- Audit: 6 rows, all `api:client/sandbox-gpt`, `client_token`.
- Outside the engine, as the agent: `total([10,20],'0.1')` gives `Decimal('33.00')`, `fmt` of a total gives `0.30 RUB`.
- Wakes for events 1 to 3 were real turns (`submitted_and_assistant_started_generating`); event 4 was `assistant_still_generating`.

## Under its own uid
- `tools/agent_uid_preflight.py --pin`: all checks passed, first run on this host. Agent pane and `claude` ran as uid 1001.
- As the agent: reading the sandbox dir and root's credentials file: permission denied.
- Shared token on ack and decision with a forged actor header: 403 both.
- Fullcycle engine worked under the setting: dispatch, verify, git, rework, commit.

## Fixed on the way
`47408dd`: the review payload had no code. The reviewer saw the agent's report, file names and check results. So earlier approvals, including the 2026-11-06 zero-touch run, judged no code. Without this fix the test above could not pass.

## Not proven
- Hard tasks: this was a small task with two simple, mechanical rules.
- Cheating by the agent: the uid separation was probed, no adversarial agent was run.
- Recovery after a crash or a lost pane under the uid, and context rotation.
- ChatGPT's tool dialog: the connector was already on "Allow all" from the earlier run, so a new connector can still ask.
- The agent used the owner's own subscription account.
- A live deploy. PROD MCP still lacks the unmerged seo branch, so reviews cannot be decided there. PROD also lacks a reviewer client token.

## Setup work by the operator
Operator actions before the start: sign-in, connector token rebind in the sandbox MCP, code rules message, first-run screens of the agent. One first-run error: Enter on the trust screen chose "No, exit"; the pane was restarted. None of it happened after the worker started.

## Cleanup, done 2026-11-06
Removed: sandbox API, worker, companion, tmux, `sandbox-mcp`, nginx site, browser units and profile, user `fc-agent` and home after `claude auth logout`, `/opt/owneros-sandbox-run`, worktree. Live services active, 10 live tmux sessions, ports free.
Left: Cloudflare record `mcp.clients.help` and connector `sandbox-owneros`, kept on the owner's word. The ChatGPT session of the removed browser stays valid until the owner logs out all devices.

Evidence: `reports/stage15/e2e_2026-11-06_reject/`.
