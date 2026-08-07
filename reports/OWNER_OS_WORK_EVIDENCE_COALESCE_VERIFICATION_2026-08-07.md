# Owner OS — owner-action coalescing: final verification

**Date:** 2026-08-07 · **Scope:** `/root/ai-dev-runtime` only. No product project was
written to. Telegram credentials/config, CDP, the wake browser, MESS and every other
project were left untouched. No second runtime was created; no additional restart was
performed by this verification.

---

## 1. What was verified

The gap: one piece of work woke the owner once per save.
`MESS_AUTO_UPDATE_AND_MOBILE_MENU_2026-08-06.md` raised `work_partial_completion` three
times in sixteen minutes (22:47, 22:58, 23:03). The fingerprint was correct — the file
really was rewritten three times — but a new set of bytes was being treated as a new thing
to wake someone for.

The rule now in force: **evidence is never lost, delivery is.** Every material rewrite is
still its own event with its own dedup identity in the CTO inbox. What is bounded is how
often one *meaning* may interrupt the owner: at most once per 30 minutes per
`project + report + semantic reason`.

## 2. Commits

| Commit | What |
|---|---|
| `af81374` | `schema_version` forward-only (v7 recorded while v5/v6/v7 objects existed) |
| `a6af93b` | owner-action coalescing — one wake per meaning per 30 min; `work_evidence_push` (v8) |
| `3867712` | **follow-up, authored by a concurrent session at 02:46:22** — the window belongs to a meaning, not to a report |

`3867712` corrected a real defect in `a6af93b`: keying the window on the report alone meant
a report *flapping* between two already-delivered classifications
(partial → blocked → partial → blocked) read as "the classification changed" on every flip
and woke the owner four times for two decisions. The key became `<report>|<class_sig>` with
`evidence_key` retaining the report it belongs to, and the schema moved to **v9**.

Nothing was pushed. `reports/phase3_postfix_soak.jsonl` is a pre-existing modification and
was deliberately excluded from every commit.

## 3. Backups

| Path | Taken |
|---|---|
| `backups/schema_version_fix_20260807-003629/` | before the forward-only fix |
| `backups/work_evidence_coalesce_20260807-012830/` | before the coalescing work |
| `backups/work_evidence_coalesce_prerestart_20260807-015037/` | before the restart |

Each holds a `.backup`-consistent `control_plane.db` (`PRAGMA integrity_check` → `ok`) plus
the source files touched and a `BASELINE.txt` recording HEAD, `git status` and the running
PID/start time.

## 4. Test results

| Suite | Result |
|---|---|
| Full (`pytest tests/ -q`, PID 765807) | **1722 passed, 1 failed**, `PYTEST_EXIT_CODE=1`, 570.07s |
| Targeted (coalesce + work_evidence + schema_version) | **42 passed**, exit `0`, 86.84s |

The single full-suite failure is
`test_control_plane_canary_sim.py::test_flags_off_by_default_before_and_after_harness`.
It is **pre-existing and environmental**, not a regression: the shell exports
`CONTROL_PLANE_ACTUATOR_ENABLED=1` (the owner-approved `canary.conf` setting) while the
test asserts that flag defaults OFF. Re-run with `env -u CONTROL_PLANE_ACTUATOR_ENABLED …`
it is **8 passed, exit 0**. It fails identically against the pre-change baseline.

Coalescing coverage is 19 tests, including the live incident (three rewrites → three
events, one wake), the cooldown boundary, every re-opening reason, the flap case, the
`work_stopped_incomplete` path, durability across a module reload, a v8 → v9 cooldown
upgrade, and the two non-merging cases.

## 5. Live state

| Item | Value |
|---|---|
| Service | `ai-runtime.service` **active (running)** |
| PID / start | **758325**, **Fri 2026-08-07 02:46:56 CEST** |
| Health | `/api/v1/health` → **200** |
| DB path (resolved by the process) | `/root/ai-dev-runtime/control_plane.db` (`CONTROL_PLANE_DB` unset) |
| `schema_version` | **9** |
| `work_evidence_push` | present, v9 shape (`evidence_key`, `last_seen_at`) |
| Forward-only trigger | `trg_schema_meta_forward_only` present |
| Policy endpoints | `/policy/decisions`, `/policy/overrides`, `/policy/explain` → **200** |
| Duplicate `we:` dedup_keys | **0** |
| work-evidence | scanning after restart; mess 41 / arbitrage2 41 / cp-canary 24 rows, refreshed 00:52:38Z |
| MESS stage pointer | `stage_09_android_test_apk` — static, and MESS evidence is still seen on it |

The 14 duplicate dedup_keys that exist database-wide are all pre-existing agent-lifecycle
keys (`dead:`, `recovered:`, `sig:`, `actblock:`) unrelated to work evidence. No `we:` key
is duplicated.

### Live coalescing proof

Run against the **production database and schema**, under namespaced canary keys, emitting
no events and no notifications and consulting no wake bridge. Rows were deleted afterwards
(0 remaining).

```
1. first partial (t+0)                    deliver=True  reason=first_time              suppressed=0
2. same meaning rewrite (t+11m)           deliver=False reason=coalesced_same_meaning  suppressed=1
3. same meaning rewrite (t+16m)           deliver=False reason=coalesced_same_meaning  suppressed=2
   => 3 identical saves in 16 minutes produced exactly 1 delivery
4. semantic transition partial->blocked   deliver=True  reason=classification_changed  suppressed=0
5. flap BACK to partial (t+18m)           deliver=False reason=coalesced_same_meaning  suppressed=3
6. a DIFFERENT report, same time          deliver=True  reason=first_time              suppressed=0
7. same meaning after 31 minutes          deliver=True  reason=cooldown_expired        suppressed=3
```

Identical saves coalesce; a semantic transition passes immediately; a flap back to an
already-delivered meaning does not re-wake; different reports keep separate windows; the
cooldown expires at 30 minutes; `suppressed_count` and `last_seen_at` advance on every
suppressed repeat, so nothing is lost.

## 6. Not done, and why

The end-to-end canary that ends in a **real same-chat wake** was not run. The wake bridge is
enabled (`_enabled() == (True, False)`), so a live owner-action event would drive an actual
submission into the control chat through the companion — which the standing instruction not
to touch CDP or the wake browser rules out. The mechanism was instead proven against the
real production database at the decision level, as above. The full wake canary can be run
on `cp-canary` (the disposable canary project, never MESS) on request.

## 7. Rollback

Code:

```
git revert --no-edit 3867712 a6af93b     # or: git checkout af81374 -- core/work_evidence.py core/control_plane/store.py
systemctl restart ai-runtime.service
curl -s -o /dev/null -w '%{http_code}' http://172.17.0.1:8199/api/v1/health   # expect 200
```

Database: restore
`backups/work_evidence_coalesce_prerestart_20260807-015037/control_plane.db` over
`/root/ai-dev-runtime/control_plane.db` with the service stopped.

`work_evidence_push` is additive — leaving the table in place after a code rollback is
harmless, since older code never references it. The forward-only trigger means a rolled-back
process carrying an older `SCHEMA_VERSION` cannot rewind the recorded number.
