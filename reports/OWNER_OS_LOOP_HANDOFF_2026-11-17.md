# Owner OS loop: handoff, 2026-11-17

**Branch:** `owneros/autonomy-loop-20261117`.

**Live:** `/root/ai-dev-runtime` runs 3423f4d, plus two edits:
- a live edit to `config/agent_creation.yaml` (the vtb deny);
- the companion drop-in `99-tech-lead-hears.conf`, which lists the non-payment agents.

The full log is in `reports/OWNER_OS_LOOP_2026-11-17.md`.

## Live since this morning (owner's word "деплой, обновляй сам")
- The tech lead (caller `api`) may send to every non-payment agent.
- Reminders for pending reviews are delivered again.
- Sandboxed agents run with no session dbus.
- Undismiss is available from the CLI.
- vtb deny: `/opt/vtb_old` holds QR payments via Bakai Bank. Applied at 07:34Z as a restriction only.

## On the branch, not live
| Item | SHA | Evidence |
|---|---|---|
| Identity-based non-payment wake offering, replacing the hand-kept list | 400e23f..f3899b8 | 3 Opus passes, "release-ready" |
| Leak root cause: tests loaded the live configs/.env | c82ee3a | plugin bisect; mutant killed |
| Reversed-order clock bug in test_agent_watcher | 63b99b7 | 8 failures reproduced, then fixed |
| #338: a declared dependency wait is announced once | 0400807..fca9ea9 | 4 Opus passes; the seventh says "release-ready" |
| AGENT_CONTROL_DB: blank or relative values refused | 2b82624, b69f206 | guard test against raw readers |
| stage_done: identity found by process ancestry; `no_agent_identity` | 1f42580 | tests |
| Durable send queue for busy agents | a32805c, b69f206 | 21 tests, mutants killed; Opus pass 8 found 1/2/5, fixed; pass 9 pending |
| Isolated toy E2E, 4 scenarios, all PROJECT DONE | 692cca0 | `reports/loop/e2e_2026-11-17/*.txt` |

**Release** (needs the owner's direct word):
- Apply: `TARGET_SHA=<final> deploy/adaptive_offer/install.sh --check`, then `--apply`.
- Rollback: `deploy/adaptive_offer/rollback.sh <backup>`. It restores the live vtb deny and refuses to finish without it.
- The send queue stays off until `SEND_QUEUE_ENABLED=1`.
- The MCP `agent_send` tool in seo needs a `queue_if_busy` param. That is a separate seo change, not done.

## Open
- **Owner decisions:**
  - session-expiry "one button" (needs remote browser exposure);
  - Bitwhite, MESS and Lootboxes stay in non-financial DEV scope only.
- **Stray SQLite file** named `"  "` in the worktree. A safety check blocked its removal, so the owner should remove it. Its contents were never read. Root cause: a blank AGENT_CONTROL_DB, now refused.
- **seo undismiss button:** branch `owneros/undismiss-20261117` (2cc6051) needs an image rebuild.
- **Known limit:** the send queue can re-type a message if the process crashes after typing but before agent_control records the delivery.

## Final evidence, b69f206
| Check | Result |
|---|---|
| Full suite, forward | 5355 passed, 0 failed, EXIT 0 |
| Full suite, reversed | 5355 passed, 0 failed, EXIT 0 (fresh worktree) |
| Opus review pass 7 (#338 final delta) | release-ready |
| Opus review pass 9 (send queue fixes) | release-ready; one optional low item: compare text_sha on a duplicate |
| Installer `--check` | live 3423f4d → b69f206 |

`--check` pane classification:
- PAYMENT, never offered: payorch-ha-fresh, autonomy-pay-sonnet, vtb.
- Offered: every other pane.

An earlier reversed run was invalid and is not counted: I removed its worktree while the run was still going.
