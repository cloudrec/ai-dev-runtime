# Runbook — Restore protected project agent contexts

Scope: continuity and observability only. This procedure makes **no product
changes**. It does not start development, deploy, send messages, alter production
data, or change configuration in `acap`, `mess`, `email`, or `JobHunter`.

## What the tool does

`core/agent_context_recovery.py` performs a read-only scan of every project in
`config/agent_context_registry.yaml` and reports, per project:

- whether `PROJECT_STATE.md` and `HANDOFF.md` exist and are non-empty,
- which conversation references are on disk (`CONVERSATION*.md`, `conversations/*`,
  `.claude/**/*.jsonl`, `*.session.json`),
- whether the expected tmux session is visible (`tmux list-sessions`, read-only),
- the current git HEAD (if the project is a git repo),
- the exact blockers preventing a clean context restore.

Protected projects (`acap`, `mess`) additionally get a **draft** reconstructed
context file. Drafts are written to `reports/runtime/context_recovery/drafts/`
inside this repository and are never copied into a project root — every unknown
field is marked `UNKNOWN` rather than guessed.

## Run it

```bash
cd /root/ai-dev-runtime
python3 -m core.agent_context_recovery                 # scan + write report + drafts
python3 -m core.agent_context_recovery --no-write      # scan and print only
python3 -m core.agent_context_recovery --json          # machine-readable output
```

Outputs:

- `reports/runtime/context_recovery/AGENT_CONTEXT_RECOVERY_<YYYY-MM-DD>.md`
- `reports/runtime/context_recovery/AGENT_CONTEXT_RECOVERY_<YYYY-MM-DD>.json`
- `reports/runtime/context_recovery/drafts/<project>_PROJECT_STATE.draft.md`

Exit code `1` means at least one protected project still needs reconstruction
(attention required); `0` means both protected contexts are intact. A non-zero
exit is a status signal, not a tool crash.

## Status vocabulary

| Status | Meaning |
| --- | --- |
| `ok` | Both context files present and non-empty, plus at least one conversation reference |
| `degraded` | Some durable context present, some missing |
| `missing` | Root exists but both context files are gone |
| `absent` | Project root is not present or not readable |

## Restoring a protected context (human step)

1. Run the scan and read the report's Blockers section.
2. Check the backups in `.ai-runtime-backups/` and the project's own git history
   (`git -C <root> log --diff-filter=D -- PROJECT_STATE.md HANDOFF.md`) for the
   last known-good copy. Recovering from history is always preferred over the draft.
3. Only if no history exists, use the draft as a skeleton, fill in the `UNKNOWN`
   fields from conversation references, and have the owner confirm before any file
   is placed in the project root.
4. Re-run the scan to confirm the project reports `ok`.

Restoring tmux visibility (`tmux new-session -d -s <name> -c <root>`) is a manual,
explicit step. The tool only observes sessions; it never creates or kills them.
