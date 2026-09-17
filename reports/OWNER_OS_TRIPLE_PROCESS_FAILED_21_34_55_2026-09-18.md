# Owner OS — "agent.commander.agent_process_failed ×3 at 2026-09-17T21:34:55.818590Z"

**Provenance:** an automated instruction was received (Owner OS API channel, not
owner-typed) asking whether the three identical-timestamp warnings on the live
notifications surface are three events, a rendering duplication, or a dedupe defect.
Nothing here is owner sign-off.

## Verdict

**Legitimately distinct events; the identical timestamp is client-side.** Not an Owner OS
dedupe or accounting defect. The consumer hides the discriminators the API already carries.

Owner OS did have one accounting gap on the drain path (the ack left no record of what
was consumed), fixed narrowly below.

## What the store holds

`agent_control.db` → `commander_events`, six rows, all `event_type=agent_process_failed`,
all `source=direct_agent_lifecycle`, all `classification=interruption` (`to_state=vanished`):

| id | ts (UTC) | agent | from_state | dedup_key |
|---|---|---|---|---|
| 7235 | 21:34:12.426 | diamond-auction:0.0 | shell_running | dlc:interrupted:diamond-auction:0.0:33f64cc6… |
| 7236 | 21:34:12.501 | email:0.0 | waiting_input | dlc:interrupted:email:0.0:c7b3419d… |
| 7237 | 21:34:12.591 | gaika-sonnet-v11-clean:0.0 | waiting_input | dlc:interrupted:gaika-sonnet-v11-clean:0.0:610b5393… |
| 7238 | 21:34:12.830 | main105-backup-repair:0.0 | waiting_owner | dlc:interrupted:main105-backup-repair:0.0:f2c240aa… |
| 7239 | 21:34:14.760 | security-demo-fresh:0.0 | waiting_input | dlc:interrupted:security-demo-fresh:0.0:455ef80a… |
| 7240 | 21:34:14.795 | server-disk-cleanup-opus:0.0 | waiting_input | dlc:interrupted:server-disk-cleanup-opus:0.0:250f2208… |

Six agents, six conversation ids, six timestamps, six dedup keys. Dedup worked exactly as
designed: nothing collapsed, nothing doubled. `service.log` at 23:34:14 CEST confirms the
emitter: `direct lifecycle: emitted=6 ['agent_process_failed', …×6]`.

Cross-check against the control-plane `event` table: no event of any kind carries ts
`21:34:5x`. The nearest are 54503–54505 (`agent_watch`, 21:35:33, three different targets:
diamond-auction, main105-backup-repair, gaika-ru-sonnet-v1) — also distinct, also not the
timestamp in question.

**`2026-09-17T21:34:55.818590+00:00` appears nowhere in Owner OS**: not in
`control_plane.db`, `agent_control.db`, the audit jsonl, or the service log. Owner OS
never produced that timestamp.

## Where the timestamp comes from

The consumer at 172.20.0.2 (outside this repo) polls
`GET /api/v1/agents` → `GET /api/v1/agents/orchestrator` → `POST /api/v1/agents/commander/events/ack`.
The `agent_list` audit places that poll at 21:34:55.196Z; the ack immediately follows.
`orchestrator.status()` returns each event with its own `ts` (21:34:12/14) and only one
response-level `checked_at`. The surface shows all items at 21:34:55.818590 — the client's
own poll/ack instant — and labels them `agent.commander.<event_type>` with severity
`warning`; neither that label nor that severity exists in this repo (the store says
`critical`, `owner_action_required=true`). Three shown vs six stored is likewise a
consumer choice (window, limit, or grouping) that Owner OS cannot see: the ack endpoint
recorded nothing about which ids it flipped.

## The discriminator the surface hides

Every item the API returns carries `id`, `ts`, `agent` (tmux target), `project` (cwd),
`dedup_key`, and `payload.conversation_id` / `from_state`. Rendering only
`<label> @ <poll time>` discards all of them, which is why six different dead agents read
as one warning repeated. The fix on the consumer side is to render `agent` and the event
`ts` — that is not this repo.

## Owner OS-side fix (narrow, tested, committed locally)

`core/agent_control.py::ack_commander_events` now:
- audits `commander_events_acked` with the ids sent and the `(id, agent, event_type, ts)`
  of every row that actually flipped 0→1, so the next "N identical warnings" question is
  answerable from `/var/log/ai-runtime/agent_control.jsonl` alone;
- returns the number of rows that flipped (was `conn.total_changes`, which also counts
  anything a schema migration wrote on the same connection).

Full suite after these changes: 3347 passed (2026-09-18).

Regression test: `tests/test_agent_control.py::test_ack_commander_events_audits_what_was_consumed`
(three distinct `agent_process_failed` events → one ack → audit names all three; re-ack
flips 0 and says so; unknown ids count 0).

Incidental, same commit: `tests/conftest.py` now clears `CONTINUATION_WATCHDOG_SESSIONS`
and `NATIVE_SUPERVISOR_{TARGETS,DENY_PROJECTS,DENY_TARGETS}` for the suite. The operator
shell this agent runs in carries the live values (`NATIVE_SUPERVISOR_TARGETS=*` since the
2026-09-04 autonomy activation), which made four controls fail before any change here —
`test_health_flags_zero_eligible_as_misconfiguration` saw two eligible sessions, and three
`test_native_supervisor` deny-list controls saw every denied target supervised. All four
verified failing on a stashed tree and passing with the variables unset.

A fifth control of the same shape, `test_continuation_verifier.py::test_the_repo_ships_with_no_canary_selected`,
asserted on `os.environ` and so judged the host: the git-ignored `configs/.env` has named
`NATIVE_CANARY_TARGET` since 2026-09-11 (set during a predecessor's tenure; not touched
here). It now asks `git grep` whether any TRACKED file arms the canary (none does) and that
the module's default with the variable unset is empty. Verified: passes on the clean tree,
fails when a tracked file is temporarily armed, tracked file restored.

## Not done / not touched

- No restart of ai-runtime (the live process is pid 6557, started 22:51:22 CEST 2026-09-17
  by something other than this agent — the owner-typed `перезапусти ai-runtime` of
  2026-09-04 was therefore overtaken; the ack audit above is NOT live until the next
  restart, which remains an owner gate).
- No push, no routes, no credentials, no BotFather, no agent creation.
- Untracked `reports/` preserved; only the three files above are staged.
