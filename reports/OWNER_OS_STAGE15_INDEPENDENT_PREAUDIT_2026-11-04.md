# Stage 15: independent Sonnet adversarial PRE-AUDIT, 2026-11-04

An automated instruction was received through the Owner OS API. It is not owner approval.

**This is a Claude Sonnet 5.5 PRE-AUDIT. It is NOT the Fable audit. Fable final audit: PENDING. PROD: NO-GO.**
Reviewed: branch `owneros/offline-acceptance` @ `53e7a2b` (clean tree), covering `6269564`, `426861c`, `06313c6`.
No code, PROD, live DB, secret, paid API, agent or subagent was touched. Repros use `:memory:` DBs and a fake `claude` script, under the scratchpad.

**Verdict for the next DEV stage (branch work only): GO, conditional on fixing F1-F3 first. Verdict for deploy/PROD: BLOCKED.**

## Fable mechanism
Not invoked. `agent_auth_mode.resolve` only allows `sonnet`, or `opus` with `risk=high`. A Fable launch via `tools/agent_create.py --model` is therefore refused (`model_not_allowed`).
The only route is a pane the tech lead starts by hand with the Fable model id. That also needs the model-routing task 209 rule ("hardest only") and a billing check.
Not verified here; it is an open question for the tech lead.

## Findings
| # | sev | conf | where | summary |
|---|---|---|---|---|
| F1 | HIGH | high (repro) | `core/fullcycle/engine.py:355-372` (decide), `:301-306` (ack) | decision commit and audit append are **separate transactions** |
| F2 | HIGH | high (repro) | `core/ai_planner.py:312` | `claude -p` planner spawns with the full daemon env, outside auth-mode control |
| F3 | HIGH | medium (design) | `core/agent_auth_mode.py:187-250`, `core/agent_control.py:2348-2362`, `config/agent_auth_modes.yaml` | API opt-in, config and ledger are not protected from the agents they constrain |
| F4 | MED | high (repro) | `core/agent_auth_mode.py:249-250` | recovery key-strip is bypassed for any non-trivial `resume_shape` |
| F5 | MED | high | `api/v1.py:40-47` | HMAC signs only the timestamp, so a captured signature replays on any endpoint or body inside the window |
| F6 | MED | high | `tests/test_offline_acceptance.py:49-65`, scenario F | harness isolation guard is detective, and the credential check is vacuous |
| F7 | LOW | medium | `agent_auth_mode.py:60-78` | strip list omits `ANTHROPIC_BASE_URL` and the Bedrock/Vertex switches |

### F1 decision/audit atomicity (HIGH)
`submit_decision` runs `UPDATE fc_review … decided` and `conn.commit()`, then `_audit(... "accepted")` in its own `BEGIN IMMEDIATE`. A crash or error between them leaves a decided review with no audit row.
The "spent key" test (D-2) reads only `fc_decision_audit`, so after a reopen the old key is no longer spent and can decide the reopened review again. `ack_review` has the same shape.
- **Repro:** `python3 -I <scratchpad>/repro_audit_gap.py` (`/tmp/claude-0/-opt-owneros-autonomy/a9ddcba5-72fc-4084-a217-d18ade6c5a37/scratchpad/`). `_audit` is made to raise on the "accepted" outcome. Output: `review: ('decided','approve','k1')`, `audit rows: 0`.
- **Fix:** write the review update and the audit row in one `BEGIN IMMEDIATE` transaction. `_audit` takes an `in_tx=True` mode and does not commit itself. Add a fault-injection test. Make the spent-key test also consult `fc_review` history.
- **Residual:** the chain is unkeyed sha256. Anyone with DB write can drop the triggers and rebuild it. Fix: keep a periodic head hash in a root-only file or an external sink, and sign it.

### F2 planner subprocess inherits credentials (HIGH, conditional)
`_invoke_cli` calls `subprocess.Popen(cmd, …)` with no `env=`. If the daemon env holds `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_CUSTOM_HEADERS`, every planner and smoke `claude -p` call runs on API credits. No cap, ledger or opt-in applies.
The handoff's question "can a subscription session inherit a credential?" is answered YES for this path.
- **Repro:** `python3 -I <scratchpad>/repro_planner_env.py <scratchpad>/fakeclaude.sh` prints `KEY_SEEN=sk-FAKE-TEST`.
- **Not verified:** whether the live daemon env actually holds such a variable. Checking it means reading live process environments, which is out of scope.
- **Fix:** pass `env={k: v for k, v in os.environ.items() if k not in API_ENV_VARS}` (a shared helper in `agent_auth_mode`). Audit `native_sessions.py:52` (`claude agents --json`) the same way. Add a test that fails if any `claude` spawn site lacks the strip.

### F3 spend gate enforceable by the thing it constrains (HIGH, design)
Panes run as root on the same host. `tools/agent_create.py --auth-mode api` needs no `owner_proof`, only a YAML entry plus ledger caps.
The YAML (`enabled`, `agents`, `limits`) and the `agent_auth_launch` ledger (in `agent_control.db`) are ordinary root-writable files and rows. A compromised or confused agent can set `enabled: true`, add its own entry, delete the ledger rows to reset the caps, then launch. Nothing in the code prevents this. `launcher` is also read from the same file; `_launcher_problem` checks the file but not its parent directory.
- **Fix:**
  1. Require a valid `owner_proof` (already plumbed through `agent_resume` for the creation gate) whenever `mode == api`.
  2. Bind the proof to `{session, project_dir, model}` with a short expiry and a single-use nonce.
  3. Keep the cap counters in the root-owned launcher or in an append-only, hash-chained table.
- **Test:** a launch with a valid YAML opt-in but no proof must be refused.
- **Mitigation owned by the owner:** the Anthropic workspace spend limit. The code cannot replace it.

### F4 recovery strip bypass (MED)
`recovery_command` strips credentials only when the shape starts with `claude`. Shapes like `cd /x && claude --resume …`, `FOO=1 claude …`, `/root/.local/bin/claude …` and `bash -lc '…'` pass through unchanged.
Today's `config/managed_sessions.yaml` shapes all start with `claude`, so this is latent, not live.
- **Repro:** `python3 -I <scratchpad>/repro_recovery.py`.
- **Fix:** fail closed. Refuse any shape not matching `^claude(\s|$)`, or always wrap in `env -u … sh -c`.

### F5 HMAC without request binding (MED, pre-existing)
`expected = HMAC(token, timestamp)` signs neither method, path nor body. One captured header pair is a replayable bearer for the whole API for the replay window. It cannot decide reviews, because `_reviewer_principal` requires a client token, so impact is limited to other endpoints.
- **Fix:** sign `ts|method|path|sha256(body)`, and keep a nonce cache.

### F6 acceptance harness false-success risks (MED)
- The `sqlite3.connect` recorder asserts **after** the real connect, so a live-DB write has already happened by the time the test fails. It also misses connections opened by subprocesses, `ATTACH`, or non-Python code. Fix: make the wrapper raise before connecting.
- The `isolation` fixture deletes every `ANTHROPIC_*` variable before each test. The "no credential in any event" invariant therefore passes trivially. Scenario F asserts only the argv list, never that the spawned process lacks the variable. Fix: set a sentinel key and assert it is absent in a spawned fake `claude`.
- The LLM agent, ChatGPT page, reviewer judgment and clock are simulated, as already stated. The mutation check (5 of 6 fail when the shared token may act) supports the identity checks only.

### F7 strip list (LOW)
Not stripped: `ANTHROPIC_BASE_URL` (redirects traffic, and the subscription token, to another host), `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `AWS_*`, and `apiKeyHelper` or an `env` block in a settings file. Add them, or run with a clean allowlisted env.

## Question verdicts (handoff audit packet)
| question | verdict |
|---|---|
| Can any path start API mode without explicit request + opt-in + caps? | **DEFECT** (F3): in code every path needs all three; on the host an agent can fabricate the opt-in and reset the caps |
| Can a subscription session inherit a credential? | **DEFECT** (F2 for `-p` planner jobs; F4 for non-trivial recovery shapes; F7). The interactive `agent_resume` path is safe for the three listed variables |
| Can recovery revive an agent into API mode? | Not by itself: a recorded API session is refused. Bypass only via F3 (ledger tampering) |
| Is counting before the start correct under concurrent launches? | CONFIRMED-SAFE in code (`creation_guard` held around resolve, record and start; a failed start still counts). Residual: sessions started outside Owner OS are not counted (already stated) |
| Is the harness isolation guard sufficient? | NO (F6) |
| Fullcycle Q6 identity: can bearer, HMAC or `X-Runtime-Actor` authorize decide? | CONFIRMED-SAFE: only `auth_method == "client"` and a listed `api:client/<id>`; `FULLCYCLE_REVIEWERS` defaults to empty (fail closed) |
| Fullcycle Q7 audit | **DEFECT** (F1). Chain logic itself is correct; concurrency serialised by `BEGIN IMMEDIATE` |
| Q1-Q5 (reopen, hold, idle window, rotation, untrusted text) | **NOT RE-VERIFIED in depth.** Fence neutralisation (`engine._neutralize`) read and looks sound; the rest is left to Fable |

## Commands run (worktree only)
- `python3 -m pytest -q -p no:cacheprovider tests/test_agent_auth_mode.py tests/test_offline_acceptance.py` → 39 passed (the green suite does not cover F1-F7).
- The three repro scripts above.
- The full 4398-test suite was not re-run. The reported result was not independently reproduced.

## Residual risks
Agents run as root, so every authentication and audit control is advisory against a malicious agent (F1 residual, F3). No live E2E exists. The migration was never tried against a copy of the live DB.
