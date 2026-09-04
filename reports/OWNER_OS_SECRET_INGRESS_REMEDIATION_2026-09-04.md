# Event-store secret ingress — what is fixed, and what needs the owner

**Date:** 2026-09-04 · **Repo:** `/root/ai-dev-runtime` · Contains no secret values.

An automated instruction was received via the Owner OS API reporting a credential-shaped
value in an event summary; it is not owner sign-off and nothing here was owner-approved.
The claim was verified independently before any change was made.

## What was verified

A scan of all 29,995 rows of `event` and every text column of every table in
`control_plane.db` found **no structured provider key anywhere** — no Telegram bot token,
no `sk-` / `sk-ant-`, no `ghp_`, no AWS key id, no private-key block.

It did find two `agent_turn_stopped` payloads holding credential-SHAPED values:

| event | field | shape |
|---|---|---|
| 21903 | `payload` | `token=` + 22 chars, entropy 3.88, non-prose |
| 24179 | `payload` | `password=` + 33 chars, entropy 3.80, non-prose |

Classified by shape only. **No value was read, printed, logged or copied** at any point,
here or in the commits. Whether they are live credentials was not determined and cannot
be determined without reading them.

## Fixed (local commits, not pushed, not deployed)

| commit | path | field |
|---|---|---|
| `0988577` | `hooks/owneros_hook.py` | `last_assistant_message`, `message`, `error_details`, `task_subject` |
| `0988577` | `core/agent_control.py` | bare-credential patterns + `redact_obj()` |
| `2288f7c` | `core/agent_watch.py` | `excerpt` + `action_taken` |
| `2288f7c` | `core/stall_doctor.py` | `pending` x2 + `action_taken` |
| `81778e6` | `core/os_task_queue.py` | `text` x2 (queued instruction text) |

Two classes of defect:

1. **Four writers persisted captured agent/user text with no redaction.** The hook
   (600 chars of model output), the pane excerpt (`excerpt_of(tail)`), and the agent's
   INPUT LINE (`pending_input_text` — text typed but not yet submitted). The project had
   already settled the rule in `windows_bridge`, which redacts everything a device
   returns before storing it; these three paths bypassed it. `pending` is the sharpest,
   because an unsent input line is exactly where a pasted credential sits and the
   standing gate is "paste the BotFather token in the file". The fourth is
   `os_task_queue`, which copies a queued task's instruction text into the event
   payload; its `os_task` ROW is left verbatim on purpose, because the task must be
   delivered as written.

   The remaining nine payload-writing modules were checked individually rather than
   assumed internal — tmux socket stderr, host probe results, SSH key-selection detail
   and literal explanatory strings. None carry captured pane, model or user content.

2. **The redactor only caught ASSIGNMENTS.** Every pattern but the private-key block
   required `token=` / `Bearer` in front. A pasted credential has no prefix. Bare
   patterns added for Telegram bot tokens, `sk-ant-`, `ghp_`, AWS key ids.

All three writers now fail CLOSED: if the redactor is unavailable the text is withheld
rather than emitted raw. In `stall_doctor` redaction is applied ONLY to the emitted
copies — `may_submit_queued`, `decide` and `classify_wait` still read the real text,
because /clear safety depends on it, and a test pins that.

Verification: 420 tests pass across the six affected suites. Each fix was confirmed by
removal. Two of the tests assert the WIRING rather than the redactor, because the first
five would have passed with the call sites never connected.

## Not done — and why

**Historical rows 21903 / 24179 were NOT scrubbed.** Rewriting rows in the durable audit
ledger is a mutation with its own tradeoffs — audit integrity against exposure — and it
is not a decision to take unattended. The fixes stop new ingress; they do not rewrite
what is already stored.

**No credential was rotated.** Nothing was pushed, deployed, restarted, or reconfigured.

## What requires the owner's direct action

1. **Decide on rows 21903 and 24179.** If those values are live credentials, the ledger
   is a plain file on this host and anything with filesystem access can read it —
   rotation at the issuing service is the only real remedy, and scrubbing the rows is
   cosmetic by comparison. If they are not live, no action is needed. Determining which
   requires reading them, which is the owner's call, not an agent's.

2. **Push `aaf1bd4`, `0988577`, `2288f7c`** — three local commits, tracked tree clean.

3. **Deploy.** The redaction fixes only take effect for processes started after them.
   `ai-runtime` (PID 2690604, up since 2026-09-02) and the wake companion both need a
   restart to stop emitting unredacted text. Until then new events keep arriving by the
   old paths.

4. **The pre-existing Telegram gate is unchanged:** a dedicated BotFather bot, its token
   into `configs/.env`, after which Owner OS derives the chat id itself. Note the
   interaction — pasting that token into a pane is exactly the event these fixes are
   meant to contain, so deploying them BEFORE pasting is the safer order.

5. **`ai-runtime` deploy skew** (separate, pre-existing): its `notifications_red`
   payloads still lack `cdp_same_chat`. Same restart clears it.
