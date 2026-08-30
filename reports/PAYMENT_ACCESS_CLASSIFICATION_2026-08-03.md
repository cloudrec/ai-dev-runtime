# PAYMENT SERVER-ACCESS — CONTROL-PLANE TRUTH CORRECTION

**2026-08-03. Authenticated owner correction.** Read/emit-only; no pane touched, nothing
connected, no credential/endpoint invented, no agent created/resumed/stopped.

## Owner truth (recorded, trusted)

payment:0.0 previously **accessed and deployed all existing servers**; the required **SSH keys
are already installed**. Recorded as a trusted `owner_decision`
(`question_id=payment_server_access_classification`, `source_channel=cto_authenticated`,
`authenticated=true`) — live decision id issued, `trusted=true`.

## Corrected classification

A failed root+key SSH attempt to **RU-PROD / NL-edge** is **NOT** an owner credential/access
gate. It is **internal connection-mapping / key-selection recovery** in progress by payment —
the historical user / IdentityFile / host alias / ssh config must be recovered.

| Signal | Class | Owner notified? |
|---|---|---|
| publickey / permission denied / no such identity / host key verification / could not resolve host / too many auth failures | `internal_recovery` | **No** — tracked as a recovery task, inbox-only record |
| all keys removed / access revoked / account disabled / exhausted all keys/users/aliases/configs | `escalate` | Yes — genuine absence/revocation only |
| any of the above for a non-payment agent | `none` | (unchanged) |

## Behavior

- **No repeated "install keys" notifications.** A key-selection signal for payment is
  reclassified in `event_pipeline.publish_significant_event` → `note_recovery` (durable,
  `owner_action_required=false`, `push=false`, 6h dedup); it is NOT emitted as an owner
  `blocker`, NOT pushed, and NOT mirrored to the legacy commander surface.
- **Recovery task tracked** in `access_recovery_task(agent,host,state,attempts,...)`; attempts
  increment per recurrence.
- **Escalation gated**: only `access_recovery.escalate` (exhaustive absence/revocation proof)
  raises an owner-actionable event.

## Files

- `core/control_plane/access_recovery.py` (new) — classify / note_recovery / escalate /
  record_owner_truth / get_recovery_tasks.
- `core/control_plane/event_pipeline.py` — reclassify guard (scoped to payment:0.0).
- `tests/test_access_recovery.py` (new) — 17 tests.

## Status

Full suite **1017 passed**. Redeployed (`ai-runtime` only); loops alive, `restart_safe`,
`consistent`; classifier live (`RU-PROD publickey → internal_recovery`). Owner truth persisted
in the live control plane. No open owner gate for payment server access. No live recovery task
yet (classifier now in place; a task is created when payment actually emits a key-selection
signal through the control plane).
