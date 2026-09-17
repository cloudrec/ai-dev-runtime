# Owner OS — restart readiness note (2026-09-18)

**Provenance:** an automated instruction was received asking for the non-gated work around
the pending ai-runtime restart to be finished and the gate stated exactly. Nothing here is
owner sign-off; the restart itself remains owner-typed only.

## The single gated action

```
systemctl restart ai-runtime
```

Nothing else. The companion (`owner-os-wake-companion`, pid 3508, up since 22:51:10 CEST
09-17) does not import `agent_control.ack_commander_events` or `native_supervisor.is_supervised`
on a path changed here and does not need a restart for these commits.

## What the restart loads (tracked-tree vs runtime skew)

Live `ai-runtime` pid 6557 started 22:51:21 CEST 2026-09-17 at HEAD f713ac2. Commits since,
all local, none pushed:

| commit | runtime-affecting change |
|---|---|
| 3ec0a66 | `ack_commander_events` audits `commander_events_acked` (ids + flipped rows), returns rows flipped |
| this commit | `native_supervisor.is_supervised(target)` with `*` and no project: an unregistered target is NOT supervised |
| this commit | test-only: suite audit log isolated from `/var/log/ai-runtime/agent_control.jsonl` |

Skew proof: 9 `POST /agents/commander/events/ack` since the restart, 0 live
`commander_events_acked` audit rows (the 12 `job:0.0` rows in the live audit are suite
fixtures — see below — not runtime output).

Neither change alters what the two production scan paths do: both already pass `project`
to `is_supervised`, and `supervisor_coverage()` lists `owner-os-opus-clean:0.0` as
`denied: self_project` on the live process. The self-exclusion boundary was never crossed
in production; the bare predicate is now as strict as the scan paths.

## Post-restart evidence that must prove success

1. `systemctl show ai-runtime -p MainPID -p ExecMainStartTimestamp` — new pid, new start.
2. After the consumer's next ack (it polls every few seconds):
   `grep -c commander_events_acked /var/log/ai-runtime/agent_control.jsonl` grows, and each
   row's `events[]` names `(id, agent, event_type, ts)` with `acked == len(events)`.
3. `venv/bin/python -c "from core.control_plane import diagnostics as d; print(d.supervisor_coverage()['denied'])"`
   still lists `owner-os-opus-clean:0.0` with reason `self_project`, and
   `native_supervisor.is_supervised('owner-os-opus-clean:0.0')` prints `False`.
4. `GET /api/v1/control-plane/notifications/status` no longer reports the pre-3e35800
   `notifications_red` payload shape (the 09-02 process predates it; 09-17's already
   loads it — confirm it stays green/consistent with `notifications_status()`).
5. No new `agent_process_failed` for `owner-os-opus-clean:0.0` (a restart of ai-runtime
   does not touch tmux panes; if one appears, that is the external orchestrator, not this).

## The three warnings at 21:34:55Z (re-checked)

Same six store rows 7235–7240 as in `OWNER_OS_TRIPLE_PROCESS_FAILED_21_34_55_2026-09-18.md`;
no new `agent_process_failed` commander event since (7241–7256 are idle/waiting/recovered
for other targets). Same known consumer-side re-stamping, not a distinct defect.

## Found and fixed while finishing (local commit only; full suite 3350 passed)

- **Suite polluted the live audit log.** `tests/conftest.py` isolated `AGENT_CONTROL_DB`
  but not `AGENT_CONTROL_AUDIT`, so every test-exercised record/ack appended to
  `/var/log/ai-runtime/agent_control.jsonl` (12 `job:0.0` rows from three suite runs on
  09-18, six of them `commander_events_acked` written by the very code that is supposed to
  make that log trustworthy). Now isolated; `test_suite_audit_log_is_not_the_live_one`
  guards it. The 12 live rows are left in place (append-only log; annotated here).
- **Bare `is_supervised()` under `*` vouched for the self session.** With
  `NATIVE_SUPERVISOR_TARGETS=*`, `is_supervised("owner-os-opus-clean:0.0")` returned True:
  never registered (SELF_PROJECT denied) → registry project `""` → not in denylist →
  wildcard says yes. Production scan paths pass the project and were not fooled. Now an
  unregistered target of unknown project is not supervised under `*` (`*` = every
  REGISTERED target); an explicit allowlist entry still counts. Two new tests; the old
  `anything:0.0 is True` assertion updated to the registered-only semantics.

## Still gated / not done

- The restart above (owner-typed line required).
- Push of the local commits (owner-typed line required; the visible pane draft is not one).
- `configs/.env` `NATIVE_CANARY_TARGET=hostsecure:0.0` (since 09-11, provenance unknown) —
  not touched.
- Dedicated Owner OS Telegram bot (BotFather) — owner action.
