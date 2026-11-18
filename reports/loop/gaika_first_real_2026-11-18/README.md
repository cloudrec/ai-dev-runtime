# First real GAIKA Full Cycle task (2026-11-18)

- **Repo source.** New allowed repo `gaika-live` → `/opt/gaika`, the live checkout at 1181e47d. It was added on the owner's direct word "разреши гайку с живой версии" at 11:21Z.
  - A project copy takes only the committed HEAD. The live directory and var/ are not touched.
  - Rollback: restore `/root/ai-dev-runtime/backups/fullcycle-repos-20261118T112107Z.json` to `/etc/ai-runtime/fullcycle-repos.json`, or run `repos.remove('gaika-live')`.
  - The old `gaika` entry (`/opt/gaika-extension`, main ed100521) is 2188 commits behind live. Projects should not use it.
- **Defect, reproduced in a scratch copy of 1181e47d.**
  - `tests/project-doc-counts.test.mjs` throws `ENOTDIR` on a plain file in var/catalog.
  - The live var/catalog holds `refresh-queue.json.post-reconcile-*` files.
  - Without var/, the file passes: 19 pass, 3 skip.
- **For the tech lead.** Start project `gaika-catalog-walk` with `fullcycle_project_start(project_id="gaika-catalog-walk", repo="gaika-live", spec=<spec.json>)`, then review it through MCP. One agent, no deploy.
