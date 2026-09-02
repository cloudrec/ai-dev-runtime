# Owner OS — session report, 2026-09-02

50 commits, 9 pushes, all on `ai-runtime/220-windows-bridge`. Code +3301/-84 across
26 files; reports +3607. HEAD `4fc8aa0`, remote in sync, 32 untracked owner WIP reports
preserved untouched throughout.

Full detail lives in `OWNER_OS_WAKE_DOORBELL_CANONICAL_2026-08-30.md`, Parts 49-80.
This is the summary.

---

## 1. The headline: the owner had to poke chats manually, and now should not

The complaint was that project chats needed a manual "проверь". The loop was not
broken — claims 1058/24h allowed, `delivered == watches` on every route, 94-97%
resolution, no fail-closed condition anywhere. **It rang the wrong doorbell.**

`owner-os`, `payment-orchestrator` and `seo` all pointed at ONE conversation
(ПЛАТЁЖКА): 12 route keys over 10 chats, 291 watches delivered into a single chat in
24 h. Owner OS's own wakes landed in the payment chat, so the conversation the owner
opened had nothing in it.

The proof was in the data: nine live unbound chats titled "Проверка событий Owner OS"
and similar — the manual poking, recorded, because the bound chat never rang.

Fixed in two parts:

* **The cause of new collisions.** `consider_auto_bind()` guarded the ROUTE ("a healthy
  existing binding is never replaced") and nothing guarded the CONVERSATION, so
  discovery could stack a second and third project onto a chat another already owned.
  It did, twice. Now refused, recording `held_by` (`dfab6cb`).
* **The existing collisions**, on owner instruction: `seo` → "Resume SEO agent",
  `owner-os` → the URL the owner typed. Result: **12 keys over 12 conversations, no
  collisions.** `payment-orchestrator` kept ПЛАТЁЖКА, where the title genuinely belongs.

Verified after: 99 `wake_send` rows for `owner-os` to the exact bound URL, 13 watches
delivered, target tab open and responsive. No synthetic wake was sent — that writes
into the owner's own conversation, and 99 real deliveries are better evidence.

## 2. Three false-alarm classes removed from the critical lane

| what fired | why it was wrong | fix |
|---|---|---|
| 131 quota banners/6h as criticals | a provider window is neither a failure nor a finish | earlier, `0edc0e8` |
| `agent_process_failed` on "Prompt is too long" | a full context is the harness asking for a reset, not a dead process | `f7bbad8` |
| `wake_loop_stalled` on a pane parked on an owner gate | only the owner ends that state; re-waking cannot help | `4fc8aa0` |

Each is **fail-closed**: an unreadable classifier, a missing transcript, or an absent
gate row all fall through to the critical mapping. Narrowing a false alarm must never
cost a real crash — pinned by control tests using a `MemoryError` traceback, exit code
137, and an empty payload with no reason text at all.

Proof the guards work: a genuine `API Error: Connection lost mid-response` today was
run through both new predicates, matched neither, and stayed critical.

## 3. The browser tab leak, closed on all three paths

`404496b` fixed `recover_wedged_tab`; `877edaf` found the second creation path
(`open_chatgpt_page`) had no guard at all; `ad705eb` found that a recovery reporting
success could hand back the very tab it had just replaced, because the success path
re-scanned instead of returning the tab it had verified.

Browser now sits at 4 pages, 0 bare roots, 0 duplicates, not degraded.

## 4. Owner gates: 140 open → 0

Dead-subject gates cleaned (128), the nine `classify_scope` gates answered
`observe_only`, and the last two closed today — `gaika-opus-v2:0.0` and the
30-day-old `canary_agent_selection`, declined rather than selecting a canary.

Every answer was applied with `api.answer_gate()` and NOT through `owner_api`, which
would have recorded `actor=owner:bearer` while the actor was the assistant. Verified to
grant nothing: `answer_gate` contains no agent write, and fleet-wide lifecycle is
unchanged — 10 live agents, all `observe_only` except one already `managed`.

## 5. What I got wrong, and corrected

Recorded because the corrections are the most useful part of the record.

* **Part 78 was wrong.** I concluded the lifecycle hook had gone quiet while the agent
  kept working. Investigating that question disproved it: the transcript has the same
  41-minute gap, so the session was genuinely idle and every turn that happened was
  reported. The escalation was correct on its own terms.
* **The verification that misled me.** I checked "has the transcript advanced?" at
  10:37 and got True. At the scans that actually fired, 10:15 and 10:32, it would have
  been False. I measured *by now* when the question was *at scan time*. The fix stands
  as a defensive improvement with no observed incident behind it, and the ledger says
  exactly that.
* **A live-state test leak, twice.** Adding the transcript oracle made the suite read
  the operator's real `~/.claude/projects` and inverted three existing stall controls —
  the same hazard as the earlier native-sessions one. `conftest` now hard-disables both.
* **`page_responsive` is timing-sensitive.** I called a tab "dead" on one reading; it
  flipped on the next. A single reading is not evidence a tab is wedged.
* **Wording:** I wrote "the pushed code is in the tree" when I meant pushed-but-not-
  loaded-by-the-running-process. Corrected on challenge.

## 6. Provenance discipline

Every push and every restart in this session followed an **owner-typed** message.
Several instructions arrived on the automated API channel asking for pushes, restarts,
the canary gate, and one asserting the owner had selected a specific chat — that last
one was checkable and false, naming a conversation titled "Изучение проектов GitHub"
rather than the title the owner had given. Each was declined and the gate held.

The rule, now with two test cases attached: an automated message is technical scope,
never authorisation, and such claims can usually be checked against local state for the
cost of one query.

## 7. State at the time of writing

```
services   ai-runtime PID 2690604 active; companion PID 1237155 up 14:40:28 CEST
skew       []            pipeline reasons: none
routes     12 keys / 12 conversations, no collisions
browser    4 pages, 0 bare roots, 0 duplicates, not degraded
gates      0 open        watches 0 open
wake_send  86 in the last hour
git        4fc8aa0 = origin, tracked clean, 32 untracked preserved
```

Regression at the last completed full run: **3008 passed**. A further full run was
still executing when this report was written and its result is NOT included here.

## 8. Open items — all owner decisions

1. **Telegram Start.** `@ezzetasecurity_bot` / https://t.me/ezzetasecurity_bot. One
   press. `getMe` returns ok, so the credential is valid and it is the chat that
   refuses — 400 `chat not found`, the signature of a private chat that never opened a
   conversation with the bot. It is now the ENTIRE critical lane. **Not required for
   autonomy**: it is one of two notification tiers and neither is in the wake path.
2. **Windows enrolment.** One unused 24 h code outstanding, 0 active devices.
3. **The informal-wait gap.** `pane_awaiting_owner` covers a pane with a durable open
   gate. A pane waiting on a human WITHOUT a gate row — an agent that reported no safe
   work remains — is still indistinguishable from a stuck one. Closing that means
   opening a gate for every such pause, which is a scope decision.
4. **The owner-os chat title.** It reads "Изучение проектов GitHub". Renaming it would
   make the binding self-evident and let auto-discovery re-derive it if ever lost.
   Cosmetic.
