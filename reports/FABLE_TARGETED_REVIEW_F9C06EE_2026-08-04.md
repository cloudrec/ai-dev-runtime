# Fable targeted review — f9c06ee (fail-closed RU/EN dialog + continuation gate)

Date: 2026-08-04 · Reviewer: Fable 5 (one authorized pass) · Scope: exactly `git show f9c06ee`
(parent b4153fa) on branch owner-os/control-plane-v2. Read-only + targeted test runs in a
throwaway worktree; zero pane contact; no service/env/allowlist change; no code changed.

## Verdict: PASS

The commit does what it claims. All six review points checked; no HIGH findings. The
governing rule (UNSURE ⇒ REFUSE) holds on every auto-submit path I could construct, for
RU and EN, including homoglyph/zero-width/mixed-script and ANSI/box-frame/wrap evasions.
No existing guard is weakened — the diff is strictly additive on guards
(`_FORBIDDEN_RE` is a superset of the old alternatives verbatim; 3b false-idle, 3c
pending-input, step-2 policy recompute, canary confinement, lease/fence, delivered-poke
ledger all untouched by the diff).

## Point-by-point

1. **Dialog detection** (`core/agent_control.py:707-762,810-817`). Probed evasions all
   detected: ANSI SGR + charset escape (`\x1b(B`) mid-word, OSC title noise, box-frame
   glyphs, line-wrap splitting the question word (numbered options still match),
   fullwidth `？`, NBSP. `classify_state` ORs `_DIALOG_RE` into rule 2 → `waiting_owner`;
   active-marker precedence (rule 1) correctly keeps a live turn "working". No fail-OPEN
   path found for recognised shapes; residuals below (M1, M2).
2. **Continuation allowlist** (`core/agent_continuation_watchdog.py:248-306`). Script
   gate is printable-ASCII-only and runs before any accept except the exact bare
   `/clear|/compact` (which is ASCII by construction — unicode-whitespace padding via
   `\s` is harmless). Zero-width (U+200B), Cyrillic homoglyph `сontinue`, CJK,
   transliteration, digits, dialog answers, >300 chars, embedded `\n/clear` — all
   refused (probed). `\x0b` is stripped by `.strip()` before the gate — harmless.
   Vocabulary+punctuation composition ("continue; do the run and commit") passes, but
   the closed vocab contains only meta-work verbs (commit-locally by design); no
   destructive composition constructible — push/deploy/delete/send/etc. are denylisted
   first.
3. **Real-tail wiring** (`core/agent_continuation_watchdog.py:588-599`). Confirmed
   `agent_list()` (core/agent_control.py:902-911) emits no `_tail`/`_pending` keys, so
   the `is None` backfill fires on every production branch and `decide` sees the live
   12-line snapshot tail. Residual M2: a capture failure yields `""`, not an error.
4. **Ordering** — no hole. Every keystroke path passes a dialog gate: watchdog
   `decide` gates before idle-confirm/submit/deliver; actuator 3b2 sits after lease
   (1), policy recompute (2), idempotency (3), false-idle (3b) and before pending-input
   (3c) and delivery (4) — all refusal-only checks, order-insensitive among themselves.
   Autopilot `evaluate` gates before the poke decision, and `deliver_next_step` →
   `actuate` re-checks at delivery. Exceptions truly refuse: actuator `dialog_sig`
   pre-set to `"dialog_detection_unavailable"` and only overwritten by a successful
   call; `pane_shows_dialog` returns True on any detector exception; an import failure
   in `evaluate` raises before any poke (crash-closed). All three verified by the
   detector-down tests.
5. **Test non-vacuity** — verified by revert, see evidence below. Exactly 23/31 fail on
   the b4153fa baseline; single-file reverts attribute failures cleanly (8 to
   agent_control, 17 to watchdog, 3 to actuator, 1 to autopilot; overlapping subsets as
   expected). The 8 pins are non-vacuous: removing one vocab word ("canary") in-process
   flips `is_safe_continuation` of the pinned registry step to False, which would fail
   two pins; the registry pin loads the real 5-entry registry and asserts non-empty.
6. **Conftest trap** — confirmed `tests/conftest.py:16` hardcodes
   `sys.path.insert(0, "/root/ai-dev-runtime")`. My baseline run repointed both lines to
   the worktree AND appended an assertion that `core.agent_control.__file__` resolves
   inside the worktree; it printed
   `IMPORT-FROM: …/scratchpad/wt-b4153fa/core/agent_control.py`. The commit's claimed
   proof method (worktree + repointed conftest) is sound and its 23-fail claim
   reproduces exactly.

## Findings (no HIGH)

- **M1** `core/agent_control.py:711` — `_DIALOG_RE` is a denylist of dialog *shapes*;
  an unrecognised phrasing evades it. Evidence:
  `"Allow this tool to run?\n> approve / deny"` → `looks_like_dialog` False. Claude
  Code's own dialogs (numbered options, y/n, trust, credential) are covered, but a
  third-party CLI prompt with nonstandard wording inside a pane would not be, and a
  proactive continuation could answer it. Inherent to the regex approach; mitigated by
  dwell + pending-input + verify. Accept as residual or extend patterns over time.
- **M2** fail-open on an *unobservable* pane: `_pane_tail` returns `""` on
  capture-pane failure (`core/agent_control.py:832-833`), `pane_shows_dialog` returns
  False for empty tail (`core/agent_continuation_watchdog.py:311-312`), actuator 3b2
  gets no signature from `""`. If capture-pane failed while send-keys still worked,
  every tail-based guard (dialog, thinking, false-idle) would silently disable and a
  proactive step could be pasted blind. Narrow: both verbs share the tmux server/target
  failure domain, and verify would then fail (after the paste). Contradicts
  UNSURE⇒REFUSE in spirit. Proposed (not applied, >1 line): in `run_once`, skip an
  alive agent whose snapshot tail comes back empty, counting it as an error.
- **L1** `Controller.snapshot` captures 12 lines; a dialog whose only recognisable
  markers scrolled >12 lines above the bottom would be missed. In practice options +
  cursor render at the bottom. No action.
- **L2** `_RESUME_TEMPLATE_RE` path slot (`core/control_plane/actuator.py:77-84`)
  admits any `[A-Za-z0-9._/-]` path → an autonomous_safe instruction to read an
  arbitrary file. Only reachable by internal callers under lease+canary, and `.ssh`
  paths are coincidentally PROHIBITED via the pre-existing `ssh\b` token (probed).
  Informational.
- **L3** false-positive breadth: bare `password:` / "are you sure" in ordinary
  scrollback classifies an at-rest pane `waiting_owner` — suppresses continuation and
  may notify the owner. Safe direction (over-refusal accepted by design). No action.

## Revert evidence (worktree `wt-b4153fa`, conftest repointed, import origin asserted)

| Configuration | Result |
|---|---|
| HEAD f9c06ee, main tree | 31 passed |
| Full baseline b4153fa | **23 failed, 8 passed** (matches commit claim exactly) |
| Only `core/agent_control.py` reverted | 8 failed, 23 passed |
| Only `core/agent_continuation_watchdog.py` reverted | 17 failed, 14 passed |
| Only `core/control_plane/actuator.py` reverted | 3 failed, 28 passed |
| Only `core/commander_autopilot.py` reverted | 1 failed, 30 passed |

Baseline failing set includes the audit's live incidents: the six Russian texts, the
three classifier holes, RU dialog → `waiting_owner`, `decide` never-submit-on-dialog,
`run_once` production-contract tail, actuator dialog guard, autopilot skip. Import
origin: `IMPORT-FROM: …/wt-b4153fa/core/agent_control.py` (trap avoided).

## Checkpoint

- HEAD: f9c06ee (branch owner-os/control-plane-v2); tree clean before and after review;
  review worktree removed (`git worktree list` back to the two pre-existing July trees).
- Tests run: tests/test_dialog_failclosed_ru_en.py on HEAD (31 passed) + 5 baseline/
  attribution runs in the throwaway worktree + in-process probes (read-only).
- State: no service touched (no restart/reload), no env/allowlist/unit change, autopilot
  untouched, zero pane contact, nothing pushed. This report is the only new file.
- Remains open (owner decision): M1 pattern breadth, M2 empty-tail skip in run_once;
  both fail-safe-adjacent residuals, neither blocks acceptance. Deploy of f9c06ee to
  the live watcher is still the explicit owner restart gate.
