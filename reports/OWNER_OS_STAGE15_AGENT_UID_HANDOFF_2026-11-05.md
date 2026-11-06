# Stage 15 agents under their own uid, 2026-11-05

Option A from `reports/OWNER_OS_STAGE15_SANDBOX_DEFAULTS_2026-11-04.md`, on the owner's word. Branch `owneros/stage15-agent-uid` off `e8f4c16`, HEAD `de5ee45` plus this report. Not merged, not deployed. Off unless `AGENT_RUN_AS` is set.
**H1: NOT PROVEN CLOSED. The last audit did not finish. PROD: NO-GO.**

## What it does
With `AGENT_RUN_AS=<user>` the runtime stays root.
- Runs as the agent: its pane, session recovery, the full-cycle engine's git, checks and file access, job steps, handoff and checkpoint files. Rebuilt environment, no groups, no capabilities, no_new_privs.
- Refused outright: job queue, deliver, release, validator, `/run`. An instance under the setting works through the full-cycle engine only. The owner should know this: those tools are off for that instance.
- API mode is refused under the setting.
- A pane is never typed into, or reused, unless both its process and its claude run as the target. This covers permission approval and key sends.
- The first dropped agent pins the setting at `<AGENT_CONTROL_DB>.run_as`. A process of that instance without the variable obeys it; a differing variable or an unsafe pin refuses. Per instance, so a runtime with another DB is unaffected.
- A problem with the setting refuses. It never falls back to root.

Commits: `2e47968` feature, `9b256de` first audit, `c4fbb08` second audit, `4524f89` setpriv path, `de5ee45` import alias. New: `core/agent_privilege.py`, `tools/agent_uid_preflight.py`, `tests/test_agent_privilege.py`.

## Audits (Fable, self-reported `claude-fable-5-1`, billing not checked)
1. `2e47968`: H1 not closed. Root git and pytest in agent trees, no pane uid check, FIFO hang reproduced. Fixed in `9b256de`.
2. `9b256de`: H1 not closed. Tree check broken three ways, root writes through a planted link, key sends unchecked. Fixed in `c4fbb08`. The per-tree check was removed in favour of the flat refusal.
3. `c4fbb08`: **stopped by Fable's safeguards (`[cyber]`, request `req_011CgikczuZ8sbJw9PouXFsv`) before the report was finished.** One finding arrived, CRITICAL, confirmed by repro: root resolved `setpriv` through the agent's PATH. Fixed in `4524f89`. Its verdict named two more root-read paths, in `project_supervisor` and `work_evidence`, with no detail. **Those were not fixed or looked at, except `work_evidence._read`, which `c4fbb08` already hardened.** The rest of that report is lost. It said H1 not closed and `c4fbb08` not fit for a sandbox run, and that with these fixed the design would be workable.

No audit has seen `4524f89` or `de5ee45`.

## Evidence
See the full suite line in `reports/stage15/full_suite_de5ee45.txt`.
- `tests/test_agent_privilege.py`: 72 passed. Real setpriv and a private tmux server, as `nobody`. These skip on a runner without root.
- Mutants on disposable copies: about 50 run, 2 survivors, both in `deliver`, where the `git_write` guard also refuses. The fixes after the survivor pass added their own tests; the `setpriv` mutant was caught.
- Real `claude` 2.1.318 in a dropped pane on a private tmux, at `2e47968`: uid 65534, sentinels absent, first-run screen shown. No login, no request made.

## Not proven
- A real agent run under the setting. Nothing here ran a logged-in agent or a full-cycle task end to end as a dropped uid.
- The agent's own subscription login stays in its home: it can read and use it. Use a dedicated low-value account.
- Under the setting several root status readers return empty for the agent's project, because root git refuses a repo it does not own.
- `hooks/owneros_hook.py` reads the runtime env file, so under the setting hook events go dark. Do not make that file readable.

## Owner gates
1. Whether to retry the audit, or decide without it. A narrower prompt may also be stopped. Defense Access in the Cyber Verification Program reduces these stops: https://support.claude.com/en/articles/14604842-real-time-cyber-safeguards-on-claude
2. Create the user, install `claude` for it and log in, with an account chosen for this. Then `AGENT_RUN_AS=<user> AGENT_CONTROL_DB=<instance db> tools/agent_uid_preflight.py --deny <mcp env> --project <dir> --pin`. `AGENT_RUN_AS` without `AGENT_CONTROL_DB` refuses as `control_db_unset`. `--pin` writes `<db>.run_as`; remove it in cleanup.
3. Gates 2, 4 and 5 of the E2E requirements report still stand.
