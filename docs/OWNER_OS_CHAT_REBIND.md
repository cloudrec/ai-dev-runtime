# Owner OS — rebinding the wake bridge to a new ChatGPT chat

**New OWNER-OS control chat? Run one command:**

```bash
cd /root/ai-dev-runtime
tools/rebind_chat.py https://chatgpt.com/c/<conversation-id>
```

**Binding a PROJECT's work chat (multi-chat routing)? One command, one route:**

```bash
tools/rebind_chat.py https://chatgpt.com/c/<conversation-id> --route <project-id>
# e.g. --route mess          (MESS / Chemmy)
#      --route gaika-drop    (GAIKA Basket / extension)
#      --route gaika-video   (GAIKA video/media)
#      --route payment-orchestrator
tools/rebind_chat.py --routes    # show the whole registry
```

The route key is the event's `project_id` exactly as it appears in the event log. An event
whose project has an explicit route wakes THAT conversation; an event whose project has no
route wakes the owner-os control chat, recorded with reason `unmapped_route:<key>` — never
silently, and never some other project's chat. Nothing bound at all means nothing is sent.

It prints `PASS` when the binding is verified by a fresh resolve, `FAIL` and exit code 1
otherwise. Nothing else needs to change — no service edit, no `.env` edit, no restart, and
above all no hunt for a hardcoded URL.

This document exists because that hunt kept happening. A chat fills up, the owner opens a
new one, and the next session starts by grepping the server for the old link. There is
nothing to grep. Read on only if you need to know why.

---

## Single source of truth

| | |
|---|---|
| **Where routes live** | `control_plane.db` → table `wake_route`, one row per route key (project id); the owner-os control chat is route key `owner-os` |
| **Who may write them** | `core/wake_routes.py` → `bind_route()` (projects) and `core/wake_bridge.py` → `bind_chat()` (owner-os; also keeps the legacy `wake_target` row in lockstep) — nothing else |
| **Who reads them** | `core/wake_routes.py` → `resolve()`, called per event from `should_wake()` and `pending_wake()` |
| **Audit of every move** | tables `wake_route_audit` and `wake_bind_audit` (new URL, previous URL, who, when, note) |
| **Legacy `wake_target` row** | migration bridge only — read when the registry has no owner-os route; never universal routing |

There is exactly one row and exactly one writer. `active_chat()` is called **on every wake
decision and every companion tick** — the pointer is never cached in code, never baked into
a systemd unit, and never read from an environment variable. That is a deliberate design
property of the bridge (`core/wake_bridge.py`, section *"the active control chat: a
rotatable POINTER, never a hardcoded URL"*), not an accident of the current install.

Consequence worth stating plainly: **a rebind takes effect on the next companion tick
(≤ 20 s) with no restart of anything.** If you find yourself restarting a service to change
the chat, you are solving the wrong problem.

### What is *not* the source of truth

- `configs/.env` — holds `WAKE_BRIDGE_ENABLED=1` and unrelated secrets. **No conversation
  URL.** Do not edit it for a rebind.
- `/etc/systemd/system/owner-os-wake-companion.service` — runs `tools/wake_companion.py`,
  passes `CONTROL_PLANE_DB` and the env file. **No conversation URL.**
- `/etc/systemd/system/owner-os-chromium.service` — launches the browser at
  `https://chatgpt.com/` (the site root, not a conversation). It is a *starting page*; the
  composer navigates the tab to the bound conversation itself. **Not a target, do not edit
  it to rebind.**
- `reports/OWNER_OS_WAKE_*.md` — historical incident records. They quote the conversation
  IDs that were live at the time. They are **history, not configuration**; a URL found there
  is by definition stale.
- Test files — `tests/test_wake_*.py` and `tests/test_cdp_composer.py` use fixture URLs like
  `https://chatgpt.com/c/abc`. Never real targets.

---

## Procedure

### 1. Rebind

```bash
cd /root/ai-dev-runtime
tools/rebind_chat.py https://chatgpt.com/c/<conversation-id> --note "why this rotation"
```

The script, in order: validates the URL through the bridge's own predicate → prints the
current target → writes a point backup → calls `bind_chat()` → re-reads `active_chat()` and
compares → prints `PASS`/`FAIL`.

Equivalent wrapper, kept for muscle memory — it delegates to the same script, so it gets the
same backup and verification:

```bash
tools/owner-os-chat bind https://chatgpt.com/c/<conversation-id>
```

Useful flags: `--dry-run` (validate and show, write nothing), `--show`, `--history`.

### 2. Backup and audit (mandatory, and automatic)

The script writes a point backup before touching the row:

```
.ai-runtime-backups/wake_target/wake_target_<UTC-timestamp>.sql
```

It contains only the `wake_target` and `wake_bind_audit` tables as replayable SQL — enough
to read the old URL by eye and to restore the pointer. It is deliberately **not** a copy of
`control_plane.db`: that file is ~25 MB of live state that eleven workers write to
concurrently, and snapshotting all of it to change one row is the bigger risk, not the
smaller one.

The audit row in `wake_bind_audit` is written by `bind_chat()` in the same transaction as
the pointer, so a rebind cannot land unaudited. Review it with:

```bash
tools/rebind_chat.py --history
```

`--no-backup` exists for tests and disposable databases. Do not use it against the live one.

### 3. Verify the service uses the new target

```bash
# 1. the pointer itself
tools/rebind_chat.py --show

# 2. what the running companion will actually resolve on its next tick
WAKE_BRIDGE_ENABLED=1 venv/bin/python -c \
  "from core import wake_bridge as wb; import json; print(json.dumps(wb.pending_wake(), ensure_ascii=False))"

# 3. the service is alive and reading the same database
systemctl status owner-os-wake-companion --no-pager
journalctl -u owner-os-wake-companion -n 20 --no-pager
```

Step 2 is the one that matters: `pending_wake()` is the exact call the companion makes, and
the `conversation` field it returns is the exact URL the companion will submit into. If that
field shows the new ID, the rebind is live.

After any delivery, the question "which chat did that send actually go to" is answerable
from state alone — every `wake_delivery` row records the conversation the attempt resolved
to at submission time:

```bash
sqlite3 /root/ai-dev-runtime/control_plane.db \
  "SELECT at, event_id, delivered, reason, conversation FROM wake_delivery ORDER BY id DESC LIMIT 5"
```

A row whose `conversation` is not the currently bound URL is a send that happened before
the rebind, never after it: the target is resolved fresh per tick and per attempt.

### 4. Non-destructive smoke test

Checks the path all the way to the composer, **typing nothing and sending nothing** into the chat:

```bash
venv/bin/python - <<'PY'
import sys, time; sys.path.insert(0, "/root/ai-dev-runtime")
from tools import cdp_composer as cdp
from core import wake_bridge as wb

url = wb.active_chat()["conversation"]
t = cdp.find_target(url)
if not t:                                    # tab is on some other ChatGPT page
    t = cdp.find_chatgpt_page()
    s = cdp._Session(t["webSocketDebuggerUrl"]); s.call("Page.enable")
    s.call("Page.navigate", {"url": url}); s.close()
    for _ in range(15):
        time.sleep(2)
        t = cdp.find_target(url)
        if t: break

s = cdp._Session(t["webSocketDebuggerUrl"]); s.call("Runtime.enable")
for _ in range(10):
    if s.boolean('document.readyState === "complete"') and s.count(cdp.COMPOSER_SEL) == 1:
        break
    time.sleep(2)
print("composer:", s.count(cdp.COMPOSER_SEL))
s.call("Runtime.evaluate", {"expression": f"document.querySelector({cdp.COMPOSER_SEL!r}).focus()"})
print("focused :", s.boolean(f"document.activeElement === document.querySelector({cdp.COMPOSER_SEL!r})"))
s.close()
PY
```

`composer: 1` and `focused: True` — path is healthy. The probe stops there on purpose: it
never calls `Input.insertText` and never clicks send, so it cannot put a phrase in the
owner's chat and cannot consume the global cooldown.

If the page takes long to settle, do not sit on it — record what you got (navigation OK /
readiness unknown) and move on. The rebind is already verified by step 3; this probe only
adds information about the browser half.

### 5. Restart — usually **not** needed

The companion re-resolves the target every tick, so a rebind needs no restart. Restart only
if the service is genuinely wedged, and check health afterwards:

```bash
systemctl restart owner-os-wake-companion
systemctl status owner-os-wake-companion --no-pager
venv/bin/python -c "from core import wake_bridge as wb; import json; print(json.dumps(wb.health(), ensure_ascii=False, indent=1))"
```

In `health()`: `last_delivery_ok` is the field that says whether the phrase actually landed;
`deliveries_failed_total` rising while `wakes_total` also rises means the browser half is
failing, which is a *separate* problem from the pointer and is never fixed by rebinding.

> `health()` run from an interactive shell reports `enabled: false` unless you export
> `WAKE_BRIDGE_ENABLED=1` — the switch is read from the environment at call time and the
> service gets it from `configs/.env`. That is a property of your shell, not an outage.

---

## Do NOT

- **Do not grep the server for the old URL.** There is one row; this runbook names it.
- **Do not edit `configs/.env`** to change the chat. It has never held the target, and
  editing it risks the secrets that live beside it.
- **Do not edit the systemd units.** Neither the companion nor the chromium unit carries a
  conversation.
- **Do not hardcode the URL** into `wake_bridge.py`, `wake_companion.py`, or
  `cdp_composer.py`. The pointer is rotatable by design; a constant there means a code
  change on every rotation.
- **Do not `UPDATE wake_target` by hand in sqlite.** It bypasses `wake_bind_audit`, and the
  pointer's whole value is that every move is attributable.
- **Do not `git checkout` / `stash` / `reset`** to "clean up" first. This repository routinely
  carries dirty files from other work; discarding them to make a one-row change is
  destroying someone else's work to avoid reading a runbook.
- **Do not copy a URL out of `reports/OWNER_OS_WAKE_*.md`.** Those are dated incident
  records. The live value comes from `--show`, always.
- **Do not send a real wake phrase as a "test".** It goes into the owner's chat and burns the
  global cooldown. The step-4 probe exists precisely so you never have to.

---

## Related

- `core/wake_bridge.py` — decision, dedupe, cooldown, acknowledgement, and the pointer.
- `tools/wake_companion.py` — the service half: asks `pending_wake()`, submits the fixed
  phrase, and knows no URL of its own.
- `tools/cdp_composer.py` — the browser half; matches the tab **by the bound URL**.
- `tests/test_rebind_chat.py` — URL validation and rebind behaviour, against a temp DB.
- `reports/OWNER_OS_WAKE_BRIDGE_REPAIR_2026-08-11.md`,
  `reports/OWNER_OS_WAKE_REBIND_STATUS_2026-08-08.md` — history of previous rotations.
