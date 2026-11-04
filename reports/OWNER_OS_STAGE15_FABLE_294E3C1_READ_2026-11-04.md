# Stage 15 Fable diff read of 294e3c1, 2026-11-04

Short read of one commit, requested by the owner in this session, not a full round. Subagent through the `Agent` tool with `model: "fable"`; model self-reported Claude Fable 5.1, no independent proof; billing not checked.
**Verdict: sound with notes. Sandbox condition LOW-1 met. PROD not granted, PROD: NO-GO.**

Filed by the Opus 5.5 session, condensed. Evidence: focused file 123 passed; `ddc75f3` against `294e3c1` over about 160 strings, every change old-wrong to new-right except Finding 1. Repo untouched.

**Finding 1, CONFIRMED, regression, not a sandbox blocker.** `core/fullcycle/hosts.py:97,109`. Deleting the fd glues a background `&` to the redirect: `x &2>/dev/null claude -p y` becomes `x &>/dev/null claude -p y`, read as a redirect, while bash runs `claude`. Same for `x&2>&1 claude`, `x &1>f claude`. The miss falls to the denylist. Fix: `_REDIRECT_FD.sub(" ", ...)`.

**Note, PLAUSIBLE, minor.** The regex runs on quoted text: `echo '|2>|' claude` gives a false positive. Cured by the same fix. Pre-existing and unchanged: `x;>| claude` is seen although `>|` is a redirect.

**OSError handling correct.** Only `os.close` in `_anchor_lock` could give `api_anchor_write_failed` after a spend, which fails closed. An `OSError` from `verify_proof` or a key read is mislabelled with that reason, nothing more.

**Handoff:** accurate about this commit.

**Not checked:** full suite, mutants, `SshHost`, the `_CLAUDE_WORD` fallback, earlier commits.
