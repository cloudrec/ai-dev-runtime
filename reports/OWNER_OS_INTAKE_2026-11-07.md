# Projects from ChatGPT, 2026-11-07

On the owner's word ("делай уже до конца чтобы работало"). Branch `owneros/stage15-agent-uid` @ `a044d4d` (code `68895a0`), seo `feat/mcp-fullcycle-client-token` @ `0aa58b2`. Not deployed: owner runs `deploy/intake/install.sh`.

## What it does
The GPT tech lead calls MCP `fullcycle_project_start(project_id, spec)`, which is reviewer-only like decisions. Owner OS then:
1. Creates `/opt/fc-projects/<id>` with a fresh git repo and stores the spec.
2. Starts agent `fc-<id>` through the creation policy. The new scope allows root `/opt/fc-projects`, caller `fullcycle:engine` only, at most 2 agents.
3. Answers claude's folder-trust prompt for that folder only, then waits until the agent is idle for 15 s.
4. Runs the tasks and sends each one for review.
5. Stops the agent when the project is done.

If the agent is replaced mid-attempt, the engine resends the task.

## Limits
- **Checks:** checks from such a spec are untrusted: a test runner on project files only. They are refused at start with the reason.
- **Trust bug fixed:** project acceptance checks always ran trusted before. Now they are trusted only if a non-reviewer wrote those exact checks.
- **Scope:** no push, no deploy. Intake is off under `AGENT_RUN_AS`. Agents run as root, as all live agents do today.

## Evidence
- Full suite at `68895a0`: 4972 passed, 4 skipped, 0 failed. Focused tests: 81 in `tests/test_fullcycle_intake.py`. seo: 145 passed in the image.
- Mutants on a scratch copy: 19 of 19 killed after two tests were added.
- **Stand, real claude** (private DBs and tmux, `/opt/fc-e2e-test`, scripted reviewer): project `wordstat`, 2 tasks.
  - The agent started by itself and the task was resent after the agent was replaced.
  - Both tasks were approved and committed. The project was done and the agent stopped.
  - About 95 s after the fixes. The agent's 8 tests pass. Evidence in `reports/intake/e2e_2026-11-07/`.
- **Found on the stand and fixed:**
  - The trust prompt with "No, exit" selected made the agent quit.
  - A task sent to a dead agent hung.
  - A first Enter 1 s after start was dropped.

## Not proven
- Real ChatGPT calling the new tool.
- The tool showing up in the connector without a refresh.
- A live deploy.
