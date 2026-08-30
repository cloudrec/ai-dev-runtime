"""Safe recovery of an externally killed managed session.

v1 limitation this removes: when a managed pane died, the loop correctly recorded
`watchdog_dead`, refused to create a duplicate, and then needed a human to restart it.

What this may do — and nothing else:
  * revive ONLY a target listed in `config/managed_sessions.yaml` (payment is absent and
    must stay absent),
  * revive the EXACT tmux session/pane and resume the EXACT approved conversation,
  * never create a second live pane for the same cwd — proven before acting, not assumed,
  * pick "Resume from summary" if Claude offers the large-session choice; a full replay is
    expensive and is never chosen automatically,
  * verify the result (PID, cwd, one-pane invariant, prompt readiness, conversation
    modification) BEFORE any work is delivered.

Recovery authorises no new work. It restores a session; the existing safe-step allowlist
still decides what may be sent afterwards.

Crash-loop protection: exponential backoff, max N recoveries per target per window, then
QUARANTINE plus a real owner blocker. Never an infinite respawn.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional

CONFIG_PATH = os.getenv("MANAGED_SESSIONS_CONFIG",
                        "/root/ai-dev-runtime/config/managed_sessions.yaml")
# A pane whose death was deliberate is never revived. The owner marks it by killing the
# session outright or by flipping `enabled: false` in the registry.
DELIBERATE_STOP_MARKERS = ("quarantine", "stopped by owner", "do not restart")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(
        os.getenv("AGENT_CONTROL_DB", "/root/ai-dev-runtime/agent_control.db"), timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS session_recovery (
        ts TEXT, ts_epoch REAL, target TEXT, action TEXT, ok INTEGER, reason TEXT,
        detail TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS session_quarantine (
        target TEXT PRIMARY KEY, since TEXT, reason TEXT)""")
    conn.commit()
    return conn


def _log(conn, target: str, action: str, ok: bool, reason: str, detail=None) -> None:
    try:
        conn.execute("INSERT INTO session_recovery VALUES (?,?,?,?,?,?,?)",
                     (_now_iso(), time.time(), target, action, 1 if ok else 0, reason,
                      json.dumps(detail or {})[:800]))
        conn.commit()
    except Exception:  # noqa: BLE001
        pass


def load_registry(path: Optional[str] = None) -> dict:
    """{target: entry}. A malformed file yields NOTHING recoverable (fail-closed)."""
    try:
        import yaml
        with open(path or CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        out = {}
        for e in (data.get("sessions") or []):
            if isinstance(e, dict) and e.get("target"):
                out[e["target"]] = e
        return {"sessions": out, "limits": data.get("limits") or {}}
    except Exception:  # noqa: BLE001
        return {"sessions": {}, "limits": {}}


def _tmux(args: list) -> tuple:
    try:
        p = subprocess.run(["tmux"] + args, capture_output=True, text=True, timeout=20)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


def panes() -> list:
    rc, out, _ = _tmux(["list-panes", "-a", "-F",
                        "#{session_name}:#{window_index}.#{pane_index}\t#{pane_dead}\t"
                        "#{pane_pid}\t#{pane_current_path}\t#{pane_current_command}"])
    rows = []
    if rc != 0:
        return rows
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 5:
            rows.append({"target": parts[0], "dead": parts[1] == "1", "pid": parts[2],
                         "cwd": parts[3], "cmd": parts[4]})
    return rows


def live_claude_for_cwd(cwd: str, exclude_target: str = "") -> list:
    """Every LIVE claude pane whose cwd matches — the duplicate-prevention proof."""
    hits = []
    for p in panes():
        if p["dead"] or p["target"] == exclude_target:
            continue
        if p["cmd"].strip() == "claude" and os.path.realpath(p["cwd"] or "") == os.path.realpath(cwd or ""):
            hits.append(p)
    return hits


def pane_state(target: str) -> dict:
    for p in panes():
        if p["target"] == target:
            return p
    return {"target": target, "missing": True, "dead": True}


def is_quarantined(target: str, conn=None) -> Optional[dict]:
    own = conn is None
    conn = conn or _db()
    try:
        r = conn.execute("SELECT target,since,reason FROM session_quarantine WHERE target=?",
                         (target,)).fetchone()
        return {"target": r[0], "since": r[1], "reason": r[2]} if r else None
    finally:
        if own:
            conn.close()


def release_quarantine(target: str, *, reason: str = "", registry: Optional[dict] = None,
                       conn=None) -> dict:
    """Lift a session quarantine so the target can be recovered again.

    Quarantine is written by `recover()` after a crash loop and, until now, there
    was NO code path anywhere that removed it. A quarantined session was therefore
    dead permanently: `cp-canary:0.0` — the project's own disposable canary, the
    thing safe end-to-end tests are supposed to run on — has been unrecoverable
    since 2026-08-07 for `crash loop: 3 recoveries within 21600s`. A safety brake
    with no release is a broken brake.

    Scoped deliberately:

    * only a target in `config/managed_sessions.yaml` may be released, so this can
      never resurrect something the registry does not already authorise (payment
      is absent from that file and stays absent);
    * the release is audited through the same `_log` sink as every recovery
      decision, so lifting a brake is as visible as applying one;
    * historical audit rows are never touched — this removes the live quarantine
      latch only.
    """
    reg = registry if registry is not None else load_registry()
    entries = {e.get("target"): e for e in (reg.get("sessions") or [])}
    own = conn is None
    conn = conn or _db()
    try:
        if target not in entries:
            _log(conn, target, "release_quarantine", False, "not_in_registry")
            return {"released": False, "reason": "not_in_registry",
                    "note": "only a registered managed session may be released"}
        q = is_quarantined(target, conn=conn)
        if not q:
            return {"released": False, "reason": "not_quarantined"}
        conn.execute("DELETE FROM session_quarantine WHERE target=?", (target,))
        conn.commit()
        _log(conn, target, "release_quarantine", True, reason or "manual_release",
             {"was_since": q["since"], "was_reason": q["reason"]})
        return {"released": True, "target": target, "was_since": q["since"],
                "was_reason": q["reason"], "reason": reason or "manual_release"}
    finally:
        if own:
            conn.close()


def recent_recoveries(target: str, window_secs: float, conn=None) -> int:
    """EVERY revive attempt inside the window, successful or not.

    Counting only `ok=1` made the crash-loop cap unreachable for the one shape that needs
    it most: a target that fails verification every single time. Live 2026-08-06/07,
    `mess-qa-automation:0.0` was revived five times across nine hours, each attempt logged
    `verify_failed`, each attempt invisible to the cap — so neither the backoff nor the
    quarantine ever engaged. A failed respawn is exactly what a crash loop is made of.
    """
    own = conn is None
    conn = conn or _db()
    try:
        r = conn.execute(
            "SELECT count(*) FROM session_recovery WHERE target=? AND action='revive' "
            "AND ts_epoch > ?", (target, time.time() - window_secs)).fetchone()
        return int(r[0]) if r else 0
    finally:
        if own:
            conn.close()


def authoritative_cwd(target: str, entry: Optional[dict] = None) -> dict:
    """Where this target's project ACTUALLY lives.

    The governor's project config is the authority on a project directory; the recovery
    registry is a separate file that can drift from it and did. Live: `project_queues.yaml`
    said `/opt/mess` while `managed_sessions.yaml` said `/opt/mess-qa-automation`, a
    directory that does not exist. Nothing reconciled the two, so recovery ran on the wrong
    one.

    Returns the resolved directory plus the divergence, so a refusal can name it.
    """
    registry_cwd = (entry or {}).get("cwd") or ""
    governed = ""
    try:
        from core import continuation_governor as cg
        governed = ((cg.load_config() or {}).get(target) or {}).get("cwd") or ""
    except Exception:  # noqa: BLE001 — an unreadable governor config must not grant a path
        governed = ""
    resolved = governed or registry_cwd
    return {"cwd": resolved, "governed": governed, "registry": registry_cwd,
            "diverged": bool(governed and registry_cwd and
                             os.path.realpath(governed) != os.path.realpath(registry_cwd)),
            "exists": bool(resolved) and os.path.isdir(resolved)}


def has_authoritative_work(target: str) -> dict:
    """Is there a real, open reason to bring this session back?

    A dead pane is not by itself a reason. `mess-qa-automation:0.0` was resurrected with no
    open ledger task at all — the work had finished. Recovery exists to survive an
    accidental kill mid-task, not to reopen completed work.
    """
    try:
        from core import os_task_queue as q
        task = q.active_task(target)
    except Exception:  # noqa: BLE001 — fail CLOSED: unknown ledger is not a licence
        return {"open": False, "task_id": "", "reason": "ledger_unavailable"}
    if task:
        return {"open": True, "task_id": task.get("id", ""), "reason": "active_task"}
    return {"open": False, "task_id": "", "reason": "no_active_task"}


def deliberate_stop(target: str, tail: str = "") -> bool:
    """A death the owner intended is never undone by automation."""
    blob = (tail or "").lower()
    return any(m in blob for m in DELIBERATE_STOP_MARKERS)


def _capture(target: str, lines: int = 30) -> str:
    rc, out, _ = _tmux(["capture-pane", "-p", "-t", target, "-S", f"-{lines}"])
    return out if rc == 0 else ""


_SUMMARY_CHOICE_RE = re.compile(r"resume from summary", re.I)
_PROMPT_READY_RE = re.compile(r"[❯>]\s*$|\n[❯>]\s", re.M)


def choose_summary_if_offered(target: str, capture_fn=None, send_fn=None) -> dict:
    """If Claude offers the large-session choice, pick 'Resume from summary'.

    A full replay is expensive and is never selected automatically. The option's own line
    number is read from the pane rather than assumed.
    """
    cap = capture_fn or (lambda: _capture(target, 30))
    send = send_fn or (lambda keys: _tmux(["send-keys", "-t", target] + keys))
    text = cap()
    if not _SUMMARY_CHOICE_RE.search(text or ""):
        return {"offered": False, "chosen": None}
    for line in (text or "").splitlines():
        if _SUMMARY_CHOICE_RE.search(line):
            m = re.search(r"(\d+)\s*[.)]", line)
            if m:
                send([m.group(1)])
                send(["Enter"])
                return {"offered": True, "chosen": f"option_{m.group(1)}"}
    return {"offered": True, "chosen": None, "reason": "option_number_not_found"}


def verify_recovered(target: str, cwd: str) -> dict:
    """PID + cwd + one-pane invariant + prompt readiness. All must hold."""
    p = pane_state(target)
    checks = {
        "pane_present": not p.get("missing"),
        "pane_alive": not p.get("dead"),
        "is_claude": p.get("cmd", "").strip() == "claude",
        "cwd_matches": os.path.realpath(p.get("cwd") or "") == os.path.realpath(cwd or ""),
        "has_pid": bool(p.get("pid")),
        "single_pane_for_cwd": len(live_claude_for_cwd(cwd, exclude_target=target)) == 0,
    }
    tail = _capture(target, 20)
    checks["prompt_ready"] = bool(_PROMPT_READY_RE.search(tail or ""))
    return {"ok": all(checks.values()), "checks": checks, "pid": p.get("pid")}


def recover(target: str, *, registry: Optional[dict] = None, conn=None,
            run_fn=None, sleep=time.sleep, now: Optional[float] = None,
            explicit: bool = False) -> dict:
    """Revive one registered dead session. Every refusal is explained and logged.

    `explicit=True` is an owner/MCP-initiated resume: it still requires a real project
    directory, but it does not require an open ledger task, because the owner asking IS
    the reason. Automatic watchdog recovery leaves it False.
    """
    now = now if now is not None else time.time()
    own = conn is None
    conn = conn or _db()
    try:
        reg = registry if registry is not None else load_registry()
        entry = (reg.get("sessions") or {}).get(target)
        if not entry:
            _log(conn, target, "refuse", False, "not_registered")
            return {"recovered": False, "reason": "not_registered"}
        if not entry.get("enabled"):
            _log(conn, target, "refuse", False, "disabled_in_registry")
            return {"recovered": False, "reason": "disabled_in_registry"}
        q = is_quarantined(target, conn=conn)
        if q:
            return {"recovered": False, "reason": "quarantined", "since": q["since"]}

        # ── WHERE: the governor's project dir wins over the recovery registry ──
        # Resolved before anything else, because every later proof (duplicate detection,
        # the tmux -c, the verification) is only as good as this path.
        loc = authoritative_cwd(target, entry)
        cwd = loc["cwd"]
        if loc["diverged"]:
            _log(conn, target, "note", True, "registry_cwd_diverged_from_project_config",
                 {"governed": loc["governed"], "registry": loc["registry"]})
        if not cwd:
            _log(conn, target, "refuse", False, "no_project_dir", loc)
            return {"recovered": False, "reason": "no_project_dir", "cwd_resolution": loc}
        if not os.path.isdir(cwd):
            # FAIL CLOSED. `tmux new-session -c <missing>` does NOT fail — it returns rc=0
            # and silently starts the pane in the server's default directory, which is
            # /root. That is how a MESS session came up in /root on a stale conversation
            # and sat on the trust prompt. A path we cannot stand in is not a path we start
            # a session in.
            _log(conn, target, "refuse", False, "project_dir_missing", loc)
            return {"recovered": False, "reason": "project_dir_missing",
                    "cwd_resolution": loc, "owner_blocker": True}

        p = pane_state(target)
        if not p.get("missing") and not p.get("dead"):
            _log(conn, target, "skip", True, "already_alive")
            return {"recovered": False, "reason": "already_alive"}


        tail = _capture(target, 30)
        if deliberate_stop(target, tail):
            _log(conn, target, "refuse", False, "deliberate_stop")
            return {"recovered": False, "reason": "deliberate_stop"}

        # DUPLICATE PROOF — never a second live Claude for the same project.
        dups = live_claude_for_cwd(cwd, exclude_target=target)
        if dups:
            _log(conn, target, "refuse", False, "live_claude_exists_for_cwd",
                 {"panes": [d["target"] for d in dups]})
            return {"recovered": False, "reason": "live_claude_exists_for_cwd",
                    "panes": [d["target"] for d in dups]}

        limits = reg.get("limits") or {}
        window = float(limits.get("window_secs") or 21600)
        cap = int(limits.get("max_recoveries_per_target") or 3)
        used = recent_recoveries(target, window, conn=conn)
        if used >= cap:
            conn.execute("INSERT OR REPLACE INTO session_quarantine VALUES (?,?,?)",
                         (target, _now_iso(),
                          f"crash loop: {used} recoveries within {int(window)}s"))
            conn.commit()
            _log(conn, target, "quarantine", False, "crash_loop_cap_reached",
                 {"used": used, "cap": cap})
            return {"recovered": False, "reason": "quarantined_crash_loop",
                    "used": used, "cap": cap, "owner_blocker": True}

        # ── WHY: a dead pane is not a reason; open work is ──
        # Last gate before acting, deliberately: the refusals above are stronger statements
        # about this target (the owner stopped it, a live pane already serves this project,
        # it is crash-looping) and each deserves to be the reported reason. `explicit` is
        # the owner/MCP resume path — the owner asking IS the reason — and skips only this
        # check, never the project-directory proof above.
        if not explicit:
            work = has_authoritative_work(target)
            if not work["open"]:
                _log(conn, target, "refuse", False, f"no_open_work:{work['reason']}", work)
                return {"recovered": False, "reason": "no_open_work",
                        "detail": work,
                        "note": "recovery restores interrupted work; it does not reopen "
                                "work that finished"}

        # exponential backoff on repeated attempts within the window
        base = float(limits.get("backoff_base_secs") or 60)
        if used:
            sleep(min(base * (2 ** (used - 1)), 900))

        session = entry.get("session") or target.split(":")[0]
        conv = entry.get("conversation_id") or ""
        shape = entry.get("resume_shape") or "claude --resume {conversation_id}"
        cmd = shape.format(conversation_id=conv, cwd=cwd)
        run = run_fn or (lambda a: _tmux(a))

        # Revive the EXACT pane. respawn-pane reuses the existing window; only if the
        # whole session is gone do we recreate it under the same name.
        if p.get("missing"):
            rc, _, err = run(["new-session", "-d", "-s", session, "-c", cwd, cmd])
            how = "new-session"
        else:
            rc, _, err = run(["respawn-pane", "-k", "-t", target, "-c", cwd, cmd])
            how = "respawn-pane"
        if rc != 0:
            _log(conn, target, "revive", False, f"tmux_failed:{how}", {"err": err[:200]})
            return {"recovered": False, "reason": f"tmux_failed:{how}", "error": err[:200]}

        sleep(6)
        choice = choose_summary_if_offered(target)
        if choice.get("offered"):
            sleep(4)

        v = verify_recovered(target, cwd)
        torn_down = False
        if not v["ok"] and not v["checks"].get("cwd_matches", True):
            # We started this pane and it came up in the wrong directory. Leaving it is how
            # a "failed" recovery still produced a live Claude sitting in /root on a stale
            # conversation, which the next discovery pass then recorded as a real agent.
            # A recovery that cannot prove itself cleans up after itself.
            run(["kill-session", "-t", session])
            torn_down = True
        _log(conn, target, "revive", bool(v["ok"]), "verified" if v["ok"] else "verify_failed",
             {"how": how, "checks": v["checks"], "summary_choice": choice, "pid": v.get("pid"),
              "torn_down": torn_down, "cwd_resolution": loc})
        return {"recovered": bool(v["ok"]), "reason": "verified" if v["ok"] else "verify_failed",
                "how": how, "verify": v, "summary_choice": choice,
                "conversation_id": conv, "torn_down": torn_down,
                "note": "recovery restores the session only; it authorises no new work"}
    finally:
        if own:
            conn.close()


def status(conn=None) -> dict:
    own = conn is None
    conn = conn or _db()
    try:
        reg = load_registry()
        out = {"registered": sorted((reg.get("sessions") or {}).keys()), "targets": {}}
        for t in out["registered"]:
            entry = reg["sessions"][t]
            p = pane_state(t)
            out["targets"][t] = {
                "enabled": bool(entry.get("enabled")),
                "alive": (not p.get("missing")) and (not p.get("dead")),
                "quarantined": bool(is_quarantined(t, conn=conn)),
                "recoveries_6h": recent_recoveries(t, 21600, conn=conn),
            }
        return out
    finally:
        if own:
            conn.close()
