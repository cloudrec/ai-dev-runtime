"""Zero-manual-registration agent discovery.

Enumerate live tmux Claude agents every control-loop tick and reconcile the durable
AgentRegistry — regardless of how the agent was created (manual shell, outside
ChatGPT/Owner OS, new session name). Visibility NEVER depends on a static YAML
allowlist; static policy limits only ACTIONS.

Lifecycle: discovered → classifying → {managed | observe_only | blocked_unknown_scope}
→ dead → recovered. A cwd that maps to an approved managed project inherits its bounded
safe policy automatically; an unknown scope is observed immediately, autonomous actions
are blocked, a candidate project is inferred, and ONE correlated owner/CTO decision is
raised (never ignored). Renames / resumes / restarts reconcile by conversation_id or
cwd — never a duplicate agent.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from core.control_plane import api
from core.control_plane import cto

MANAGED = "managed"
OBSERVE_ONLY = "observe_only"
BLOCKED_UNKNOWN = "blocked_unknown_scope"
DEAD = "dead"
RECOVERED = "recovered"


def _allowed_roots(config: dict) -> list:
    return config.get("allowed_roots") or ["/opt", "/root/ai-dev-runtime"]


def classify_scope(cwd: str, session: str, config: dict) -> dict:
    """Pure classification of an agent's scope from the orchestrator config.

    - a session with `mode: auto`, or a cwd under a configured session root → MANAGED
      (inherits that project's bounded safe policy);
    - a cwd under an allowed root but not configured → OBSERVE_ONLY with an inferred
      candidate project (monitor now, block autonomous actions, raise one decision);
    - anything else → BLOCKED_UNKNOWN_SCOPE.
    """
    sessions = config.get("sessions") or {}
    sc = sessions.get(session)
    if isinstance(sc, dict):
        if sc.get("mode") == "auto":
            return {"lifecycle": MANAGED, "project_id": sc.get("project") or session,
                    "policy": "inherited_managed", "owner_action_required": False}
        # configured but hold/monitor → observed, not autonomously actuated
        return {"lifecycle": OBSERVE_ONLY, "project_id": sc.get("project") or session,
                "policy": "configured_non_auto", "owner_action_required": False}
    # not configured — match by cwd against configured roots
    for name, s in sessions.items():
        root = isinstance(s, dict) and s.get("root")
        if root and cwd and (cwd == root or cwd.startswith(root.rstrip("/") + "/")):
            if s.get("mode") == "auto":
                return {"lifecycle": MANAGED, "project_id": s.get("project") or name,
                        "policy": "inherited_by_cwd", "owner_action_required": False}
            return {"lifecycle": OBSERVE_ONLY, "project_id": s.get("project") or name,
                    "policy": "configured_root_non_auto", "owner_action_required": False}
    # unknown scope — infer a candidate project, observe, block autonomous, raise decision
    for root in _allowed_roots(config):
        if cwd and (cwd == root or cwd.startswith(root.rstrip("/") + "/")):
            inferred = os.path.basename(cwd.rstrip("/")) or "unknown"
            return {"lifecycle": OBSERVE_ONLY, "project_id": inferred,
                    "policy": "inferred_unknown_scope", "owner_action_required": True}
    return {"lifecycle": BLOCKED_UNKNOWN, "project_id": "",
            "policy": "unknown_scope", "owner_action_required": True}


def _load_config() -> dict:
    try:
        from core import agent_orchestrator as orch
        return orch.load_config() or {}
    except Exception:  # noqa: BLE001
        return {}


def _conversation_id(cwd: str) -> Optional[str]:
    try:
        from core import agent_control as ac
        ev = ac.conversation_evidence(cwd) or {}
        return (ev.get("latest") or {}).get("conversation_id")
    except Exception:  # noqa: BLE001
        return None


def discover(inventory: Optional[dict] = None, *, config: Optional[dict] = None,
             conversation_fn: Optional[Callable] = None, emit_fn: Optional[Callable] = None,
             now_ts: Optional[float] = None) -> dict:
    """One discovery sweep. Reconciles the registry from the live tmux inventory and
    emits correlated CTO events. Read-only w.r.t. the panes (no actuation)."""
    from core.control_plane.store import now_ts as _nowts
    now_ts = now_ts if now_ts is not None else _nowts()
    config = config if config is not None else _load_config()
    conversation_fn = conversation_fn or _conversation_id
    emit = emit_fn or cto.emit

    if inventory is None:
        from core import agent_control as ac
        inventory = ac.agent_list()

    live = [a for a in (inventory.get("agents") or []) if a.get("is_agent") and a.get("alive")]
    live_targets = {a["target"] for a in live}
    summary = {"discovered": 0, "managed": 0, "observe_only": 0, "blocked": 0,
               "duplicates": 0, "dead": 0, "recovered": 0, "events": []}

    # group for duplicate detection: >1 live agent on the same cwd/project
    by_cwd: dict = {}
    for a in live:
        cwd = a.get("claude_cwd") or a.get("cwd") or ""
        by_cwd.setdefault(cwd, []).append(a)

    for a in live:
        target = a["target"]
        session = a.get("session") or target.split(":", 1)[0]
        cwd = a.get("claude_cwd") or a.get("cwd") or ""
        conv = conversation_fn(cwd) if cwd else None
        prior = api.get_agent(target)

        # rename / move / restart reconciliation: a record with this conversation but a
        # DIFFERENT target is the same agent renamed — update, never duplicate.
        renamed_from = None
        if not prior and conv:
            match = api.find_agent_by_conversation(conv)
            if match and match["target"] != target and match["target"] not in live_targets:
                renamed_from = match["target"]
                api.set_lifecycle(match["target"], DEAD)   # old target retired (renamed)
                # The watcher tracks panes by tmux target, so the old name is about to
                # stop appearing in its inventory — and absence is how it recognises a
                # crash. Retiring the watch state here is the only moment anything knows
                # the difference between "renamed" and "died".
                #
                # But the match above is NOT strong enough to silence a crash alarm on.
                # `conv` comes from the CWD, not the pane, so every agent working in the
                # same directory carries the same conversation id: a pane that genuinely
                # died and a DIFFERENT pane that replaced it in that directory are
                # indistinguishable by conversation alone. A rename moves a label and
                # keeps the process, so process continuity is the evidence that separates
                # them, and the pid is already in both records. Without pid equality the
                # registry still reconciles exactly as before — one row, no duplicate —
                # and the watcher is left to reach its own verdict, because the cheap way
                # to stop a false crash alarm is to stop reporting crashes.
                same_process = False
                try:
                    same_process = bool(a.get("pid")) and \
                        int(match.get("pid") or 0) == int(a.get("pid"))
                except (TypeError, ValueError):
                    same_process = False
                if same_process:
                    try:
                        from core import agent_watch as _aw
                        _aw.retire(renamed_from, reason=f"renamed to {target}",
                                   by="discovery")
                    except Exception:  # noqa: BLE001 — must not break the sweep
                        pass

        reg = api.register_agent(target, session=session, cwd=cwd, pid=a.get("pid"),
                                 command=a.get("command"), conversation_id=conv or "")
        summary["discovered"] += 1

        scope = classify_scope(cwd, session, config)
        # recovery: a previously-dead record for the SAME conversation returns to life
        recovered = bool(prior and prior.get("lifecycle_state") == DEAD)
        api.set_lifecycle(target, RECOVERED if recovered else scope["lifecycle"],
                          project_id=scope["project_id"],
                          responsible_controller="control_plane")
        if recovered:
            summary["recovered"] += 1
            emit("discovery", "agent_recovered", project_id=scope["project_id"],
                 agent_id=target, severity="high", payload={"conversation_id": conv},
                 dedup_key=f"recovered:{target}:{conv}")

        # classification tallies + first-sight / unknown-scope CTO events
        if scope["lifecycle"] == MANAGED:
            summary["managed"] += 1
        elif scope["lifecycle"] == OBSERVE_ONLY:
            summary["observe_only"] += 1
        else:
            summary["blocked"] += 1

        if reg["is_new"] or renamed_from:
            ev = emit("discovery", "new_agent_discovered", project_id=scope["project_id"],
                      agent_id=target, severity="info",
                      owner_action_required=scope["owner_action_required"],
                      payload={"cwd": cwd, "session": session, "pid": a.get("pid"),
                               "conversation_id": conv, "lifecycle": scope["lifecycle"],
                               "policy": scope["policy"], "renamed_from": renamed_from},
                      action_taken=f"registered as {scope['lifecycle']}",
                      dedup_key=f"newagent:{target}:{conv or ''}")
            summary["events"].append({"target": target, "lifecycle": scope["lifecycle"],
                                      "event_id": ev["event_id"]})
            # unknown scope → exactly ONE correlated owner decision (a gate), not ignored
            if scope["owner_action_required"]:
                api.open_gate(agent_id=target, reason=f"unknown-scope agent at {cwd}",
                              kind="classify_scope", correlation_id=f"scope:{target}")

    # duplicate detection (post-pass): >1 live agent on the same cwd. The PRIMARY is the
    # oldest first-seen (then lowest target); the others are flagged duplicate_of it, and
    # NO conflicting command is ever issued (visibility + one owner decision only).
    reg_by_target = {r["target"]: r for r in api.get_registry()}
    for cwd, group in by_cwd.items():
        if not cwd or len(group) < 2:
            continue
        members = [reg_by_target[a["target"]] for a in group if a["target"] in reg_by_target]
        primary = sorted(members, key=lambda r: (r.get("first_seen_at") or "", r["target"]))[0]["target"]
        for a in group:
            t = a["target"]
            if t == primary:
                continue
            if reg_by_target.get(t, {}).get("duplicate_of") == primary:
                continue                                  # already flagged
            api.set_lifecycle(t, reg_by_target[t]["lifecycle_state"], duplicate_of=primary)
            summary["duplicates"] += 1
            emit("discovery", "duplicate_agent_detected",
                 project_id=reg_by_target[t].get("project_id") or "", agent_id=t,
                 severity="high", owner_action_required=True,
                 payload={"duplicate_of": primary, "cwd": cwd},
                 action_taken="flagged duplicate; no conflicting command issued",
                 dedup_key=f"dup:{t}:{primary}")

    # dead detection: registry agents (not already dead) missing from the live set and
    # not reconciled as a rename.
    for rec in api.get_registry():
        if rec["target"] in live_targets:
            continue
        if rec.get("lifecycle_state") in (DEAD,):
            continue
        api.set_lifecycle(rec["target"], DEAD)
        summary["dead"] += 1
        emit("discovery", "agent_dead", project_id=rec.get("project_id") or "",
             agent_id=rec["target"], severity="high",
             payload={"conversation_id": rec.get("conversation_id")},
             action_taken="marked dead; conversation_id preserved for recovery",
             dedup_key=f"dead:{rec['target']}:{rec.get('conversation_id') or ''}")

    return summary
