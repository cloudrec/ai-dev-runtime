"""Job kinds and outcomes.

Why this module exists
---------------------
The runtime previously had a single `status` field and exactly one notion of
success: "the repository test suite is green". That produced two false results:

1. An *operational* / *content* / *deployment* job that legitimately changes no
   code was still gated on `python3 -m pytest -q` for the whole repository, so
   it failed on defects it did not introduce (jobs OWNER-113..120).
2. A *fallback plan* — a Markdown document describing what should be done — was
   committed and reported as `completed`, indistinguishable from a real
   implementation (OWNER-111 "Build Release Controller").

A job therefore carries a `kind` (what sort of work it is) and an `outcome`
(what was actually achieved). `status` stays the lifecycle field; `outcome` is
the *result* field. `fallback_plan_only` is never a completed implementation.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

# --------------------------------------------------------------------------
# Kinds
# --------------------------------------------------------------------------

CODE_CHANGE = "code_change"
OPERATIONAL = "operational"
CONTENT_PRODUCTION = "content_production"
DEPLOYMENT = "deployment"
DATA_HANDOFF = "data_handoff"
CONTEXT_RESTORE = "context_restore"

KINDS = (CODE_CHANGE, OPERATIONAL, CONTENT_PRODUCTION, DEPLOYMENT, DATA_HANDOFF, CONTEXT_RESTORE)

#: Only `code_change` is gated on the repository test suite. Every other kind is
#: validated against its own task-specific checks (see `validation_kind_for`).
KINDS_REQUIRING_REPO_TESTS = frozenset({CODE_CHANGE})

# --------------------------------------------------------------------------
# Outcomes
# --------------------------------------------------------------------------

IMPLEMENTED = "implemented"
FALLBACK_PLAN_ONLY = "fallback_plan_only"
OPERATIONAL_COMPLETE = "operational_complete"
CONTENT_COMPLETE = "content_complete"
DEPLOYMENT_PREPARED = "deployment_prepared"
DATA_HANDOFF_COMPLETE = "data_handoff_complete"
CONTEXT_RESTORED = "context_restored"
FAILED = "failed"

OUTCOMES = (IMPLEMENTED, FALLBACK_PLAN_ONLY, OPERATIONAL_COMPLETE, CONTENT_COMPLETE,
            DEPLOYMENT_PREPARED, DATA_HANDOFF_COMPLETE, CONTEXT_RESTORED, FAILED)

#: Outcomes that represent a real, finished implementation of the requested
#: change. Deliberately excludes `fallback_plan_only`: a plan is not a change.
IMPLEMENTATION_OUTCOMES = frozenset({IMPLEMENTED})

# --------------------------------------------------------------------------
# Terminal statuses
# --------------------------------------------------------------------------
# `outcome` alone was not enough. Jobs 59/60/61 carried `outcome=fallback_plan_only`
# *and* `status=completed`, and every consumer that filtered on status — the
# poller, the notifier, the owner's job list — read them as done. A plan-only
# run therefore gets its own terminal status, so a truthful result no longer
# depends on a reader remembering to check a second field.
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"
STATUS_FALLBACK_PLAN_ONLY = "fallback_plan_only"

#: Statuses a plan-only run may legally end in. `completed` is not among them.
PLAN_ONLY_TERMINAL_STATUSES = frozenset({STATUS_FAILED, STATUS_BLOCKED, STATUS_FALLBACK_PLAN_ONLY})


def terminal_status_for(outcome: Optional[str], proposed: str = STATUS_COMPLETED) -> str:
    """The terminal status an outcome is allowed to end in.

    Fail-closed: a `fallback_plan_only` outcome can never resolve to `completed`,
    whatever the caller proposes. This is the single chokepoint the executor's
    `_finish` runs through, so a future code path cannot reintroduce the lie by
    passing "completed" directly.
    """
    if outcome == FALLBACK_PLAN_ONLY:
        return proposed if proposed in PLAN_ONLY_TERMINAL_STATUSES else STATUS_FALLBACK_PLAN_ONLY
    return proposed


def is_truthful_terminal(status: str, outcome: Optional[str]) -> bool:
    """Whether a (status, outcome) pair is self-consistent."""
    if outcome == FALLBACK_PLAN_ONLY:
        return status in PLAN_ONLY_TERMINAL_STATUSES
    return True

#: Outcomes that may feed a release/deployment workflow. A plan never can.
RELEASABLE_OUTCOMES = frozenset({IMPLEMENTED})

_SUCCESS_OUTCOME_BY_KIND = {
    CODE_CHANGE: IMPLEMENTED,
    OPERATIONAL: OPERATIONAL_COMPLETE,
    CONTENT_PRODUCTION: CONTENT_COMPLETE,
    DEPLOYMENT: DEPLOYMENT_PREPARED,
    DATA_HANDOFF: DATA_HANDOFF_COMPLETE,
    CONTEXT_RESTORE: CONTEXT_RESTORED,
}

# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

# Ordered: the first kind whose pattern matches the goal/instructions wins, so
# the more specific intents are tested before the generic ones.
_KIND_PATTERNS = (
    # A goal that *opens* with a code verb is a code change even when it talks
    # about other kinds of work ("Fix operational job test gating" changes code;
    # it does not run an operational batch).
    (CODE_CHANGE, re.compile(
        r"^\s*\[?[^\]]*\]?\s*(fix|implement|build|refactor|patch|migrate|rewrite|"
        r"add|remove|codify|harden)\b", re.I)),
    (CONTEXT_RESTORE, re.compile(
        r"\b(restore|recover|reconstruct)\b.{0,40}\b(contexts?|sessions?|handoffs?|states?)\b"
        r"|\bagent contexts?\b|\bcontext (restore|recovery)\b", re.I)),
    (DATA_HANDOFF, re.compile(
        r"\bhandoff\b|\bhand[-\s]?off\b|\bexport\b.{0,30}\b(data|list|batch)\b"
        r"|\bprepare\b.{0,30}\b(email|data)\b.{0,30}\bwithout sending\b", re.I)),
    (DEPLOYMENT, re.compile(
        r"\bdeploy(ment|ing)?\b|\brelease\b.{0,20}\b(to|on)\b.{0,20}\b(prod|production|vps|server)\b"
        r"|\brestart\b.{0,20}\bservice\b|\bgo[-\s]?live\b", re.I)),
    (CONTENT_PRODUCTION, re.compile(
        r"\b(content|social|post|posts|copywriting|article|newsletter|campaign)\b"
        r".{0,40}\b(batch|production|produce|generate|write|publish)\b"
        r"|\b(produce|generate|write)\b.{0,20}\b(content|posts?|social)\b", re.I)),
    (OPERATIONAL, re.compile(
        r"\brun\b.{0,30}\bbatch\b|\bworkstream\b|\baudit batch\b|\binventory\b|\breconcile\b"
        r"|\boperational\b|\bbackfill\b|\bcleanup\b", re.I)),
    (CODE_CHANGE, re.compile(
        r"\b(fix|implement|build|refactor|add|remove|migrate|patch|bug|test|code)\b", re.I)),
)


def classify(goal: str = "", instructions: str = "", explicit: Optional[str] = None) -> str:
    """Resolve a job's kind.

    An explicit, valid kind from the caller (Owner OS / API) always wins —
    classification from free text is only a fallback for legacy callers that do
    not yet send a kind. Defaults to `code_change`, the strictest gate, so an
    unrecognised job is never *under*-validated.
    """
    if explicit and explicit in KINDS:
        return explicit
    text = f"{goal or ''}\n{instructions or ''}"
    for kind, pattern in _KIND_PATTERNS:
        if pattern.search(text):
            return kind
    return CODE_CHANGE


def requires_repo_tests(kind: str) -> bool:
    """True only for kinds whose success is defined by the repository suite."""
    return kind in KINDS_REQUIRING_REPO_TESTS


def success_outcome_for(kind: str) -> str:
    return _SUCCESS_OUTCOME_BY_KIND.get(kind, IMPLEMENTED)


def is_implementation(outcome: str) -> bool:
    """A plan is not an implementation. Guards release/merge decisions."""
    return outcome in IMPLEMENTATION_OUTCOMES


def is_releasable(outcome: str) -> bool:
    return outcome in RELEASABLE_OUTCOMES


def requires_code_changes(kind: str) -> bool:
    """Whether producing no file changes is an error.

    For operational/content/deployment/handoff/context work, changing no code is
    a normal result — such a job must never fabricate a commit to look successful.
    """
    return kind == CODE_CHANGE


def allows_empty_commit(kind: str) -> bool:
    """No kind may create an empty/fictitious commit purely to appear successful."""
    return False


def validation_kind_for(kind: str) -> str:
    """Human-readable description of what counts as validation for this kind."""
    return {
        CODE_CHANGE: "repository test suite",
        OPERATIONAL: "task-specific operational checks (artifacts/records produced)",
        CONTENT_PRODUCTION: "task-specific content checks (assets produced, nothing published)",
        DEPLOYMENT: "task-specific deployment readiness checks (prepared, not deployed)",
        DATA_HANDOFF: "task-specific handoff checks (data prepared, nothing sent)",
        CONTEXT_RESTORE: "task-specific context checks (context restored/reported)",
    }.get(kind, "repository test suite")


def normalize_outcome(outcome: Optional[str]) -> Optional[str]:
    return outcome if outcome in OUTCOMES else None


def summarize(kind: str, outcome: str) -> str:
    """Short operator-facing phrase, e.g. for notifications."""
    if outcome == FALLBACK_PLAN_ONLY:
        return "plan only — NOT implemented"
    if outcome == FAILED:
        return "failed"
    return {
        IMPLEMENTED: "implemented",
        OPERATIONAL_COMPLETE: "operational work complete",
        CONTENT_COMPLETE: "content produced",
        DEPLOYMENT_PREPARED: "deployment prepared (not deployed)",
        DATA_HANDOFF_COMPLETE: "data handoff prepared (nothing sent)",
        CONTEXT_RESTORED: "agent context restored",
    }.get(outcome, outcome or "unknown")
