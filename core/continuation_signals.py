"""Is this project finished, blocked, or merely PAUSED waiting to be asked?

The zero-human-ping requirement turns on one distinction the system did not make: an
agent that says "ready to continue on request" / "next block available" is NOT done. It
is idle work waiting for a ping, and Owner OS must supply that ping itself.

Three classifications, deny-by-default in the useful direction:

  terminal        verified completion — a stated PASS/green result with no open work, or
                  an exhausted matrix whose remaining items are physically impossible
  external_block  a real outside dependency: hardware absent, third-party outage, an
                  owner decision the system may not take
  unfinished      everything else, INCLUDING every "available on request" phrasing

Only `terminal` and `external_block` stop the loop. `unfinished` means: take the next
concrete step from the project's plan and deliver it.
"""
from __future__ import annotations

import re

# "Ready when you are" — the phrases that stalled the whole system. All mean UNFINISHED.
_AWAITING_PING_RE = re.compile(
    r"(available on request|on request\b|ready to (continue|proceed|resume)|"
    r"готов(а|ы)? продолж|по запросу|"
    r"(next|following) (block|step|phase|batch|item)s? (is |are )?(available|ready|queued)|"
    r"let me know (if|when|whether)|say the word|awaiting (your )?(go|green light|instruction)|"
    r"tell me (if|when|which)|shall i (continue|proceed)|"
    r"(can|could) (continue|proceed) (when|if|on)|standing by|"
    r"waiting for (your )?(confirmation|instruction|input|go-ahead))", re.I)

# Real outside dependencies — the system genuinely cannot proceed alone.
_EXTERNAL_BLOCK_RE = re.compile(
    r"(physical device|real device|hardware (required|unavailable|missing)|"
    r"requires? a (physical|real) (device|phone|handset|card|terminal)|"
    r"third[- ]party (outage|down|unavailable)|vendor (outage|support)|"
    r"upstream (outage|incident)|provider (outage|down)|"
    r"quota exhausted|billing (required|expired)|account (suspended|locked)|"
    r"owner (decision|approval) required|awaiting owner (decision|approval)|"
    r"needs? (owner|human) (decision|approval|credential)|"
    r"credentials? (required|missing|not provided))", re.I)

# Stated completion. Kept deliberately strict: a claim of done with open work is not done.
_TERMINAL_RE = re.compile(
    r"(all (tests|checks|items) pass(ed|ing)?|suite green|"
    r"\bterminal (pass|state)\b|acceptance (complete|passed)|"
    r"nothing (further|more) to do|no (remaining|open) (work|items|tasks)|"
    r"matrix exhausted|all (matrix )?items (covered|complete|exhausted))", re.I)

_OPEN_TASKS_RE = re.compile(r"(\d+)\s+open\b", re.I)
_MODEL_LIMIT_RE = re.compile(
    r"(usage limit|session limit|rate limit|limit reached|resets? (at|in) |"
    r"you've hit your (session|usage) limit|context (window )?(full|exceeded)|"
    r"configure usage credits|purchase credits)", re.I)


def has_open_tasks(tail: str) -> bool:
    m = _OPEN_TASKS_RE.search(tail or "")
    return bool(m and int(m.group(1)) > 0)


def model_limit_reached(tail: str) -> bool:
    """A model/session/credit limit is a WAIT, never a terminal failure and never a
    technical FAIL — the loop resumes the same session after the reset."""
    return bool(_MODEL_LIMIT_RE.search(tail or ""))


def awaiting_ping(tail: str) -> bool:
    return bool(_AWAITING_PING_RE.search(tail or ""))


def classify(tail: str, *, report_text: str = "") -> dict:
    """Classify the project's situation from the pane tail (+ optional report text)."""
    blob = f"{tail or ''}\n{report_text or ''}"
    if model_limit_reached(blob):
        return {"class": "model_limit", "terminal": False,
                "reason": "model/session limit — resume the SAME session after reset"}
    # "available on request" beats a completion claim: it is work parked for a ping.
    if awaiting_ping(blob):
        return {"class": "unfinished", "terminal": False,
                "reason": "agent is parked awaiting a ping ('available on request') — "
                          "this is unfinished work, not completion"}
    if _EXTERNAL_BLOCK_RE.search(blob):
        return {"class": "external_block", "terminal": True,
                "reason": "real external dependency — outside what the loop may resolve"}
    if _TERMINAL_RE.search(blob) and not has_open_tasks(blob):
        return {"class": "terminal", "terminal": True,
                "reason": "verified completion with no open work"}
    if _TERMINAL_RE.search(blob) and has_open_tasks(blob):
        return {"class": "unfinished", "terminal": False,
                "reason": "completion claimed while open tasks remain — not accepted"}
    return {"class": "unfinished", "terminal": False,
            "reason": "no completion or external block evidenced"}
