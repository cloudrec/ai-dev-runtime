# Canary candidates for the zero-ping effectiveness proof

**Date:** 2026-09-04 · `/root/ai-dev-runtime` · read-only analysis, nothing selected.

An automated instruction was received via the Owner OS API asking for this matrix; it is
not owner sign-off. **No canary was selected or activated.** `NATIVE_CANARY_TARGET`
remains unset and `core/continuation_verifier` remains dormant.

## Pool

Ten live agents. Five are excluded by existing policy and by the exclusion list —
`owner-os-opus-next` (self_project), `payorch-ha-next`, `capacity-blockchain`,
`diamond-auction`, `email` (denylisted_project). The five `covered` agents remain.

## Matrix

Window 14:00–22:00Z. `deliv` = wake_delivery rows on that agent's route; every delivery
landing between a continuation and the turn that follows it forces the sample to be
DISCARDED as `unattributable`, so a quiet route is what makes a canary usable.

| agent | cont | turns | deliv | route shared | stall/exhaustion (all time) | gate now |
|---|---|---|---|---|---|---|
| `gaika-opus-v5:0.0` | 7 | 11 | **0** | no | **none** | none |
| `security-demo:0.0` | 28 | 58 | 15 | **yes** (w/ hostsecure) | exhausted 5, dead 1 | none |
| `mess-safe-finish:0.0` | 22 | 25 | **74** | no | exhausted 1 | 25 owner-gated events |
| `hostsecure:0.0` | 8 | 14 | 10 | **yes** (w/ security-demo) | exhausted 8, no_progress 8, stalled 4 | **continuation_gate_open x5** |
| `mess-postsignup-cleanup-sonnet-v4:0.0` | 5 | 6 | 10 | no | exhausted 16, stalled 13, no_progress 19, dead 1, process_failed 1 | idle |

### Why each is or is not suitable

* **`gaika-opus-v5:0.0` — suitable, and the only clean one.** Zero deliveries in the
  window and the last one on that route was 04:49Z, sixteen hours earlier, so the chat is
  effectively dormant and essentially every sample would be attributable. No stall,
  exhaustion, death or process failure in its entire history — the only candidate with a
  clean record. `agent_watch_state` reads `working`, emissions 0, miss_count 0. Sole
  occupant of its conversation. No owner-gated events. Seven continuations and eleven
  turn boundaries in eight hours is ample signal for a verdict within hours.
* **`security-demo:0.0` — highest signal, worst noise.** Most active by far, but it
  SHARES conversation `...bd75-733306a73900` with `hostsecure` by deliberate owner
  design, so deliveries for EITHER route land in the same chat and taint samples on both
  — 25 combined in the window. Attribution would be discarded constantly.
* **`mess-safe-finish:0.0` — unsuitable, noisiest route.** 74 deliveries in eight hours
  means a wake lands somewhere in most continuation windows. 25 owner-gated events also
  make it the busiest gate surface.
* **`hostsecure:0.0` — excluded, currently gated.** The supervisor is skipping it with
  `continuation_gate_open` (5 times in the window); it is stopped on a real gate, which
  the exclusion criteria rule out. Also shares its conversation.
* **`mess-postsignup-cleanup-sonnet-v4:0.0` — unsuitable, unhealthy.** 16 continuation
  exhaustions, 13 stalls, 19 no-progress, one death and one process failure: the sickest
  candidate. Currently idle with only 6 turn boundaries. A canary must be able to fail
  for canary reasons, not because the agent is already broken. (Its `seo` project
  attribution is CORRECT despite the `mess-` session name — `cwd=/opt/seo`. Checked; not
  a defect.)

## Recommendation — not a selection

**`gaika-opus-v5:0.0`** is the safest candidate: a quiet route, an unshared conversation,
a clean history, no open gate, and enough turn boundaries to reach a verdict quickly.

**What would prove success.** After a native continuation at `t0` with pane digest `d0`,
a NEW `agent_turn_stopped` for `gaika-opus-v5:0.0` arriving after `t0` whose digest
differs from `d0`, with NO `wake_delivery` on route `gaika-extension` between `t0` and
that turn. Three consecutive such samples give `proven`.

**What would make it fail closed.** No qualifying turn within the timeout records
`continuation_unverified`, which resets the streak — absence of evidence is failure, not
silence. A turn carrying the unchanged digest counts for nothing. Any delivery landing in
the interval discards the sample as `unattributable` rather than crediting it, so a
ChatGPT wake can never be mistaken for supervisor effectiveness.

## The gate

One line from the owner unlocks it:

> Use `gaika-opus-v5:0.0` as the canary.

Setting `NATIVE_CANARY_TARGET` is the activation step and was NOT taken here. Zero-ping
remains UNRESOLVED: delivery is proven, effectiveness is not.
