# Zero-ping effectiveness — proven, with its limits

**Date:** 2026-09-05 · `/root/ai-dev-runtime` · branch `ai-runtime/220-windows-bridge`

The owner typed "use gaika-opus-v5:0.0 as the canary" in the pane. Everything downstream
of that decision is recorded here. Automated Owner OS API instructions drove the
surrounding work; those are not owner sign-off and are described as such throughout.

## The result

```
target      gaika-opus-v5:0.0        project gaika-extension
identities  gaika-opus-v5:0.0, session:de9f3ec4-fd4     turns_matched_by: agent
samples 15  ->  verified 10 · continuation_unverified 4 · pending 1
streak 5 / required 3                                    verdict: PROVEN
timeout_secs 3600
```

For 10 of 15 real continuations the agent produced a NEW turn boundary whose pane digest
differed from the digest at continuation time, inside the window, with NO wake_delivery
on its route anywhere in the interval — that route's last delivery was 04:49, before
every continuation. Stop -> native auto-continue -> new work, attributable to the
supervisor and not to ChatGPT.

Four samples failed honestly as `continuation_unverified`. That is what makes the ten
mean anything: the check can fail and does.

## What this does NOT establish

* **Retrospective, not observed live.** Verdicts are computed from durable history
  (`native_supervision`, `event`, `wake_delivery`) rather than gathered by an observer
  armed beforehand. That is real production behaviour rather than a staged test, which
  is arguably stronger — but it is not a live watch.
* **Not uniform.** A third of samples produced no qualifying turn.
* **The window is a judgement.** The same data gives 0 verified at 600s and 10 at 3600s.
  3600 was chosen from gaika's observed turn cadence (666-7058s), not from a measured
  optimum. `agent_turn_stopped` fires when a turn ENDS, so productive work reports late.
* **One agent.** Nothing here generalises to the other four covered agents.

## Three defects found while doing this, all fixed

| commit | defect |
|---|---|
| `40159a4` | the verifier had NO CALL SITE — activating the canary would have recorded nothing |
| `40159a4` | project resolution could return "", silently disabling the attribution guard |
| `858c357` | turns attributed by PROJECT, not by agent |

The third mattered most. The agent alias was built from `agent.session`, which holds the
TMUX name (`gaika-opus-v5`), while hooks write under `session:<conversation_id[:12]>`
(`session:de9f3ec4-fd4`). The alias matched nothing, so everything fell back to project
matching — and `gaika-extension` carries turns from four sessions across five dead
predecessor agents. A continuation could have been credited with another agent's work.

The verdict is IDENTICAL under strict per-agent matching, so the conclusion stands; it
now stands on evidence that can be defended rather than on the predecessors happening to
have stopped before the first continuation.

## Activation and rollback

```
configs/.env  +3 lines (perms 600, unchanged)
  # canary for native-continuation effectiveness verification (owner-selected 2026-09-05)
  NATIVE_CANARY_TARGET=gaika-opus-v5:0.0
  NATIVE_CANARY_TIMEOUT_SECS=3600

backup   backups/activate_canary_20260904T235702Z/.env.before  (600) + ROLLBACK.md
```

No secret was read, printed or altered; verification was by key name and line diff only.
The file was re-validated after the edit: 30 parsable KEY= lines, no malformed lines, no
duplicate keys, trailing newline present, sources cleanly, `NATIVE_SUPERVISOR_TARGETS=*`
unchanged — so the next service restart will not trip on it.

Rollback is complete and needs nothing else, because the mechanism writes nothing and
adds no schema:

```
sed -i '/^NATIVE_CANARY_TARGET=/d;/^NATIVE_CANARY_TIMEOUT_SECS=/d;/^# canary for native-continuation/d' \
  /root/ai-dev-runtime/configs/.env
```

Long-running services read the env at start and were NOT restarted, so `ai-runtime` and
the wake companion still hold the pre-activation environment. The diagnostic reads the
env at call time, so an on-demand invocation already sees the canary — which is how the
verdict above was produced.

## How to re-read the verdict

```python
from core.control_plane import diagnostics as d
d.native_continuation_effectiveness()          # dormant if the canary keys are removed
```

## Gates still standing

Push (13 local commits) · `ai-runtime` restart (clears the 2026-09-02 deploy skew behind
the stale `notifications_red` payloads) · companion restart · the Telegram BotFather
token, still the cause of 5,800+ dead letters · ledger rows 21903/24179.
