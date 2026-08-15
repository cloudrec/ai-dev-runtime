"""Model Router core (task 209) — the standing Owner OS 2.0 cost-aware model
routing policy.

Same discipline as core/venture_radar.py: partition WORK by model instead of
paying for the strongest model on everything. This module DECIDES and
RECORDS; it never dispatches an agent, spends money, or calls the network
itself — dispatch is someone else's job, using the model this returns.

The decision + outcome ledger is the feed for the future Agent Reputation
routing work: once enough (model, task_class) history exists, routing can
condition on measured success rate instead of the static partition below.

NOTE: this is NOT core/model_routing.py. That module is the separate Night
Shift budget policy (spend caps across a run) and is untouched by this one.
"""
from __future__ import annotations

from typing import Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

SONNET, OPUS, FABLE = "sonnet", "opus", "fable"
MODEL_IDS = {SONNET: "claude-sonnet-5", OPUS: "claude-opus-5", FABLE: "claude-fable-5"}
RANK = {SONNET: 1, OPUS: 2, FABLE: 3}

# Closed task-class vocabulary, partitioned by which model normally owns it.
# route() refuses unknown classes only when strict=True; otherwise it fails
# toward the cheap model (sonnet) rather than toward the expensive one.
SONNET_CLASSES = (
    "routine_implementation", "tests", "docs", "repo_inspection",
    "deterministic_bugfix", "context_pack", "monitoring",
    "repetitive_analysis", "clear_finding_implementation",
)
OPUS_CLASSES = (
    "architecture", "ambiguous_root_cause", "money_integrity", "security",
    "high_risk_decision", "migration_design", "release_design", "senior_review",
)
FABLE_CLASSES = (
    "hardest_unresolved_bug", "adversarial_audit", "final_deep_audit",
    "tier_disagreement",
)
PARTITION = {SONNET: list(SONNET_CLASSES), OPUS: list(OPUS_CLASSES), FABLE: list(FABLE_CLASSES)}

_CLASS_MODEL = {}
for _m, _classes in PARTITION.items():
    for _cls in _classes:
        _CLASS_MODEL[_cls] = _m

_RISK_VALUES = ("normal", "low", "money", "security", "high")
_RISK_FLOOR_VALUES = ("money", "security", "high")
_OUTCOME_VALUES = ("success", "failure", "uncertain", "disagreement")
_RECORD_OUTCOMES = ("success", "failure", "retry", "escalated")

_LADDER_DESCRIPTION = [
    "sonnet failure/uncertain -> at least opus (sonnet_insufficient)",
    "opus failure/uncertain/disagreement, or sonnet+opus disagreeing -> fable "
    "(escalate_to_fable: opus insufficient or tiers disagree)",
    "clear_finding_implementation always de-escalates back to sonnet after "
    "any prior escalation, unless the money/security/high risk floor holds "
    "it at opus (fable findings implement at sonnet unless high-risk)",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS router_decision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_ref TEXT, task_class TEXT, risk TEXT,
    model TEXT, model_id TEXT, reason TEXT,
    context_pack TEXT, requires_context_pack INTEGER,
    at TEXT, ts REAL
);
CREATE TABLE IF NOT EXISTS router_outcome (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER, outcome TEXT,
    tokens_in INTEGER, tokens_out INTEGER, usd REAL, retries INTEGER,
    note TEXT, at TEXT, ts REAL
)
"""


class RouterError(Exception):
    """Refusal with an exact reason — mapped to HTTP 409 by the API layer."""


def _conn(conn=None):
    conn, own = _c(conn)
    for stmt in _SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    return conn, own


def _raise_to(model: str, floor: str) -> str:
    """Never lowers — only raises `model` up to `floor` if floor outranks it."""
    return floor if RANK[floor] > RANK[model] else model


def _validate_prior_attempts(prior_attempts) -> list:
    out = []
    for a in prior_attempts or []:
        m = a.get("model") if isinstance(a, dict) else None
        o = a.get("outcome") if isinstance(a, dict) else None
        if m not in (SONNET, OPUS, FABLE):
            raise RouterError(f"invalid prior_attempts model {m!r}")
        if o not in _OUTCOME_VALUES:
            raise RouterError(f"invalid prior_attempts outcome {o!r}")
        out.append({"model": m, "outcome": o})
    return out


def route(task_class: str, *, risk: str = "normal", prior_attempts=None,
          context_pack: str = "", task_ref: str = "", strict: bool = False,
          conn=None) -> dict:
    """Decide a model for a unit of work and RECORD the decision durably.

    Order: base class -> risk floor -> escalation ladder -> de-escalation
    for clear_finding_implementation -> context-pack economics -> record.
    """
    if risk not in _RISK_VALUES:
        raise RouterError(f"unknown risk {risk!r}; valid: {_RISK_VALUES}")
    attempts = _validate_prior_attempts(prior_attempts)

    # 1. base model from the partition (fail-toward-cheap by default)
    if task_class in _CLASS_MODEL:
        model = _CLASS_MODEL[task_class]
        reason = f"class:{task_class}->{model}"
    else:
        if strict:
            raise RouterError(f"unknown task_class {task_class!r}; strict routing refuses")
        model = SONNET
        reason = "unknown_class_defaults_to_sonnet"

    # 2. risk floor — never lowered
    if risk in _RISK_FLOOR_VALUES:
        model = _raise_to(model, OPUS)
        reason += f"; risk={risk} floors to opus"

    # 3. escalation ladder from prior attempts
    sonnet_attempts = [a for a in attempts if a["model"] == SONNET]
    opus_attempts = [a for a in attempts if a["model"] == OPUS]
    if any(a["outcome"] in ("failure", "uncertain") for a in sonnet_attempts):
        model = _raise_to(model, OPUS)
        reason += "; sonnet_insufficient"
    tiers_disagree = (any(a["outcome"] == "disagreement" for a in sonnet_attempts)
                       and any(a["outcome"] == "disagreement" for a in opus_attempts))
    if any(a["outcome"] in ("failure", "uncertain", "disagreement") for a in opus_attempts) \
            or tiers_disagree:
        model = FABLE
        reason += "; escalate_to_fable: opus insufficient or tiers disagree"

    # 4. de-escalation: clear findings go back to sonnet — unless the risk
    # floor holds opus, or sonnet already failed THIS unit (a sonnet failure
    # means the finding was not actually clear; forcing it back would loop)
    sonnet_failed_this = any(a["outcome"] in ("failure", "uncertain")
                             for a in sonnet_attempts)
    if task_class == "clear_finding_implementation" and not sonnet_failed_this:
        if risk in _RISK_FLOOR_VALUES:
            model = OPUS
        else:
            model = SONNET
            reason += "; fable findings implement at sonnet unless high-risk"

    # 5. context-pack economics — advisory only, never blocks
    requires_context_pack = model in (OPUS, FABLE)
    context_pack_missing = requires_context_pack and not (context_pack or "").strip()
    if context_pack_missing:
        reason += "; generate a compact context pack with sonnet first"

    conn, own = _conn(conn)
    try:
        cur = conn.execute(
            "INSERT INTO router_decision (task_ref,task_class,risk,model,model_id,"
            "reason,context_pack,requires_context_pack,at,ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (task_ref, task_class, risk, model, MODEL_IDS[model], reason,
             context_pack or "", int(requires_context_pack), now_iso(), now_ts()))
        conn.commit()
        return {
            "decision_id": cur.lastrowid,
            "model": model,
            "model_id": MODEL_IDS[model],
            "reason": reason,
            "requires_context_pack": requires_context_pack,
            "context_pack_missing": context_pack_missing,
        }
    finally:
        if own:
            conn.close()


def record_outcome(decision_id: int, *, outcome: str, tokens_in: int = 0,
                    tokens_out: int = 0, usd: float = 0.0, retries: int = 0,
                    note: str = "", conn=None) -> dict:
    """Multiple outcomes per decision are allowed — retries each get a row."""
    if outcome not in _RECORD_OUTCOMES:
        raise RouterError(f"unknown outcome {outcome!r}; valid: {_RECORD_OUTCOMES}")
    conn, own = _conn(conn)
    try:
        row = conn.execute("SELECT id FROM router_decision WHERE id=?",
                           (decision_id,)).fetchone()
        if not row:
            raise RouterError(f"unknown decision_id {decision_id}")
        cur = conn.execute(
            "INSERT INTO router_outcome (decision_id,outcome,tokens_in,tokens_out,"
            "usd,retries,note,at,ts) VALUES (?,?,?,?,?,?,?,?,?)",
            (decision_id, outcome, int(tokens_in), int(tokens_out), float(usd),
             int(retries), note[:500], now_iso(), now_ts()))
        conn.commit()
        return {"id": cur.lastrowid, "decision_id": decision_id, "outcome": outcome}
    finally:
        if own:
            conn.close()


def effectiveness(days: int = 30, conn=None) -> dict:
    """The Agent Reputation feed: per (model, task_class) and per-model rollup
    over the trailing window — attempts, outcomes, success rate, cost."""
    cutoff = now_ts() - (days * 86400)
    conn, own = _conn(conn)
    try:
        decisions = conn.execute(
            "SELECT id, model, task_class FROM router_decision WHERE ts >= ?",
            (cutoff,)).fetchall()
        dec_by_id = {d[0]: {"model": d[1], "task_class": d[2]} for d in decisions}
        pair_stats = {}
        model_stats = {}

        def _bucket(store, key):
            if key not in store:
                store[key] = {"attempts": 0, "outcomes": 0, "successes": 0,
                              "failures": 0, "tokens_in": 0, "tokens_out": 0,
                              "usd": 0.0, "retries": 0}
            return store[key]

        for d in dec_by_id.values():
            _bucket(pair_stats, (d["model"], d["task_class"]))["attempts"] += 1
            _bucket(model_stats, d["model"])["attempts"] += 1

        if dec_by_id:
            placeholders = ",".join("?" * len(dec_by_id))
            outcomes = conn.execute(
                f"SELECT decision_id, outcome, tokens_in, tokens_out, usd, retries "
                f"FROM router_outcome WHERE decision_id IN ({placeholders})",
                tuple(dec_by_id)).fetchall()
        else:
            outcomes = []

        for decision_id, outcome, tin, tout, usd, retries in outcomes:
            d = dec_by_id[decision_id]
            for store, key in ((pair_stats, (d["model"], d["task_class"])),
                              (model_stats, d["model"])):
                b = _bucket(store, key)
                b["outcomes"] += 1
                if outcome == "success":
                    b["successes"] += 1
                elif outcome == "failure":
                    b["failures"] += 1
                b["tokens_in"] += tin or 0
                b["tokens_out"] += tout or 0
                b["usd"] += usd or 0.0
                b["retries"] += retries or 0

        def _finish(b):
            b = dict(b)
            b["success_rate"] = (b["successes"] / b["outcomes"]) if b["outcomes"] else None
            return b

        pairs = [dict(model=m, task_class=tc, **_finish(v))
                for (m, tc), v in pair_stats.items()]
        by_model = {m: _finish(v) for m, v in model_stats.items()}
        return {"days": days, "since_ts": cutoff, "pairs": pairs, "by_model": by_model}
    finally:
        if own:
            conn.close()


def policy() -> dict:
    """Static dump for the adapter UI to render — no DB access needed."""
    return {"partition": PARTITION, "model_ids": MODEL_IDS, "ladder": list(_LADDER_DESCRIPTION)}
