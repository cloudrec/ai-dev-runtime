# Owner OS loop: handoff, 2026-11-17

## Current state (canonical; everything below is history)
**Live** (released on the owner's direct word):
- **Runtime** `d052832`, rollback: `deploy/adaptive_offer/rollback.sh /root/ai-dev-runtime/backups/adaptive-offer-20261117T144358Z`.
- **SEO** `97cb001` (MCP send-queue contract + owner-page undismiss button), rollbacks: `deploy/seo_undismiss/rollback.sh`, then `deploy/seo_send_queue/rollback.sh`.
- **Queue**: `SEND_QUEUE_ENABLED=1` via `/etc/systemd/system/ai-runtime.service.d/99-send-queue.conf`. To undo, remove the file and restart ai-runtime.

**On the branch, not live:** the WEST dependency-wait wordings and the changed-task-identity re-arm. See `reports/OWNER_OS_ACCEPTANCE_2026-11-17.md`.

**Open for the owner:**
- refresh or reconnect the ChatGPT connector: its tool list is stale, so `queue_if_busy` is not visible in the chat;
- rotate the MCP token (checklist in the acceptance report, section 7);
- the session login link, which needs a new access path.

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

## Stage 6: send-queue MCP contract and crash window (automated tech-lead instruction, event 87705)
**Runtime:**
| Commit | Change |
|---|---|
| a4a33de | No blind replay. `send_started_ts` is committed before typing. A lost claim is re-queued only if sending never started; otherwise it is settled from agent_control's own record, and with no record it becomes `unknown`, which is never retried automatically. `reconcile()` (delivered, abandon or requeue; attributed; recorded evidence wins) is the explicit exit. |
| 8a3965d | Path-safe keys. loop_watch raises `send_queue_unknown` when a row stays unknown for more than 30 min. |

Guarantee: at most one send happens automatically. Exactly-once is not claimed.

**seo** (branch `owneros/mcp-send-queue-20261117`, from live 6114af8):
- 8ae6df3: `agent_send` gains `queue_if_busy` (default false) and `queue_ttl_secs`. An old payload is forwarded byte-identical. New tools: `agent_send_queue_status`, `agent_send_queue_cancel` and `agent_send_queue_reconcile`.
- ae60962: keys must start with a letter or digit, since `..` in a path segment collapses to another route.
- Tests in the image container: 1049 passed. The failure set is identical to base 6114af8 (68 rows, all caused by no database inside the offline container).

**E2E** (`tools/e2e/send_queue_e2e.sh`): seo MCP handlers over real HTTP to a disposable runtime with synthetic panes. It passed twice. The second run (`reports/loop/e2e_send_queue_2026-11-17/run2_8a3965d.txt`) hit a real kill during a claim: the row recovered after 120 s and was delivered exactly once. The runs cover:
- queue disabled by default;
- an old client unchanged;
- a queued send reporting queued, not delivered;
- a reused key with a different text refused;
- a synthetic payment target refused;
- cancel;
- a restart with a message still queued, delivered exactly once.

**Tests at 8a3965d:**
- forward: 5369 passed, 0 failed, EXIT 0;
- reversed: 5369 passed, 0 failed, EXIT 0;
- crash-boundary mutants: all killed.

**Opus review pass 10:** release-ready after the key-regex fix, which is done.

**Release:** nothing is live. This needs the owner's direct word in this session.
- runtime: `TARGET_SHA=8a3965d deploy/adaptive_offer/install.sh --check` and `--apply`;
- seo: rebuild the image from the merged branch (seo memory: rebuild to deploy);
- turn the queue on with `SEND_QUEUE_ENABLED=1`.

## Stage 7 (automated tech-lead instruction, event 87806)
SHAs:
- **Runtime code:** d052832 (branch head a105f6e: docs only after the code).
- **SEO:** `owneros/mcp-send-queue-20261117` @ **ae60962**. Diff from live: `git -C /opt/seo-send-queue diff 6114af8 ae60962`.

Changes in this stage:
- **d052832, reconcile authority:**
  - every outcome needs the caller's own policy permission on the live target, including the payment hard-deny;
  - requeue needs the same pane (pane id + pid);
  - refusals are audited.
- **SEO tests on disposable PostgreSQL:**
  - Setup: an `--internal` docker network, synthetic data, `init_db` schema.
  - Results: base 48 failed / 1059 passed; candidate 48 failed / 1069 passed. The failure lists are identical and all outside MCP; the 18 offline errors are gone.
  - Focused MCP, auth, ownership and owner-page suites with the full repo mounted: 216 passed. The only skip is DNS.
- **Compatibility and rollback matrix:** `reports/OWNER_OS_RUNTIME_SEO_COMPAT_2026-11-17.md`. Old runtime + new SEO was checked: unknown fields are ignored, so the reply is never a false `queued`.

Test runs at d617a0d (code d052832):
- forward: 5373 passed, 0 failed, EXIT 0;
- reversed: 5373 passed, 0 failed, EXIT 0;
- E2E PASS (third run).

Opus review pass 11: release-ready. Its two wording notes are applied in the matrix.

Installer `--check` at d052832: live 3423f4d → d052832; payorch-ha-fresh, autonomy-pay-sonnet and vtb are classed PAYMENT.

The queue flag is unset on live, so the queue is off.

Release: nothing applied. The owner's direct word in this session is needed for each step: runtime apply, SEO image rebuild, `SEND_QUEUE_ENABLED=1`. After a release, validate with one disposable synthetic project through the real MCP.
