# Agent Supervisor + Permission Resolver — Evidence (2026-07-20)

Owner-approved. Eliminates routine permission-prompt stalls for owner-approved
agents by auto-confirming **only** provably-safe local read-only prompts, while
leaving everything unclear or consequential for the owner. Scoped to `seo-audit`
only. **No product files changed, no new agents, no sessions stopped/restarted.
Polyinput, Safe Guard, Security, email, JobHunter, Mess, Justice untouched.**

- **ai-dev-runtime commit:** `/root/ai-dev-runtime` · `main` · `7c841ca`
- **Service:** `ai-runtime.service` (existing always-on daemon) — supervisor runs as a startup task, independent of any MCP/ChatGPT client.

---

## The safety boundary (deny-by-default, fail-closed)

`core/permission_resolver.py` classifies a shell command as safe **only if** every
pipeline segment's leading program is on the read-only allowlist and passes that
program's sub-rules:

- **Allowed (read-only):** file reads/list/find/grep/`sed -n`/head/tail/cat/wc/stat/diff/jq; `git status/diff/log/show/branch/rev-parse/ls-files/remote`; process/service **status** (`ps`, `systemctl status/is-active`, `journalctl`); `docker ps/inspect/logs/images/version` and `docker compose ps/config/logs`; read-only SQL (`SELECT`/`count`/`SHOW`/`\dt`/`EXPLAIN`); test runners (`pytest`, `python -m pytest`, `npm test`, `go test`, `cargo test`) and dry-runs.
- **Always unsafe → stays `waiting_owner`:** any write/redirection (`>`/`>>`), command/process substitution (`$(…)`, backticks, `<(…)`), variable expansion (`$VAR`), background (`&`), `sudo`/`eval`/`exec`/`xargs`/subshell wrappers; secret-looking paths (`.env`, `*.pem`, `id_rsa`, `credentials`, `token`…); SQL writes/DDL; `git push/reset/commit/checkout/clean`; `docker run/exec/restart/build/compose up/down/restart`; `systemctl start/stop/restart`; `pip/npm install`, `npm publish`; `curl`/`wget`/`ssh`/`nc`; `alembic upgrade`, `manage.py migrate`; `sed -i`, `find -exec/-delete`, `awk system()`, `tee`, `dd`, `chmod`, `kill`; unknown programs.

Unknown/ambiguous ⇒ unsafe. It never executes anything; it classifies.

---

## Live canary (deployed code, on the host)

**Safe vs dangerous — through the deployed classifier:**
```
SAFE:      cmd='docker compose ps'              safe=True   category=read_only:docker
                                                 → eligible for auto-confirm
DANGEROUS: cmd='docker compose restart backend' safe=False  category=denied
                                                 reason='docker compose write subcommand'
                                                 → LEFT for owner (never auto-confirmed)
```
Both extracted from a real Claude Code permission dialog by `extract_pending_command`.

**Supervisor armed, scoped to seo-audit only:**
```
service.log: "agent supervisor started (interval 45s, sessions=['seo-audit'])"
env (configs/.env): AGENT_SUPERVISOR_ENABLED=1  AGENT_AUTORESOLVE_SESSIONS=seo-audit
```

**Dry-run sweep reads real state (no keystroke):**
```
POST /api/v1/agents/supervise  → {"ok":true,"sessions":["seo-audit"],"resolved":[],"approved":0}
```
At canary time seo-audit was `working` then `idle` (no pending permission prompt),
so there was correctly nothing to resolve. A bounded watcher is armed to capture
the next real prompt (approve → `waiting_owner`→`working` + latency); the
end-to-end approve/resume/latency path is proven by the tests below and will be
appended here when a live prompt occurs.

---

## Counterexample — a risky request is not auto-confirmed

`test_dangerous_prompt_stays_waiting_and_is_not_approved`: the supervisor reads a
`docker compose restart backend` prompt, classifies it unsafe, records
`left_for_owner`, and **never** calls `approve_prompt` (asserted `approve==0`).
Plus 60+ dangerous-command classifier denials (rm, docker restart/exec/up,
systemctl restart, git push/reset, pip/npm install, curl, ssh, sudo, cat .env,
psql DELETE/DROP, alembic upgrade, redirections, `$(…)`, xargs, sed -i,
find -exec, awk system(), `; rm -rf /`).

---

## How it behaves

- Poll every 45s over allowlisted sessions; on `waiting_owner`, read the exact
  prompt, extract + classify the command.
- Safe + allowlisted ⇒ send the single approval keystroke `1` ("Yes", this once —
  **never** `2`/"don't ask again"), then verify resume within 8s and record
  latency.
- Unsafe, ambiguous, unextractable, or non-allowlisted ⇒ leave `waiting_owner`.
- Each `(target, prompt-hash)` decision persists in sqlite (`supervisor_prompts`),
  so a prompt is never re-processed or re-alerted after a restart, and a safe
  prompt is confirmed at most once per appearance.
- Confirmed-but-not-resumed is recorded (`approved_no_resume`) and the agent stays
  `waiting_owner`, so the existing Owner OS `agent.waiting_owner` notifier raises
  the single deduplicated Telegram owner alert (one per transition, persisted).

**The owner keeps manual override:** the supervisor only sends `1` to a
provably-safe allowlisted prompt; the owner can answer any prompt directly at any
time, and non-allowlisted sessions are never touched.

---

## Tests

ai-dev-runtime full suite: **415 passed**. New:
- `test_permission_resolver.py` — **99**: exact SEO read-only prompts safe; dangerous counterexamples unsafe; construct-level rejection; dialog extraction.
- `test_agent_supervisor.py` — **8**: safe+allowlisted→approved+resume+latency+persisted; dangerous→left, never approved; safe-but-not-allowlisted→left; dry-run→no keystroke; unextractable→left; approved-no-resume flagged; restart-safe persistence.

---

## Files changed (ai-dev-runtime only)

```
core/permission_resolver.py   NEW — command classifier + dialog extraction
core/agent_supervisor.py      NEW — always-on supervision loop + resolve_target
core/agent_control.py         approve_prompt + supervisor_prompts store
api/v1.py                     POST /agents/resolve (dry-run default), /agents/supervise
api/main.py                   supervisor startup task
tests/test_permission_resolver.py, tests/test_agent_supervisor.py
.gitignore                    ignore runtime agent_control.db
configs/.env                  AGENT_SUPERVISOR_ENABLED=1, AGENT_AUTORESOLVE_SESSIONS=seo-audit
```

No Owner OS/product code changed for this feature; the Telegram alert path reuses
the already-deployed `agent.waiting_owner` notifier.

## duplicate_created / sessions

`duplicate_created=false` — 0 tmux sessions created, 0 stopped, 0 restarted. The
supervisor's only side effect is a single `1` keystroke to an allowlisted,
provably-safe prompt (none sent during the canary window, since seo-audit had no
pending prompt).

## Rollback

- Disable instantly: set `AGENT_SUPERVISOR_ENABLED=0` (or empty `AGENT_AUTORESOLVE_SESSIONS`) in `configs/.env`; `systemctl restart ai-runtime.service`.
- Revert code: `git revert 7c841ca`; `systemctl restart ai-runtime.service`.
- `supervisor_prompts` / `pane_tail_cache` are additive sqlite tables (safe to drop).
