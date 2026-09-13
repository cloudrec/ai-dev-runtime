"""Is the ChatGPT -> Owner OS control path alive?

Owner OS has TWO channels to the owner's ChatGPT conversation, and they fail
independently:

  outbound  wake  : companion -> CDP browser -> the chat.       Depends on nobody.
  inbound   control: chat -> nginx :8088 -> seo-backend -> our API :8199.
                     Depends ENTIRELY on the `seo` project's containers.

On 2026-09-13 the second one had been down for four days. Every wake was delivered
(15 of 15) and every one was useless: the supervisor could see the alarm and could not
act on it, because `agent_status` / `agent_read` / `agent_send` never reached this host.
Fourteen wake watches closed as `pane_awaiting_owner` — nobody was coming. Owner OS
itself was healthy throughout and reported itself healthy, which is exactly why nobody
noticed. The owner found out by hitting a 502 in the chat and asking what we had broken.

This module exists so that never happens silently again. It answers ONE question that
`notifications_status` and `observability_summary` do not: **can the supervisor still
reach us?**

Design notes, each paid for by the incident:

* It probes the DEPENDENCY, not ourselves. Our own `/health` answered in 9ms for the
  whole four days. Self-checks cannot see this class of failure.
* It NEVER restarts or mutates the `seo` project. The dependency is another project's
  to run; this only reports. `Exited (137)` on its worker was an OOM kill, so "just
  restart it" is a decision with consequences, and it belongs to the owner.
* It fails CLOSED. An unreachable probe, a timeout, an unreadable store — all report
  `broken` or `unknown`, never `ok`. A monitor that reports healthy when it cannot tell
  is worse than no monitor, because it launders ignorance into reassurance.
* It keeps last-known-good and last-failure, so the answer to "since when?" is data
  rather than someone's memory of when the chat started erroring.
"""
from __future__ import annotations

import os
import time
from typing import Callable, Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

# The chain's public entrance on this host. Probing it exercises nginx AND the backend
# behind it, which is the whole point: either being down breaks the supervisor.
REVERSE_PATH_URL = os.getenv("REVERSE_PATH_PROBE_URL", "http://127.0.0.1:8088/")
PROBE_TIMEOUT_SECS = float(os.getenv("REVERSE_PATH_PROBE_TIMEOUT", "8"))

# Older than this and the stored verdict is not evidence about now. Reported as
# `unknown`, never as `ok` — see the fail-closed note above.
STALE_AFTER_SECS = int(os.getenv("REVERSE_PATH_STALE_SECS", "900"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reverse_path_health (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT, detail TEXT,
    checked_ts REAL, checked_at TEXT,
    last_ok_ts REAL, last_ok_at TEXT,
    last_fail_ts REAL, last_fail_at TEXT, last_error TEXT
)
"""


def _conn(conn=None):
    conn, own = _c(conn)
    conn.execute(_SCHEMA)
    return conn, own


def probe_http(url: str = "", timeout: float = 0.0) -> tuple:
    """(ok, detail). Read-only GET; never raises. Any failure is a closed door."""
    import urllib.error
    import urllib.request
    url = url or REVERSE_PATH_URL
    timeout = timeout or PROBE_TIMEOUT_SECS
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            ms = (time.perf_counter() - t0) * 1000
            code = getattr(r, "status", 0) or 0
            # Any answer at all proves nginx AND its upstream are up; the supervisor's
            # own calls carry auth and hit different routes, so the status code of an
            # unauthenticated GET is not the interesting part. A 5xx is: that is nginx
            # alive with a dead upstream, which is precisely the 502 the owner saw.
            if 500 <= code < 600:
                return False, f"upstream_error:{code} in {ms:.0f}ms"
            return True, f"http_{code} in {ms:.0f}ms"
    except urllib.error.HTTPError as e:            # noqa: PERF203 — distinct meaning
        code = getattr(e, "code", 0) or 0
        if 500 <= code < 600:
            return False, f"upstream_error:{code}"
        return True, f"http_{code}"
    except Exception as e:                          # noqa: BLE001 — closed door
        return False, f"unreachable:{type(e).__name__}"


def record_probe(ok: bool, detail: str, *, conn=None, now: Optional[float] = None) -> dict:
    """Persist one probe result, preserving both last-good and last-failure."""
    now = now if now is not None else now_ts()
    at = now_iso()
    conn, own = _conn(conn)
    try:
        row = conn.execute(
            "SELECT last_ok_ts, last_ok_at, last_fail_ts, last_fail_at, last_error "
            "FROM reverse_path_health WHERE id=1").fetchone()
        prev = dict(zip(("last_ok_ts", "last_ok_at", "last_fail_ts", "last_fail_at",
                         "last_error"), row)) if row else {}
        fields = {
            "state": "ok" if ok else "broken",
            "detail": detail,
            "checked_ts": now, "checked_at": at,
            "last_ok_ts": now if ok else prev.get("last_ok_ts"),
            "last_ok_at": at if ok else prev.get("last_ok_at"),
            "last_fail_ts": prev.get("last_fail_ts") if ok else now,
            "last_fail_at": prev.get("last_fail_at") if ok else at,
            "last_error": prev.get("last_error") if ok else detail,
        }
        conn.execute(
            "INSERT INTO reverse_path_health "
            "(id,state,detail,checked_ts,checked_at,last_ok_ts,last_ok_at,"
            " last_fail_ts,last_fail_at,last_error) VALUES (1,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET state=excluded.state, detail=excluded.detail,"
            " checked_ts=excluded.checked_ts, checked_at=excluded.checked_at,"
            " last_ok_ts=excluded.last_ok_ts, last_ok_at=excluded.last_ok_at,"
            " last_fail_ts=excluded.last_fail_ts, last_fail_at=excluded.last_fail_at,"
            " last_error=excluded.last_error",
            (fields["state"], fields["detail"], fields["checked_ts"], fields["checked_at"],
             fields["last_ok_ts"], fields["last_ok_at"], fields["last_fail_ts"],
             fields["last_fail_at"], fields["last_error"]))
        conn.commit()
        return fields
    finally:
        if own:
            conn.close()


def check(*, probe: Optional[Callable[[], tuple]] = None, conn=None,
          now: Optional[float] = None) -> dict:
    """Run one probe and persist it. `probe` is injectable so tests need no network."""
    now = now if now is not None else now_ts()
    try:
        ok, detail = (probe or probe_http)()
    except Exception as e:                          # noqa: BLE001 — a broken probe is broken
        ok, detail = False, f"probe_raised:{type(e).__name__}"
    record_probe(bool(ok), str(detail), conn=conn, now=now)
    return status(conn=conn, now=now)


def status(*, conn=None, now: Optional[float] = None) -> dict:
    """The reverse path's health, and the evidence for it.

    `state` is one of:
      ok      — the supervisor can reach us; the last probe proved it.
      broken  — it cannot. `depends_on` names what to look at, `last_ok_at` says since
                when, and `remediation` says who fixes it. Owner OS itself may be
                perfectly healthy at the same time, and usually is.
      unknown — no probe, or the last one is older than STALE_AFTER_SECS. NOT ok.
    """
    now = now if now is not None else now_ts()
    # The connection itself is inside the guard. It was outside once, and a test that
    # made the store unreadable proved status() would RAISE rather than report — a
    # monitor whose own failure mode is an exception cannot report the outage it exists
    # for, and the caller sees a traceback instead of "unknown".
    row = None
    conn_local = own = None
    try:
        conn_local, own = _conn(conn)
        row = conn_local.execute(
            "SELECT state,detail,checked_ts,checked_at,last_ok_ts,last_ok_at,"
            "last_fail_ts,last_fail_at,last_error FROM reverse_path_health "
            "WHERE id=1").fetchone()
    except Exception:                               # noqa: BLE001 — closed door
        row = None
    finally:
        if own and conn_local is not None:
            try:
                conn_local.close()
            except Exception:                       # noqa: BLE001
                pass

    base = {
        "channel": "chatgpt_to_owner_os",
        "depends_on": "seo containers (nginx :8088 -> seo-backend -> this API)",
        "probe_url": REVERSE_PATH_URL,
        "checked_at": None, "last_ok_at": None, "last_failure_at": None,
        "last_error": None, "age_secs": None,
    }
    if not row:
        return {**base, "state": "unknown", "healthy": False,
                "reason": "never probed — no evidence either way",
                "remediation": "run reverse_path.check()"}

    (state, detail, checked_ts, checked_at, last_ok_ts, last_ok_at,
     last_fail_ts, last_fail_at, last_error) = row
    age = now - float(checked_ts or 0)
    out = {**base, "detail": detail, "checked_at": checked_at,
           "last_ok_at": last_ok_at, "last_failure_at": last_fail_at,
           "last_error": last_error, "age_secs": round(age, 1)}

    if age > STALE_AFTER_SECS:
        return {**out, "state": "unknown", "healthy": False,
                "reason": f"last probe {int(age)}s old (> {STALE_AFTER_SECS}s) — "
                          f"stale evidence is not evidence",
                "remediation": "check that the reverse-path probe is still running"}
    if state != "ok":
        down_for = (now - float(last_ok_ts)) if last_ok_ts else None
        return {**out, "state": "broken", "healthy": False,
                "reason": f"the supervisor cannot reach Owner OS: {detail}",
                "down_for_secs": round(down_for, 1) if down_for else None,
                # Deliberately NOT "restart it". The worker's last exit was an OOM kill
                # (137); bringing the stack back is a decision with consequences on a
                # host this size, and it is another project's to make.
                "remediation": "the seo containers are the dependency — check them; "
                               "Owner OS core may be healthy and usually is"}
    return {**out, "state": "ok", "healthy": True,
            "reason": "the supervisor can reach Owner OS"}


def summary_line(st: Optional[dict] = None) -> str:
    """One line for a human, naming the dependency rather than blaming Owner OS."""
    st = st or status()
    if st["state"] == "ok":
        return "reverse control path OK — ChatGPT can reach Owner OS"
    if st["state"] == "broken":
        since = st.get("last_ok_at") or "never"
        return (f"reverse control path BROKEN since {since} — ChatGPT cannot control "
                f"agents. Dependency: {st['depends_on']}. Owner OS core is separate.")
    return f"reverse control path UNKNOWN — {st.get('reason')}"


# ── the periodic watch ───────────────────────────────────────────────────────
WATCH_INTERVAL_SECS = int(os.getenv("REVERSE_PATH_WATCH_SECS", "120"))
WATCH_ENABLED = os.getenv("REVERSE_PATH_WATCH_ENABLED", "1") not in ("0", "", "false", "no")


def _is_real_recovery(before: dict) -> bool:
    """Did this `ok` follow something that was actually broken?

    The first probe after a service start goes unknown -> ok, and announcing that as a
    RECOVERY would mean every restart telling the owner something was fixed when nothing
    had broken. A test caught exactly that: a clean run emitted two recoveries.

    A prior `unknown` still counts when a failure is on record with no success after it —
    that is the stale-after-outage case, where the watch was interrupted mid-incident and
    the recovery is genuine news.
    """
    prev = before.get("state")
    if prev == "broken":
        return True
    if prev != "unknown":
        return False
    fail, ok = before.get("last_failure_at"), before.get("last_ok_at")
    return bool(fail) and (not ok or fail > ok)


def watch_once(*, probe: Optional[Callable[[], tuple]] = None, emit_fn=None,
               log=None, conn=None, now: Optional[float] = None) -> dict:
    """One cycle: probe, persist, and speak ONLY when the verdict changes.

    Speaking on change rather than on every tick is not politeness, it is the whole
    design. This condition lasts days — the incident it was written for lasted four —
    so a per-tick alert would be 720 identical messages a day into the owner's Telegram,
    which is the noise problem removed from the stall doctor earlier in the same session.
    One message when it breaks, one when it recovers.

    The alert reaches the owner even while the reverse path is down, because Telegram and
    the wake browser do not route through `seo`. That asymmetry is the reason this is
    worth emitting at all rather than only logging.
    """
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        before = status(conn=conn, now=now)
        st = check(probe=probe, conn=conn, now=now)
        changed = before.get("state") != st.get("state")
        if changed and emit_fn is not None:
            if st["state"] == "broken":
                emit_fn(
                    "reverse_path", "reverse_path_broken", severity="critical",
                    owner_action_required=True,
                    payload={"channel": st["channel"], "depends_on": st["depends_on"],
                             "detail": st.get("detail"), "last_ok_at": st.get("last_ok_at"),
                             "probe_url": st["probe_url"]},
                    action_taken=summary_line(st)[:400],
                    dedup_key="reverse_path_broken", dedup_window_secs=86400,
                    # inbox + owner push, but NEVER through the path being reported on
                    conn=conn)
            elif st["state"] == "ok" and _is_real_recovery(before):
                emit_fn(
                    "reverse_path", "reverse_path_recovered", severity="info",
                    owner_action_required=False,
                    payload={"channel": st["channel"],
                             "was_down_since": before.get("last_ok_at"),
                             "detail": st.get("detail")},
                    action_taken=summary_line(st)[:400],
                    dedup_key="reverse_path_recovered", dedup_window_secs=3600,
                    conn=conn)
        if changed and log is not None:
            log("warning" if st["state"] != "ok" else "info", summary_line(st))
        return {**st, "changed": changed, "previous_state": before.get("state")}
    finally:
        if own:
            conn.close()


async def watch_loop(log=None, emit_fn=None, sleep=None) -> None:
    """Background watch. Started from `api/main.py` alongside the other loops."""
    import asyncio
    log = log or (lambda level, msg: None)
    sleep = sleep or asyncio.sleep
    if not WATCH_ENABLED:
        log("info", "reverse path watch disabled (REVERSE_PATH_WATCH_ENABLED=0)")
        return
    log("info", f"reverse path watch started (every {WATCH_INTERVAL_SECS}s, "
                f"probing {REVERSE_PATH_URL})")
    while True:
        try:
            # to_thread: the probe is a blocking socket read and this shares a process
            # with the MCP control path. Two worker ticks were found on the event loop
            # earlier in this same session; this one does not join them.
            await asyncio.to_thread(watch_once, emit_fn=emit_fn, log=log)
        except Exception as e:  # noqa: BLE001 — a watcher must never kill the daemon
            log("warning", f"reverse path watch error: {type(e).__name__}: {e}")
        await sleep(WATCH_INTERVAL_SECS)
