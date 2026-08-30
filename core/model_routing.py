"""Tier policy for Night Shift model use.

The routing MECHANISM already exists — `/opt/seo/backend/services/dispatcher.py` carries the
per-model cost registry, tier metadata and risk-fit scoring. This module is the POLICY that
sits on top: which tier a task is allowed to use, when it may escalate, and what it may spend.
It deliberately does not re-implement model selection, and it does not modify the dispatcher.

The rule that saves the most money is tier 0: polling, state reduction, dedupe, tests and
routine checks use no model at all. An executive that spends tokens to prove it is alive is
burning money to tell you something a timestamp already says.

Order of preference is always the LOWEST tier that can do the job safely. Escalation is
allowed, but never silent — it must carry a recorded reason, because "we used the expensive
model" with no explanation is how a budget disappears without anyone being able to audit it.
"""
from __future__ import annotations

import os
from typing import Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

TIER_NONE, TIER_LOCAL, TIER_CHEAP, TIER_SONNET, TIER_OPUS = 0, 1, 2, 3, 4

TIER_NAMES = {
    TIER_NONE: "deterministic (no model)",
    TIER_LOCAL: "local/free/lowest-cost",
    TIER_CHEAP: "inexpensive capable",
    TIER_SONNET: "sonnet-class",
    TIER_OPUS: "opus-class",
}

# The minimum tier each class of work is ALLOWED to start at. Anything absent defaults to
# tier 1, never higher: an unknown task is not an excuse to reach for the expensive model.
TASK_CLASS_TIER = {
    "poll": TIER_NONE, "state_reduce": TIER_NONE, "dedupe": TIER_NONE,
    "test": TIER_NONE, "routine_check": TIER_NONE, "health": TIER_NONE,
    "classify": TIER_LOCAL, "extract": TIER_LOCAL, "summarize": TIER_LOCAL,
    "idea_screen": TIER_LOCAL,
    "research": TIER_CHEAP, "spec": TIER_CHEAP, "code": TIER_CHEAP,
    "complex_code": TIER_SONNET, "review": TIER_SONNET,
    "architecture": TIER_OPUS, "critical_audit": TIER_OPUS,
}

DAILY_BUDGET_USD = float(os.getenv("NIGHT_SHIFT_DAILY_BUDGET_USD", "5.0"))
PER_TASK_CEILING_USD = float(os.getenv("NIGHT_SHIFT_TASK_CEILING_USD", "0.50"))
KILL_SWITCH = os.getenv("NIGHT_SHIFT_MODEL_KILL_SWITCH", "0") not in ("0", "", "false", "no")
# Identical work repeated this many times is a loop, not progress.
LOOP_THRESHOLD = int(os.getenv("NIGHT_SHIFT_LOOP_THRESHOLD", "3"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ns_spend (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, at TEXT, day TEXT, project TEXT, task_class TEXT, tier INTEGER,
    model TEXT, usd REAL, tokens_in INTEGER, tokens_out INTEGER, fingerprint TEXT,
    escalation_reason TEXT, artefact TEXT
)
"""


def _conn(conn=None):
    conn, own = _c(conn)
    conn.execute(_SCHEMA)
    return conn, own


def _day(now: Optional[float] = None) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(
        now if now is not None else now_ts(), datetime.timezone.utc).strftime("%Y-%m-%d")


def spent_today(conn=None, now: Optional[float] = None) -> float:
    conn, own = _conn(conn)
    try:
        r = conn.execute("SELECT COALESCE(SUM(usd),0) FROM ns_spend WHERE day=?",
                         (_day(now),)).fetchone()
        return float(r[0] or 0.0)
    finally:
        if own:
            conn.close()


def loop_count(fingerprint: str, conn=None, now: Optional[float] = None) -> int:
    """How many times identical work has already been paid for today."""
    conn, own = _conn(conn)
    try:
        r = conn.execute("SELECT COUNT(*) FROM ns_spend WHERE day=? AND fingerprint=?",
                         (_day(now), fingerprint)).fetchone()
        return int(r[0] or 0)
    finally:
        if own:
            conn.close()


def route(task_class: str, *, escalate_to: Optional[int] = None,
          escalation_reason: str = "", estimated_usd: float = 0.0,
          fingerprint: str = "", conn=None, now: Optional[float] = None) -> dict:
    """Decide the tier and whether the work may run at all.

    Returns {"allow", "tier", "tier_name", "reason", ...}. Refusals are explicit so the
    caller records WHY nothing was dispatched instead of silently doing nothing.
    """
    base = TASK_CLASS_TIER.get(task_class, TIER_LOCAL)

    if KILL_SWITCH and base > TIER_NONE:
        return {"allow": False, "tier": base, "tier_name": TIER_NAMES[base],
                "reason": "model_kill_switch_engaged"}

    # Tier 0 is free and deterministic: no budget, no loop and no kill switch applies,
    # because refusing to check health for cost reasons would be its own failure.
    if base == TIER_NONE:
        return {"allow": True, "tier": TIER_NONE, "tier_name": TIER_NAMES[TIER_NONE],
                "reason": "deterministic_no_model", "estimated_usd": 0.0}

    tier = base
    reason = "lowest_tier_for_task_class"
    if escalate_to is not None and escalate_to > base:
        if not escalation_reason.strip():
            # Escalation without a reason is exactly how a budget vanishes unauditably.
            return {"allow": False, "tier": base, "tier_name": TIER_NAMES[base],
                    "reason": "escalation_requires_recorded_reason"}
        tier = min(int(escalate_to), TIER_OPUS)
        reason = f"escalated: {escalation_reason.strip()[:160]}"

    if estimated_usd > PER_TASK_CEILING_USD:
        return {"allow": False, "tier": tier, "tier_name": TIER_NAMES[tier],
                "reason": f"per_task_ceiling_exceeded:{estimated_usd:.4f}>"
                          f"{PER_TASK_CEILING_USD:.4f}"}

    already = spent_today(conn=conn, now=now)
    if already + estimated_usd > DAILY_BUDGET_USD:
        return {"allow": False, "tier": tier, "tier_name": TIER_NAMES[tier],
                "reason": f"daily_budget_exhausted:{already:.4f}+{estimated_usd:.4f}>"
                          f"{DAILY_BUDGET_USD:.2f}", "spent_today": already}

    if fingerprint and loop_count(fingerprint, conn=conn, now=now) >= LOOP_THRESHOLD:
        return {"allow": False, "tier": tier, "tier_name": TIER_NAMES[tier],
                "reason": "loop_detected_identical_work_repeated"}

    return {"allow": True, "tier": tier, "tier_name": TIER_NAMES[tier], "reason": reason,
            "estimated_usd": estimated_usd, "spent_today": already}


def record_spend(*, project: str, task_class: str, tier: int, model: str, usd: float,
                 tokens_in: int = 0, tokens_out: int = 0, fingerprint: str = "",
                 escalation_reason: str = "", artefact: str = "", conn=None,
                 now: Optional[float] = None) -> None:
    """Durable spend, attributed. Without the project and the artefact there is no way to
    answer the only question that matters: what did this money actually produce?"""
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        conn.execute(
            "INSERT INTO ns_spend (ts,at,day,project,task_class,tier,model,usd,tokens_in,"
            "tokens_out,fingerprint,escalation_reason,artefact) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, now_iso(), _day(now), project, task_class, int(tier), model, float(usd),
             int(tokens_in), int(tokens_out), fingerprint, escalation_reason[:200],
             artefact[:300]))
        conn.commit()
    finally:
        if own:
            conn.close()


def cost_report(conn=None, now: Optional[float] = None) -> dict:
    """Cost by project and by model, plus artefacts per dollar — the acceptance metric."""
    conn, own = _conn(conn)
    try:
        day = _day(now)
        by_project = {r[0] or "-": round(float(r[1]), 6) for r in conn.execute(
            "SELECT project, SUM(usd) FROM ns_spend WHERE day=? GROUP BY project", (day,))}
        by_model = {r[0] or "-": round(float(r[1]), 6) for r in conn.execute(
            "SELECT model, SUM(usd) FROM ns_spend WHERE day=? GROUP BY model", (day,))}
        by_tier = {int(r[0]): round(float(r[1]), 6) for r in conn.execute(
            "SELECT tier, SUM(usd) FROM ns_spend WHERE day=? GROUP BY tier", (day,))}
        total = round(sum(by_project.values()), 6)
        artefacts = conn.execute(
            "SELECT COUNT(*) FROM ns_spend WHERE day=? AND artefact<>''", (day,)).fetchone()[0]
        return {"day": day, "total_usd": total, "by_project": by_project,
                "by_model": by_model, "by_tier": by_tier,
                "artefacts": int(artefacts),
                "artefacts_per_usd": round(artefacts / total, 2) if total > 0 else None,
                "daily_budget_usd": DAILY_BUDGET_USD,
                "remaining_usd": round(max(0.0, DAILY_BUDGET_USD - total), 6)}
    finally:
        if own:
            conn.close()
