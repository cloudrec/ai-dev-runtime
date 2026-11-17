"""Watch the spec -> agent -> ChatGPT loop itself, and say so when it has stopped.

Every failure of 2026-11-09 was silent: a worker unit without the wake switch (seven gaika
questions never reached the chat for an hour), a tmux server that did not exist yet (no
agent ever started), a dropped Enter. Each time the work stood still until the owner asked.

`check()` looks at facts only (the review inbox, the wake bridge's own records, the units,
the workers' logs), restarts a stopped worker (at most once per RESTART_GAP), and tells the
owner one Telegram line per problem: when it starts, every REMIND_SECS while it lasts, and
when it is over. It never types into an agent or a chat.
"""
from __future__ import annotations

import json
import subprocess
import time
from typing import Callable, Optional

from core.control_plane.api import _c

UNWOKEN_AFTER = 600          # a review this old with no wake decision at all
UNDELIVERED_AFTER = 1200     # a decided wake not delivered for this long
ERROR_WINDOW = 600
ERROR_STREAK = 10
REMIND_SECS = 7200
RESTART_GAP = 1800
WORKERS = ("owner-os-fullcycle-intake", "owner-os-fullcycle-worker")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS loop_watch (
    key TEXT PRIMARY KEY, first_ts REAL, alert_ts REAL, active INTEGER, text TEXT
);
CREATE TABLE IF NOT EXISTS loop_watch_restart (
    unit TEXT PRIMARY KEY, ts REAL
)"""


def _conn(conn=None):
    conn, own = _c(conn)
    for stmt in _SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    return conn, own


def _has(conn, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                             (table,)).fetchone())


# ── the checks: each returns {key: text} for what is wrong right now ──────────────────
def unwoken_reviews(conn, now: float) -> dict:
    """A review the tech lead must answer whose event never got a wake decision: the
    emitting process had the bridge off (the 2026-11-09 failure)."""
    if not (_has(conn, "fc_review") and _has(conn, "wake_audit")):
        return {}
    rows = conn.execute(
        "SELECT r.id, r.project_id, r.event_id, r.created_ts FROM fc_review r "
        "WHERE r.status='pending' AND r.decision IS NULL AND r.event_id IS NOT NULL "
        "AND r.created_ts < ? AND NOT EXISTS (SELECT 1 FROM wake_audit w "
        "WHERE w.event_id = r.event_id)", (now - UNWOKEN_AFTER,)).fetchall()
    if not rows:
        return {}
    projects = sorted({r[1] for r in rows})
    return {"unwoken_reviews": (
        f"{len(rows)} ревью ждут техлида, но уведомление в ChatGPT по ним не создавалось "
        f"(проекты: {', '.join(projects[:6])}). Проверь WAKE_BRIDGE_ENABLED у воркера.")}


def undelivered_wakes(conn, now: float) -> dict:
    """A wake the bridge decided to send that has not reached the chat for too long."""
    if not _has(conn, "wake_audit"):
        return {}
    # exactly what pending_wake would still offer: a wake already fired (wake_submitted) is
    # latched and never offered again, and a wake for an agent outside the canary filter
    # (WAKE_BRIDGE_ONLY_AGENTS) is passed over by design, so neither is waiting for delivery
    from core import wake_bridge as wb
    latched = ("AND NOT EXISTS (SELECT 1 FROM wake_submitted s WHERE s.event_id=a.event_id) "
               if _has(conn, "wake_submitted") else "")
    rows = conn.execute(
        "SELECT a.ts, COALESCE(a.agent_id,''), COALESCE(a.project_id,''), "
        "COALESCE(a.offer_identity,'') FROM wake_audit a "
        "WHERE a.decision='wake' AND "
        "COALESCE(a.acknowledged,0)=0 AND a.superseded_by IS NULL " + latched +
        "AND a.ts < ?", (now - UNDELIVERED_AFTER,)).fetchall()
    # the same predicate pending_wake applies, route resolution included
    from core import wake_routes
    only, mode = wb.only_agents(), wb.offer_mode()
    panes = wb.pane_cwds() if mode == "nonpayment" else {}
    pay_chats = wb.payment_conversations(conn)
    waiting = []
    for ts, agent, proj, ident in rows:
        try:
            resolved = wake_routes.resolve(project_id=proj, agent_id=agent, conn=conn)
        except Exception:  # noqa: BLE001
            continue
        if resolved.get("bound"):
            ok = wb.offerable(conn, agent, proj, ident, resolved, only, panes, mode,
                              pay_chats)
        else:               # unrouted: stuck too, if it would be offered at all
            ok = ident != wb.ID_PAYMENT and not wb.payment_identity(agent, proj) and \
                wb.offer_allowed(agent, proj, "", only, panes, mode, ident)
        if ok:
            waiting.append(ts)
    r = (min(waiting), len(waiting)) if waiting else None
    if not r or not r[1]:
        return {}
    last = None
    if _has(conn, "wake_delivery"):
        last = conn.execute("SELECT reason FROM wake_delivery ORDER BY id DESC LIMIT 1"
                            ).fetchone()
    return {"undelivered_wakes": (
        f"{r[1]} уведомлений для ChatGPT не доставлены, старшему {int((now - r[0]) // 60)} мин"
        + (f"; последняя попытка: {str(last[0])[:80]}" if last else "") + ".")}


def _unit_active(unit: str, run=subprocess.run) -> Optional[bool]:
    try:
        p = run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() == "active"


def workers_down(conn, now: float, run=subprocess.run) -> dict:
    out = {}
    for unit in WORKERS:
        if _unit_active(unit, run) is not False:
            continue
        last = conn.execute("SELECT ts FROM loop_watch_restart WHERE unit=?", (unit,)).fetchone()
        restarted = ""
        if not last or now - (last[0] or 0) >= RESTART_GAP:
            conn.execute("INSERT OR REPLACE INTO loop_watch_restart VALUES (?,?)", (unit, now))
            conn.commit()
            try:
                run(["systemctl", "restart", unit], capture_output=True, timeout=60)
                restarted = " Перезапустил."
            except (OSError, subprocess.SubprocessError):
                restarted = " Перезапуск не удался."
        out[f"down:{unit}"] = f"Служба {unit} остановлена.{restarted}"
    return out


def worker_errors(conn, now: float, run=subprocess.run) -> dict:
    out = {}
    for unit in WORKERS:
        try:
            p = run(["journalctl", "-u", unit, "--since", f"-{ERROR_WINDOW}s", "--no-pager",
                     "-o", "cat"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        errs = []
        for line in p.stdout.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("what") == "error":
                errs.append(str(d.get("error", ""))[:120])
        if len(errs) >= ERROR_STREAK:
            top = max(set(errs), key=errs.count)
            out[f"errors:{unit}"] = (f"{unit}: {len(errs)} ошибок за {ERROR_WINDOW // 60} мин, "
                                     f"чаще всего: {top}")
    return out


CHECKS = (unwoken_reviews, undelivered_wakes, workers_down, worker_errors)


def owner_digest(conn, notify, now: float, sent: list) -> Optional[str]:
    """What waits for the owner, sent whenever that list changes (and not more often than
    every DIGEST_GAP), with the owner page link. Returns the text sent, if any."""
    from core.fullcycle import owner_page
    _conn(conn)                                  # its own table, also when called alone
    items = owner_page.todo(conn)
    keys = ",".join(sorted(i["key"] for i in items))
    row = conn.execute("SELECT alert_ts, text FROM loop_watch WHERE key='digest'").fetchone()
    if row and row[1] == keys:
        return None
    if row and now - (row[0] or 0) < DIGEST_GAP:
        return None
    if not items and not row:
        return None
    link = owner_page.url()
    text = ("📋 Owner OS, нужно от тебя:\n" + "\n".join(f"• {i['text']}" for i in items)
            if items else "📋 Owner OS: от тебя сейчас ничего не нужно.")
    if link:
        text += f"\nСтраница: {link}"
    if _send(notify, text, sent):
        conn.execute("INSERT OR REPLACE INTO loop_watch VALUES (?,?,?,?,?)",
                     ("digest", now, now, 0, keys))
        conn.commit()
        return text
    return None


DIGEST_GAP = 600


def check(*, notify: Callable[[str], object], conn=None, now: Optional[float] = None,
          checks=None, run=subprocess.run) -> dict:
    """One pass. Returns {"problems": {...}, "sent": [...]}."""
    now = now if now is not None else time.time()
    conn, own = _conn(conn)
    sent = []
    try:
        problems: dict = {}
        for fn in checks or CHECKS:
            try:
                if fn in (workers_down, worker_errors):
                    problems.update(fn(conn, now, run))
                else:
                    problems.update(fn(conn, now))
            except Exception as e:  # noqa: BLE001 — one broken check must not hide the rest
                problems[f"check_failed:{fn.__name__}"] = f"проверка {fn.__name__} упала: {e}"[:200]
        known = {r[0]: r for r in conn.execute(
            "SELECT key, first_ts, alert_ts, active, text FROM loop_watch")}
        for key, text in problems.items():
            row = known.get(key)
            if row and row[3] and now - (row[2] or 0) < REMIND_SECS:
                continue
            first = row[1] if row and row[3] else now
            msg = ("⚠️ Owner OS: " if not (row and row[3]) else "⚠️ Owner OS, всё ещё: ") + text
            if _send(notify, msg, sent):        # unsent: asked again on the next pass
                conn.execute("INSERT OR REPLACE INTO loop_watch VALUES (?,?,?,?,?)",
                             (key, first, now, 1, text[:500]))
        for key, row in known.items():
            if row[3] and key not in problems:
                if _send(notify, f"✅ Owner OS: прошло — {str(row[4])[:200]}", sent):
                    conn.execute("UPDATE loop_watch SET active=0 WHERE key=?", (key,))
        conn.commit()
        if checks is None:
            try:
                owner_digest(conn, notify, now, sent)
            except Exception as e:  # noqa: BLE001 — the digest never stops the watch
                problems["check_failed:owner_digest"] = f"сводка упала: {e}"[:200]
        return {"problems": problems, "sent": sent}
    finally:
        if own:
            conn.close()


def _send(notify, msg: str, sent: list) -> bool:
    """True when the line left. _send_owner_push reports failure as (False, ...)."""
    try:
        r = notify(msg)
    except Exception:  # noqa: BLE001
        return False
    if isinstance(r, tuple) and r and r[0] is False:
        return False
    sent.append(msg)
    return True
