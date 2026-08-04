# Commander Watcher — dialog recognition + continuation gate, FAIL-CLOSED (RU/EN)

Date: 2026-08-04 · Branch: `owner-os/control-plane-v2` · Base: `b4153fa` (suite 1092)
Scope: SAFE INTERNAL — code + tests + this report only. No service restart/reload
(PID 4063628, started 00:29:37 CEST, untouched), no allowlist/unit/drop-in/env change,
autopilot stays dormant, ZERO pane contact with any agent, no push.

## 1. What was vulnerable (verified on b4153fa before fixing)

All three findings from the 2026-08-03 audit (§3.1, §8) were re-verified live with
probes against the unmodified code:

1. **`is_safe_continuation` denylist-only + English-only**
   (`core/agent_continuation_watchdog.py`). All SIX owner-typed Russian texts the
   watchdog live auto-Entered on arbitrage2-opus:0.0 (21:12–21:38Z, `cw_step`
   ledger) returned `True` — including «удали старый scratchpad», a delete
   instruction the English denylist cannot see. Probe output: `[True ×6]`.
2. **`classify_action` = prefix + denylist miss ⇒ autonomous_safe**
   (`core/control_plane/actuator.py`). Verified: `"proceed to send 5 BTC to
   wallet X"` → `autonomous_safe`; `"resume and promote staging traffic to
   production"` → `autonomous_safe`; `"continue and delete everything"` →
   `autonomous_safe`.
3. **Dialog recognition English-only** (`core/agent_control.py`
   `_STATE_WAIT_OWNER_RE`). `classify_state` returned `idle` for
   «Точно удалить все данные?\nПродолжить? (да/нет)», for a numbered-only menu
   (`❯ 1. Yes / 2. No` without a "do you want" line), for
   "Do you trust the files in this folder?", for "Enter passphrase for key …:",
   for «Введите пароль:» and for "Confirm deployment to production? [y/N]".
   Consequence probed: `decide()` on the Russian-dialog pane with pending «да»
   returned **`action=submit`** — one Enter away from ANSWERING a permission
   dialog. Additionally, in production `run_once` never passed the real pane tail
   into `decide` (the inventory has no `_tail` key), so even the English
   tail-based guards evaluated against `""`.

## 2. What changed (file:line, post-fix)

**`core/agent_control.py`**
- 704–741: `_ANSI_ANY_RE` (full ANSI escapes, not just SGR), `_BOX_NOISE_RE`
  (box-drawing/block glyphs), `_DIALOG_RE` — bilingual RU/EN dialog patterns:
  do-you-want-to-proceed/trust variants, numbered option lists (`1. Yes … 2.`,
  `1. Да`), menu cursor `❯ N.`, `(y/n)`/`(да/нет)`/`[д/н]`, are-you-sure,
  press-enter-to-continue, type-X-to-confirm, credential/secret prompts
  (password/passphrase/token/api-key/otp/2fa, «введите пароль/токен/ключ/
  секрет/код», «пароль:»), deploy/publish confirmations, «разрешить?»,
  «продолжить?», «подтвердите/подтверждени», «вы уверены», «точно удалить…»,
  «нажмите enter/ввод», «доверяете».
- 743–762: `_dialog_scan_text` (strip ANSI + box frames + NBSP, collapse
  whitespace so options spanning frame lines still match), `dialog_signature`,
  `looks_like_dialog` — reuses the existing styled-capture/status machinery
  rather than a new capture path.
- 812–818: `classify_state` step 2 now ORs `_DIALOG_RE` over the normalised tail
  with the legacy `_STATE_WAIT_OWNER_RE` → these panes classify `waiting_owner`
  (watchdog skips, autopilot excludes, rotation refuses — all pre-existing
  fail-closed consumers of that state).

**`core/agent_continuation_watchdog.py`**
- 71–96: `_FORBIDDEN_RE` extended — EN: bare `delete`, `promote/promotion`,
  `send`, `wallet`, `btc/eth/bitcoin/crypto`, `buy/sell/trade/trading`,
  `wipe/erase/destroy/restart`; RU stems: `удал`, `снес(и|ти)/снос`,
  `стереть/сотр`, `очист`, `перезапус/рестарт`, `деплой`, `публик`, `отправ`,
  `перевед/перевест/перевод/перечисл`, `купи`, `прода(й|ть|ж)`, `ключ/парол/
  секрет/токен`, `продвин/промоут/промот`, `залей/залить/форс`.
- 248–306: **inverted default.** `is_safe_continuation` is now
  ALLOWLIST-STRUCTURAL: refuse on denylist hit; accept exact bare
  `/clear|/compact`; refuse ANY non-printable-ASCII script
  (`_EVALUABLE_TEXT_RE` — Cyrillic/CJK/… cannot be evaluated by the denylist);
  refuse bare dialog answers (`_DIALOG_ANSWER_RE`: `1`, `y`, `yes`, `да`,
  `нет`, …); refuse any digits; require a documented continuation prefix
  (`_SAFE_STEP_PREFIX_RE`) AND every word ∈ `_SAFE_STEP_VOCAB` (closed benign
  vocabulary); length cap 300. Unrecognised ⇒ refuse.
- 308–316: `pane_shows_dialog` — fail-closed wrapper (detector unavailable ⇒
  treated as a dialog, never as clear).
- 363–368: `decide` gains the dialog gate — a visible RU/EN dialog on the pane ⇒
  `skip: dialog_open_never_auto_answer`, before any submit/deliver, even when
  state read `idle`.
- 588–599: `run_once` backfills the REAL pane tail into the agent dict when the
  inventory lacks `_tail` (production contract) — the dialog gate and the
  thinking-marker guard now see the live tail, not `""`.

**`core/control_plane/actuator.py`**
- 55–91: comment corrected (the audit-flagged "NARROW by design" overstatement)
  + `_RESUME_TEMPLATE_RE`: the context-rotation resume message is recognised
  STRUCTURALLY — exact fixed template, path slot restricted to
  `[A-Za-z0-9._/-]+`, and the embedded next-command slot must itself pass the
  fail-closed continuation gate (95–101). Free-form text never matches; a
  template with an unrecognised step is owner-gated.
- 204–219: `actuate` step **3b2 DIALOG GUARD** — after the false-idle snapshot,
  refuse (`reason: dialog_open`, event `action_deferred_dialog_open`) when
  `dialog_signature` matches the pane tail OR the snapshot state is
  `waiting_owner`; detector failure ⇒ refuse. No pane contact happens on this
  path.

**`core/commander_autopilot.py`**
- 151–159: `evaluate` gains `skip_dialog_open` — a dialog-showing pane is never
  a poke candidate even when its state reads idle/waiting_input (the Actuator
  re-checks at delivery; this makes the recorded decision honest too).

**`tests/test_dialog_failclosed_ru_en.py`** — 31 new tests (below).

## 3. Fail-closed decision table (RU/EN × input class)

| Input class | Example | `is_safe_continuation` | `classify_action` | watchdog `decide` | actuator `actuate` |
|---|---|---|---|---|---|
| EN recognised safe step | "continue with the next safe step", every registry `next_step` | **True** | `autonomous_safe` | submit/deliver (unchanged) | delivers + verifies |
| Bare context cmd | `/clear`, `/compact` | True | `autonomous_safe` | submit | delivers (rotation gates still apply) |
| Rotation resume template | `_resume_text(path, registry step)` | n/a (template) | `autonomous_safe` iff embedded step passes the gate | n/a | delivers |
| EN destructive/live (incl. the 3 live probes) | "proceed to send 5 BTC to wallet X", "resume and promote staging traffic to production", "continue and delete everything" | False | **`prohibited`** | blocker → owner | blocked + owner gate |
| RU destructive/live/credential | «удали старый scratchpad», «задеплой на прод», «покажи ключ и пароль» | False | **`prohibited`** | blocker → owner | blocked + owner gate |
| RU benign / any unknown script | «продолжи со следующим безопасным шагом», 继续下一步 | **False (script gate)** | `owner_approval_required` | blocker → owner | blocked + owner gate |
| EN free prose, prefix but unrecognised shape | "resume the migration of the cluster" | False | `owner_approval_required` | blocker → owner | blocked + owner gate |
| Digits/amounts anywhere | "proceed to step 3" | False | `owner_approval_required` | blocker → owner | blocked + owner gate |
| Bare dialog answer | "1", "y", "yes", «да», «нет» | False | `owner_approval_required` | blocker → owner | blocked + owner gate |
| Pane SHOWS dialog (RU or EN, styled/boxed) | «Продолжить? (да/нет)», `❯ 1. Yes / 2. No`, trust-folder, passphrase, deploy-confirm | n/a | n/a | **skip `dialog_open_never_auto_answer`** (before any submit) | **refused `dialog_open`**, zero keystrokes |
| Pane state `waiting_owner` | any | n/a | n/a | skip (supervisor's job, unchanged) | refused `dialog_open` |
| Detector unavailable (import/regex failure) | any | n/a | n/a | skip (fail-closed) | refused (fail-closed) |

Governing rule everywhere: UNSURE ⇒ REFUSE. Over-refusal accepted by design.

## 4. Tests + pre-fix failure proof

New file `tests/test_dialog_failclosed_ru_en.py` (31 tests). Proof method: fresh
`git worktree` at `b4153fa`, new test file copied in, and — because
`tests/conftest.py:16` hardcodes `sys.path.insert(0, "/root/ai-dev-runtime")` —
the conftest was **repointed to the worktree** (verified: `cw.__file__` resolved
inside the worktree and the pre-fix probe `is_safe_continuation("удали старый
scratchpad") == True` reproduced there). Result on b4153fa: **23 FAILED, 8 passed**.

FAILED on pre-fix (each one is a real behaviour change):
`test_six_live_russian_texts_are_never_safe_continuations`,
`test_russian_delete_instruction_is_prohibited_not_merely_gated`,
`test_russian_destructive_live_credential_verbs_are_prohibited`,
`test_any_unevaluable_script_is_fail_closed_even_when_benign`,
`test_live_verified_classifier_holes_are_closed`,
`test_prefix_plus_denylist_miss_is_no_longer_sufficient`,
`test_digits_and_amounts_are_never_part_of_a_safe_step`,
`test_dialog_answer_tokens_are_never_safe_continuations`,
`test_arbitrary_english_owner_prose_requires_owner_approval`,
`test_resume_template_with_unsafe_embedded_step_is_never_safe`,
`test_russian_dialogs_are_detected_and_classified_waiting_owner`,
`test_english_dialog_shapes_beyond_the_legacy_regex_are_detected`,
`test_dialog_detection_survives_ansi_styling_and_box_frames`,
`test_non_dialog_panes_are_not_flagged`,
`test_decide_never_submits_on_a_russian_dialog_pane`,
`test_decide_never_delivers_proactively_onto_a_dialog_pane`,
`test_decide_blocks_each_of_the_six_live_russian_pending_texts`,
`test_decide_fail_closed_when_dialog_detector_unavailable`,
`test_run_once_never_enters_on_a_dialog_pane_production_contract`,
`test_actuator_refuses_to_act_on_a_dialog_pane`,
`test_actuator_refuses_waiting_owner_state_even_without_dialog_text`,
`test_actuator_dialog_guard_fail_closed_when_detector_unavailable`,
`test_autopilot_skips_a_dialog_pane_even_when_state_reads_idle`.

Passed on pre-fix (by design — anti-overcorrection guards + one
defense-in-depth pin):
`test_six_live_russian_texts_are_never_autonomous_safe` (the six lack an
English continuation prefix, so `classify_action` already owner-gated them
pre-fix — the WATCHDOG path was the live hole, covered by the failing tests),
`test_autopilot_still_pokes_a_genuinely_idle_pane_with_open_work`,
`test_documented_safe_continuations_still_classify_safe`,
`test_every_registry_next_step_still_safe_and_recognised`,
`test_decide_still_submits_the_documented_safe_step`,
`test_actuator_still_delivers_a_safe_step_to_a_clean_idle_canary`,
`test_dim_recall_ghost_and_menu_selection_still_not_pending_input`,
`test_active_working_pane_still_working_not_waiting_owner`.

Existing guards re-proven, not weakened: pending-input guard, false-idle guard,
policy recompute, canary confinement, rotation gates, delivered-poke ledger fix,
dim-ghost/styled-capture, CI registry invariant
(`test_every_registry_next_step_is_autonomous_safe`) — all still green in the
full suite.

## 5. Full suite

- Pre-change baseline (this session, b4153fa): **1092 passed**.
- Post-change: **1123 passed** (1092 + 31 new), 0 failed, 4 pre-existing
  warnings. Two production texts required allowlist recognition during
  integration: the test-fixture step "…write a progress report." (word `write`
  added to the vocabulary) and the rotation resume template (recognised
  structurally with a gated step slot) — both are covered by tests that pin the
  gate cannot be abused (`test_resume_template_with_unsafe_embedded_step_is_never_safe`).

## 6. Limitations

- The safe class is a CLOSED vocabulary: an owner-configured `safe_continuation`
  or registry `next_step` using words outside `_SAFE_STEP_VOCAB` will be
  owner-gated (surfaced, never silently sent). This is the intended trade —
  e.g. the live proactive paste "Continue with the fault-matrix extension and
  replay harness" would now be refused. The CI registry invariant catches such
  an edit at commit time.
- The RU denylist is stem-based and over-broad by intent (e.g. `удал` also
  matches «удалённый»/remote) — irrelevant to safety because ALL non-ASCII text
  is refused by the script gate anyway; the stems only upgrade recognisably
  destructive Russian to `prohibited` for honest gate labelling.
- Dialog detection is pattern-based over the last ~1500 chars of a normalised
  tail: a dialog phrased in a third language, or quoting dialog-like text in
  ordinary output (e.g. an agent printing «вы уверены» in prose), will
  over-trigger `waiting_owner`/refusal — fail-closed direction, but can delay a
  legitimate poke until the pane text scrolls. A dialog rendered entirely as
  images/custom glyphs with no recognisable words would not match; the
  watchdog's other guards (waiting_owner state, pending-input, menu-selection
  filter) remain the backstop, and `approve_prompt` remains the only
  dialog-answering path (supervisor, classifier-gated, unchanged).
- The six live Russian texts are pinned as watchdog refusals (blocker → owner);
  the watchdog's pending-submit policy for arbitrary OWNER-typed English prose
  is now also refuse-by-default — an explicit owner decision could later relax
  this per-session, not in this change.
- `_EVALUABLE_TEXT_RE` refuses ANY non-ASCII character, including a stray
  typographic quote/ellipsis in otherwise-English text — over-refusal accepted.

## 7. What remains owner-gated (unchanged)

1. Live actuation of payment:0.0 / arbitrage2-opus:0.0 — never touched here.
2. `CONTROL_PLANE_CANARY_AGENTS` membership (systemd drop-in) — unchanged.
3. `COMMANDER_AUTOPILOT_ENABLED` — still dormant.
4. Deploying THIS change to the running service: the live process
   (PID 4063628, started 00:29:37 CEST) still runs the pre-fix code by mandate —
   **a service restart is required for these guards to take effect live**, and
   that restart is an explicit owner gate not crossed in this task.
5. Any relaxation of the refuse-by-default pending-submit policy.
