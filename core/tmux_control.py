"""Authoritative reachability + integrity of the tmux control plane, and the ONE safe repair.

WHY THIS MODULE EXISTS — the 2026-08-30 incident, root-caused, not inferred:

`/root/cleanup_disk_pass2.sh` prunes `/tmp` top-level objects that contain nothing
modified in the last 48 h. A unix socket's mtime is stamped once, at `bind()` time, and
is NEVER updated by traffic — so the tmux control socket, bound when the server started
on 2026-08-12, looked 18 days idle to that cleaner and was deleted directory and all
(`disk_cleanup_pass2_20260830_134223.log:1353: DELETE OLD TMP: /tmp/tmux-0`).

What that did, and why it was invisible for 100 minutes (13:45:11 -> 15:25:07):

  * The tmux SERVER survived. A bound listening socket lives in the kernel; unlinking
    its filesystem name does not close it, and every ALREADY-ATTACHED client kept
    working, so nothing looked wrong from inside a pane.
  * Every NEW `connect()` — i.e. every Owner OS `agent_list` / `agent_status` — failed
    with ENOENT. Managed-agent control was gone.
  * Managed-agent health still reported `ok`: `agent_continuation_watchdog.health()`
    caught the inventory error, recorded it as a field, and fell through to `ok`; and
    `agent_list()`'s "no server running" branch returns an EMPTY-BUT-SUCCESSFUL
    inventory that all 20 of its callers read as "the fleet is fine, it is just empty".
  * A client that could not reach the control plane did what tmux always does: started
    a NEW server. `tmux new-session -d -s gaika-opus` (15:21:52) bound the same path and
    launched a SECOND live Claude on a project that already had one. The manual repair
    at 15:25:07 re-bound the path to the original server, orphaning that second server —
    which still runs, still holds an agent, and is now invisible to Owner OS entirely.

So the guard here is deliberately in two halves:

  DETECT (fail-closed). Reachability is a first-class health input. "I could not ask"
  is never "nothing is wrong": no health surface may report ok while the control plane
  is unreachable, and no mutating path (session creation, recovery) may run on an
  inventory it could not actually read. An unreadable inventory is UNKNOWN, and unknown
  is not empty.

  REPAIR (narrow). Exactly one repair is safe and it is the one an operator would do by
  hand: ask the SURVIVING server to re-create its socket file (`SIGUSR1`, which tmux
  handles by re-binding the listening socket). No session is created, killed, restarted
  or detached, and the proof of that is explicit — the repaired socket must lead back to
  the SAME server pid, or the repair reports failure. This module NEVER starts a tmux
  server: spawning one is precisely what turned a 100-minute outage into a duplicate
  live agent, and a missing server is an owner decision, not a self-heal.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from typing import Callable, Optional

# SO_ACCEPTCON in /proc/net/unix's Flags column: the socket is LISTENING.
_LISTEN_FLAG = 0x10000
_PROC_NET_UNIX = "/proc/net/unix"
_TMUX_TIMEOUT = int(os.getenv("TMUX_CONTROL_TIMEOUT", "15"))
# Auto-repair is on by default: the alternative is that a socket loss stays invisible
# until a human notices, which on 2026-08-30 took 100 minutes. It is safe because every
# precondition below must hold first, and because the only action taken is a signal to a
# process proven to be the surviving server for this exact socket path.
AUTO_REPAIR = os.getenv("TMUX_CONTROL_AUTO_REPAIR", "1") not in ("0", "false", "no")
_REPAIR_WAIT_SECS = float(os.getenv("TMUX_CONTROL_REPAIR_WAIT", "5"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def socket_path() -> str:
    """The control socket this host's tmux clients actually use.

    Inside a pane $TMUX names it exactly; otherwise it is tmux's own default,
    $TMUX_TMPDIR (or /tmp) / tmux-<euid> / default.
    """
    env = os.getenv("TMUX")
    if env:
        first = env.split(",")[0].strip()
        if first:
            return first
    base = os.getenv("TMUX_TMPDIR") or "/tmp"
    return os.path.join(base, f"tmux-{os.geteuid()}", "default")


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(
        os.getenv("AGENT_CONTROL_DB", "/root/ai-dev-runtime/agent_control.db"), timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS tmux_control_audit (
        ts TEXT, ts_epoch REAL, action TEXT, ok INTEGER, reason TEXT, detail TEXT)""")
    conn.commit()
    return conn


def _log(action: str, ok: bool, reason: str, detail=None, conn=None) -> None:
    """Durable audit. Never raises: a guard that dies while recording why it acted is
    worse than one that acts silently."""
    own = conn is None
    try:
        conn = conn or _db()
        conn.execute("INSERT INTO tmux_control_audit VALUES (?,?,?,?,?,?)",
                     (_now_iso(), time.time(), action, 1 if ok else 0, reason,
                      json.dumps(detail or {}, default=str)[:1200]))
        conn.commit()
    except Exception:  # noqa: BLE001
        pass
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _tmux(args: list) -> tuple:
    """One tmux command. The single seam the tests replace."""
    try:
        p = subprocess.run(["tmux", *args], capture_output=True, text=True,
                           timeout=_TMUX_TIMEOUT)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "tmux is not installed"
    except subprocess.TimeoutExpired:
        return 124, "", "tmux timed out"
    except Exception as e:  # noqa: BLE001
        return 1, "", f"{type(e).__name__}: {e}"


def _read_proc_net_unix() -> str:
    try:
        with open(_PROC_NET_UNIX, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:  # noqa: BLE001
        return ""


def parse_listeners(raw: str, path: str) -> list:
    """Every LISTENING unix socket bound to `path`, from /proc/net/unix text.

    Split out from the read so it is testable against recorded kernel output. The point
    of reading the kernel rather than the filesystem: a socket whose file was unlinked
    KEEPS its original name here, so this sees both the live listener and any orphaned
    server still holding the same path — the split-brain the incident produced.
    """
    out = []
    for line in (raw or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 8:
            continue
        if parts[7] != path:
            continue
        try:
            flags = int(parts[3], 16)
        except ValueError:
            continue
        if not (flags & _LISTEN_FLAG):
            continue
        out.append({"inode": parts[6], "flags": parts[3]})
    return out


def _pid_for_inode(inode: str) -> Optional[int]:
    """Which process holds socket:[inode]. Only called on the failure/integrity path —
    it walks every /proc/<pid>/fd, which is far too expensive for the healthy tick."""
    target = f"socket:[{inode}]"
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except Exception:  # noqa: BLE001
        return None
    for d in pids:
        fddir = f"/proc/{d}/fd"
        try:
            for fd in os.listdir(fddir):
                try:
                    if os.readlink(f"{fddir}/{fd}") == target:
                        return int(d)
                except (OSError, ValueError):
                    continue
        except (OSError, PermissionError):
            continue
    return None


def _proc_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        return ""


def _proc_ppid(pid: int) -> Optional[int]:
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as fh:
            after = fh.read().rsplit(")", 1)[1].split()
        return int(after[1])
    except Exception:  # noqa: BLE001
        return None


def is_tmux_server(pid: int, cmdline_fn: Optional[Callable] = None,
                   ppid_fn: Optional[Callable] = None) -> bool:
    """A process we are willing to SIGNAL. Deliberately strict: SIGUSR1's default
    disposition is TERMINATE, so signalling the wrong process — a tmux *client*, or a
    pid that was recycled — kills it. A tmux server is reparented to init and its
    argv still names tmux."""
    cmd = (cmdline_fn or _proc_cmdline)(pid)
    if not cmd:
        return False
    first = cmd.split()[0] if cmd.split() else ""
    if os.path.basename(first) not in ("tmux", "tmux:") and not cmd.startswith("tmux"):
        return False
    return (ppid_fn or _proc_ppid)(pid) == 1


def probe(run: Optional[Callable] = None, path: Optional[str] = None,
          resolve_pids: Optional[bool] = None,
          net_unix: Optional[Callable] = None) -> dict:
    """Can Owner OS reach the tmux control plane right now, and is that plane intact?

    Returns `reachable` plus a `reason` that names the failure class instead of
    collapsing every one of them into a falsy inventory:

      ok               — a real client round-trip succeeded
      socket_missing   — the socket path is gone (the 2026-08-30 class)
      no_server        — tmux answered that no server is running
      tmux_missing     — no tmux binary on this host
      timeout          — the server did not answer in time (a hung/blocked server)
      error            — anything else, verbatim

    `listeners` counts LISTENING sockets bound to this path, orphans included. More than
    one means two servers claim the control plane and one of them holds agents nobody
    can see — never healthy, and never auto-repairable (the fix would be to kill a
    server, i.e. to kill live agents, which is an owner decision).
    """
    run = run or _tmux
    p = path or socket_path()
    rc, _out, err = run(["list-sessions", "-F", "#{session_name}"])
    e = (err or "").lower()
    if rc == 0:
        reachable, reason = True, "ok"
    elif rc == 127 or "not installed" in e or "not found" in e:
        reachable, reason = False, "tmux_missing"
    elif rc == 124 or "timed out" in e or "timeout" in e:
        reachable, reason = False, "timeout"
    elif "no such file" in e or "error connecting" in e or "connection refused" in e:
        reachable, reason = False, "socket_missing"
    elif "no server running" in e:
        # tmux says this whenever it cannot reach a server, INCLUDING when the socket
        # file is simply absent. Only the kernel can tell the two apart, and the
        # difference decides whether starting a server would create a duplicate.
        reachable, reason = False, ("socket_missing" if not os.path.exists(p) else "no_server")
    else:
        reachable, reason = False, "error"

    listeners = parse_listeners((net_unix or _read_proc_net_unix)(), p)
    if resolve_pids is None:
        resolve_pids = (not reachable) or len(listeners) != 1
    pids = []
    if resolve_pids:
        for row in listeners:
            pid = _pid_for_inode(row["inode"])
            row["pid"] = pid
            if pid:
                pids.append(pid)

    out = {"reachable": reachable, "reason": reason, "socket_path": p,
           "socket_exists": os.path.exists(p), "listeners": len(listeners),
           "listener_pids": pids, "detail": (err or "").strip()[:200],
           "checked_at": _now_iso()}
    # Split brain: reachable or not, part of the fleet is unreachable through the
    # documented path, so managed-agent health is NOT ok.
    out["split_brain"] = len(listeners) > 1
    out["healthy"] = bool(reachable and not out["split_brain"])
    if out["split_brain"]:
        out["reason"] = "split_brain" if reachable else reason
    return out


def repair(*, dry_run: bool = False, run: Optional[Callable] = None,
           path: Optional[str] = None, kill_fn: Optional[Callable] = None,
           sleep: Callable = time.sleep, net_unix: Optional[Callable] = None,
           probe_fn: Optional[Callable] = None, conn=None) -> dict:
    """Re-create a DELETED control socket by asking the surviving server to re-bind it.

    Every precondition is a refusal, not a fallback. In particular this never starts a
    tmux server: when no server survives, the sessions are already gone and starting one
    would only manufacture an empty control plane that looks healthy.
    """
    run = run or _tmux
    pf = probe_fn or (lambda: probe(run=run, path=path, resolve_pids=True,
                                    net_unix=net_unix))
    before = pf()
    p = before.get("socket_path") or path or socket_path()

    def _refuse(reason, **extra):
        detail = {"probe": before, **extra}
        _log("repair_refused", False, reason, detail, conn=conn)
        return {"repaired": False, "reason": reason, "probe": before, **extra}

    if before.get("reachable"):
        return _refuse("already_reachable")
    if before.get("reason") != "socket_missing":
        # A hung server, a missing binary or an unknown error are not this repair's
        # problem, and signalling on a guess is how a repair becomes an outage.
        return _refuse(f"not_repairable:{before.get('reason')}")
    listeners = before.get("listeners", 0)
    if listeners == 0:
        return _refuse("no_surviving_server")
    if listeners > 1:
        return _refuse("multiple_servers_bound", listener_pids=before.get("listener_pids"))
    pids = [x for x in (before.get("listener_pids") or []) if x]
    if len(pids) != 1:
        return _refuse("server_pid_unresolved")
    pid = pids[0]
    if not is_tmux_server(pid):
        return _refuse("pid_is_not_a_tmux_server", pid=pid,
                       cmdline=_proc_cmdline(pid)[:120])

    plan = {"pid": pid, "socket_path": p, "signal": "SIGUSR1",
            "make_dir": not os.path.isdir(os.path.dirname(p))}
    if dry_run:
        _log("repair_planned", True, "dry_run", plan, conn=conn)
        return {"repaired": False, "reason": "dry_run", "plan": plan, "probe": before}

    # tmux re-binds into an EXISTING directory; the cleaner took the directory too.
    # Created 0700 and owner-only, exactly as tmux itself creates it. Nothing is ever
    # removed here.
    d = os.path.dirname(p)
    if not os.path.isdir(d):
        try:
            os.makedirs(d, mode=0o700, exist_ok=True)
            os.chmod(d, 0o700)
        except Exception as e:  # noqa: BLE001
            return _refuse(f"socket_dir_create_failed:{type(e).__name__}", detail=str(e)[:160])

    try:
        (kill_fn or os.kill)(pid, signal.SIGUSR1)
    except Exception as e:  # noqa: BLE001
        return _refuse(f"signal_failed:{type(e).__name__}", pid=pid, detail=str(e)[:160])

    deadline = time.time() + _REPAIR_WAIT_SECS
    after = None
    while True:
        sleep(0.5)
        after = pf()
        if after.get("reachable") or time.time() >= deadline:
            break
    if not (after or {}).get("reachable"):
        _log("repair_failed", False, "still_unreachable_after_signal",
             {"pid": pid, "after": after}, conn=conn)
        return {"repaired": False, "reason": "still_unreachable_after_signal",
                "pid": pid, "probe": after}

    # PRESERVATION PROOF. Reachable is not enough: if a race started a NEW server, the
    # path would answer while every original session sat orphaned behind it. The socket
    # must lead back to the SAME pid we signalled.
    rc, out, err = run(["display-message", "-p", "#{pid}"])
    served = (out or "").strip()
    if rc != 0 or served != str(pid):
        _log("repair_failed", False, "server_identity_changed",
             {"expected_pid": pid, "serving_pid": served, "rc": rc,
              "err": (err or "")[:160]}, conn=conn)
        return {"repaired": False, "reason": "server_identity_changed",
                "expected_pid": pid, "serving_pid": served, "probe": after}

    _log("repaired", True, "socket_rebound_by_sigusr1",
         {"pid": pid, "socket_path": p, "before": before, "after": after}, conn=conn)
    return {"repaired": True, "reason": "socket_rebound_by_sigusr1", "pid": pid,
            "socket_path": p, "probe": after,
            "sessions_preserved": True, "serving_pid": served}


def guard(*, auto_repair: Optional[bool] = None, emit: bool = True,
          run: Optional[Callable] = None, path: Optional[str] = None,
          emit_fn: Optional[Callable] = None, repair_fn: Optional[Callable] = None,
          probe_fn: Optional[Callable] = None) -> dict:
    """One tick of the control-plane guard: probe, repair when it is the repairable
    class, and make the failure DURABLE either way.

    Emission is what the incident was missing: 100 minutes of blackout produced log
    lines in one service's stdout and nothing the owner could ever be woken by.
    """
    pf = probe_fn or (lambda: probe(run=run, path=path))
    st = pf()
    result = {"probe": st, "repair": None, "event_id": None}
    if st.get("healthy"):
        return result

    if (auto_repair if auto_repair is not None else AUTO_REPAIR) \
            and st.get("reason") == "socket_missing":
        rf = repair_fn or (lambda: repair(run=run, path=path))
        result["repair"] = rf()
        result["probe"] = (result["repair"] or {}).get("probe") or st

    if emit:
        try:
            result["event_id"] = _emit(st, result["repair"], emit_fn=emit_fn)
        except Exception as e:  # noqa: BLE001 — detection must never die on reporting
            result["emit_error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return result


def _emit(st: dict, rep: Optional[dict], emit_fn: Optional[Callable] = None) -> Optional[int]:
    """Durable CTO event. A self-healed blip is recorded but does NOT wake anyone; a
    blackout we could not repair, or a split control plane, is owner-action-required."""
    if emit_fn is None:
        from core.control_plane import cto as _cto
        emit_fn = _cto.emit
    healed = bool((rep or {}).get("repaired"))
    reason = st.get("reason")
    if st.get("split_brain"):
        etype, severity, oar, push = "agent_control_plane_split", "critical", True, None
    elif healed:
        etype, severity, oar, push = "agent_control_plane_recovered", "info", False, False
    else:
        etype, severity, oar, push = "agent_control_plane_unreachable", "critical", True, None
    payload = {"reason": reason, "socket_path": st.get("socket_path"),
               "socket_exists": st.get("socket_exists"), "listeners": st.get("listeners"),
               "listener_pids": st.get("listener_pids"), "detail": st.get("detail")}
    if rep:
        payload["repair"] = {k: rep.get(k) for k in
                             ("repaired", "reason", "pid", "serving_pid")}
    r = emit_fn("tmux_control", etype, severity=severity, owner_action_required=oar,
                payload=payload, push=push,
                # One alert per failure class per half hour: a control plane that is
                # down stays down for many ticks, and re-alerting every 20 s would
                # bury the wake queue the alert has to travel through.
                dedup_key=f"tmux_control:{etype}:{reason}", dedup_window_secs=1800)
    return (r or {}).get("event_id")


def health() -> dict:
    """Read-only surface for the API. `status` is GREEN only on a proven round-trip."""
    st = probe()
    st["status"] = "ok" if st.get("healthy") else "unreachable"
    if st.get("split_brain"):
        st["status"] = "split_brain"
        st["warning"] = (
            f"{st['listeners']} tmux servers are bound to {st['socket_path']}; agents on "
            "the orphaned server(s) are invisible to Owner OS. Not auto-repairable: "
            "resolving it means killing a server, i.e. killing live agents.")
    elif not st.get("reachable"):
        st["warning"] = (
            f"tmux control plane unreachable ({st.get('reason')}) — managed-agent health "
            "is UNKNOWN, not ok. An inventory that could not be read is not an empty one.")
    return st
