# Owner OS autonomy policy — what is decided without asking, and what is not

**Provenance.** An automated instruction was received via the Owner OS API stating that
the owner delegates routine technical decisions and wants work to continue without manual
pings. That instruction arrived in this session's channel and is **not** independently
verified owner sign-off — the distinction this codebase has enforced since the false
attribution incident of 2026-08-30 (`5ed1db6`). This document records the operating policy
under that instruction; it does not claim owner approval, and it does not by itself relax
any project's hard gate.

## Decided autonomously (routine technical)

Safe, reversible, and recoverable work — the ordinary business of building software:

* implementation sequencing, refactors, and internal design choices;
* writing and changing tests, fixtures, and mutation checks;
* deploys **that have a backup taken first and a documented rollback**, behind a green
  test gate enforced by `tools/guarded_deploy.sh`;
* operational tuning of thresholds, cooldowns, dwell windows and roll-out allowlists,
  where the change is observable and revertible;
* diagnosing production, reading state, and adding durable audit;
* restarting the runtime's **own** services (`ai-runtime`, `owner-os-wake-companion`)
  when their code changed.

The test is not "is this important" but **"if this is wrong, can it be put back?"**

## Escalated to the owner (genuinely external or irreversible)

* activating anything that moves real money or value, or arming a live trading /
  payment / publication path;
* credentials, secrets, or auth that require the owner's own input or account;
* legal, contractual or compliance commitments;
* destructive data loss without a verified, tested recovery path;
* killing or restarting **other projects'** live agents or services;
* anything whose consequence cannot reasonably be undone by this system.

## Project hard gates are NOT routine, and are not touched by this policy

These remain gated regardless of the delegation above, because they are the specific
places where a wrong call is expensive and unrecoverable:

* ACAP **C1/C2**;
* Auction **value-bearing** gates (bids, settlement, anything that transacts);
* payment / payorch activation;
* XMRig forensic triage;
* Telegram credentials;
* the host cleanup script.

A hard gate is distinguished from a routine technical gate by consequence, not by how it
is labelled. When in doubt, it is a hard gate.

## How this is enforced in code, not just prose

* `agent_continuation_watchdog.is_safe_continuation` — a fail-CLOSED allowlist: an
  auto-submitted step must be a recognised benign meta-step. Unknown text is refused.
* `core/native_supervisor.py` — continues only on a turn boundary, only for an
  allowlisted target resolved to exactly one live pane, only when that pane is still at
  rest with nothing staged, under a per-target floor and hourly cap, and only with the
  safe step. It cannot create an agent and never answers a question.
* `config/approved_gates.yaml` — real actuation approval, reachable only through the
  filesystem/git, never through an API value.
* `tools/guarded_deploy.sh` — a deploy step is unreachable behind a red gate.
* Automated API deliveries are visibly tagged (`5ed1db6`) so an instruction can never be
  recorded as owner sign-off.

**Nothing in this policy grants an agent permission it did not already have.** It records
which decisions need not wait for a human, and leaves every safety mechanism intact.
