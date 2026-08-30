# Wake companion — rebinding to a new control chat

**Date:** 2026-08-08 · **Mode:** read-only inspection. **No rebind was performed** and no test
wake was sent, for the reason in §3. Nothing in `/opt/mess` or any product project was
touched; CTO inbox and `cto_cursor` are untouched.

---

## 1. The bridge is healthy and still delivering — into the OLD chat

| Item | Value |
|---|---|
| `owner-os-wake-companion` | active (running), PID **3981253**, since 2026-08-06 20:05:20 CEST, `NRestarts=0` |
| `owner-os-chromium` | active (running), PID **3893871**, since 2026-08-06 19:34:42 CEST |
| Bridge enabled / kill switch | **true** / **false** |
| Cooldown | 900 s |
| Wakes total | **65** |
| Last wake | event **3535**, decided `2026-08-08T06:31:56.092289Z`, submitted 08:32:21 CEST, **acknowledged** |

Recent submissions (journal, CEST): 06:31:40 → 3526, 06:54:08 → 3527, 07:31:59 → 3531,
08:11:49 → 3532, 08:32:21 → 3535. Nothing is broken; the phrase is simply arriving in the
conversation the owner has moved away from.

## 2. Where the binding lives

Table `wake_target` in `/root/ai-dev-runtime/control_plane.db`, a single enforced row
(`CHECK (id = 1)`), read fresh on every wake — never cached in code or in the unit:

```
id           = 1
conversation = https://chatgpt.com/c/6a7423e6-06d8-83eb-b1fb-5408e0b3b3b9
bound_at     = 2026-08-06T14:01:43.477218+00:00
bound_ts     = 1786024903.47537
bound_by     = owner
```

Every move is appended to `wake_bind_audit` (3 rows so far: `bind`, `rebind`, `rebind`, all
`by=owner`, 2026-08-06 14:00–14:01). There is **no HTTP endpoint and no MCP tool** that binds
a chat — `wake_bridge.bind_chat()` is the only writer, deliberately.

## 3. Why nothing was rebound: the new chat is not open on the server

Server Chromium exposes exactly **one** page target over CDP (`127.0.0.1:9222`):

```
A399ACD5C7BA | https://chatgpt.com/c/6a7423e6-06d8-83eb-b1fb-5408e0b3b3b9
```

That is the **already-bound** conversation. No second ChatGPT tab exists, so there is no new
conversation to identify. Inventing an ID, or navigating the tab somewhere to "discover" one,
would be exactly the guess the design forbids — a wrong ID sends the owner's doorbell into a
chat nobody is reading, and `valid_conversation()` would happily accept a well-formed but
wrong URL.

## 4. What the owner must do — either one is enough

**Option A — give the URL (fastest, no browser interaction).**
Paste the new conversation URL. The companion navigates the existing tab to the bound URL on
every submission, so the tab does not need to be opened by hand; the profile is already
signed in. Then:

```bash
cd /root/ai-dev-runtime
venv/bin/python -c "from core import wake_bridge as wb; \
print(wb.bind_chat('https://chatgpt.com/c/<NEW-CONVERSATION-ID>', by='owner', \
note='rotated 2026-08-08'))"
```

`bind_chat` refuses anything that is not a conversation URL, writes `wake_target` atomically
and appends the `rebind` row to `wake_bind_audit`.

**Option B — open the chat on the server.**
Open the new conversation in server Chromium through noVNC (`127.0.0.1:6080`, reachable over
an SSH tunnel; x11vnc on `127.0.0.1:5901`, both localhost-only). Once it is the open tab,
its URL can be read from CDP and bound with no guessing at all.

## 5. Verifying the rebind

There is no endpoint that injects a synthetic wake, and none should be added casually. Two
honest ways to prove delivery:

1. **Wait for the next real urgent event.** One arrives roughly every 20–60 minutes at the
   current rate; `wake_bridge.health()` then shows the new `last_wake_at` and the phrase
   appears in the new chat.
2. **One deliberate submission** through the same choke point, which still consumes the
   global claim and the 900 s cooldown:

```bash
cd /root/ai-dev-runtime
venv/bin/python -c "import sys; sys.path.insert(0,'tools'); \
from cdp_composer import submit_phrase; from core import wake_bridge as wb; \
print(submit_phrase(wb.active_chat()['conversation'], wb.WAKE_PHRASE, source='owner-test'))"
```

**Caveat that must be settled first.** `tools/cdp_composer.py` and `core/wake_bridge.py`
currently carry the **uncommitted, paused** delivery-verification patch. The running
companion is unaffected — it holds the pre-patch modules in memory from 2026-08-06 — but a
*fresh* Python process picks the patched code up from disk, and its new rule (delivery counts
only when the user-turn node count rises) has never been exercised against the live DOM. So
either restore those two files from
`backups/wake_delivery_verify_20260807-182631/` before running the manual test, or accept that
a `user_turn_not_observed_after_send` result may be a selector problem rather than a real
delivery failure.

## 6. One incidental change, disclosed

Calling `wake_bridge.health()` during this inspection executed the patched module's
`CREATE TABLE IF NOT EXISTS wake_delivery`, so that table now exists in the production
`control_plane.db` — **empty, 0 rows**. It is additive and inert: no running process
references it, and `SCHEMA_VERSION` is unchanged at 9. It can be left in place or dropped
when the paused patch is decided. Nothing else in the database was written.
