"""Stall Doctor — the supervisor layer that ends "the owner had to look at the
terminal and ping each chat" (live acceptance incident 2026-08-15 ~12:27).

Three at-rest wait shapes, each with a distinct safe policy:

  LOST_CONTINUATION   an actionable instruction is already QUEUED in the
                      composer while the agent sits waiting_input/idle
                      (gaika-video: 'Proceed with the opening fix…' — nobody
                      pressed Enter). Doctor re-submits the owner's OWN line
                      (Enter only, never authors content) when it is
                      provenance-safe; otherwise escalates.
  CHILD_WORKFLOW_WAIT the pane says it waits for a child workflow and the
                      child's progress counter is visible (jobhunter:
                      'Waiting for 1 dynamic workflow… ◯ fable-second-pass-
                      audit 38/71'). While the counter MOVES the doctor is
                      silent; a static child past its SLO gets a safe
                      diagnose-and-continue nudge.
  INTERNAL_WAIT       the pane waits on an internal gate/test/build result
                      (payorch: 'Waiting for gate results before pushing').
                      NOT automatically an owner decision: the doctor nudges
                      the agent to check its own gate and continue the safe
                      local path. Only a wait naming an owner/approval/prod
                      power escalates.

Fail-closed rules, in order of authority:
  * a pane in waiting_owner or showing a permission dialog is agent_watch's
    domain — the doctor never touches it and never answers dialogs;
  * ANY observed progress (bottom-region digest moved, child counter moved,
    working state) resets the episode — a busy agent is never nudged;
  * authored nudge texts must pass the continuation-watchdog allowlist
    classifier (is_safe_continuation) — they are composed from its closed
    vocabulary and verified at runtime anyway;
  * a queued line is re-submitted only when ASCII-evaluable, not a dialog
    answer, and free of the destructive/live/credential denylist — the exact
    lessons of the 2026-08-03 auto-Enter incident. Anything else escalates.
  * loop guard: at most MAX_ACTIONS_PER_EPISODE per (target, digest) episode,
    with ACTION_COOLDOWN_SECS between actions; every action is ledgered
    durably and audited as a CTO event.

Delivery uses the continuation watchdog's hardened deliver_and_verify (lease,
five-proof verification, one bounded Enter retry) — no new transport.
"""
from __future__ import annotations

import os
import re
from typing import Callable, Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

ENABLED = os.getenv("STALL_DOCTOR_ENABLED", "1") not in ("0", "", "false", "no")
# Comma list of targets, or "all". The owner's 2026-08-15 instruction makes
# universal safe continuation the default; "" disables actuation (observe-only).
ACTUATE = os.getenv("STALL_DOCTOR_ACTUATE", "all")
WAIT_SLO_SECS = int(os.getenv("STALL_DOCTOR_SLO_SECS", "300"))
# A queued line at rest is EXPLICIT intent already sitting in the composer; the
# 2026-08-15 second gaika-video incident showed 300s + tick cadence reads as
# "10+ minutes of nothing" to the owner. Re-submitting the owner's own line is
# cheap and loop-guarded, so it earns a tighter clock.
QUEUED_SLO_SECS = int(os.getenv("STALL_DOCTOR_QUEUED_SLO_SECS", "120"))
CHILD_SLO_SECS = int(os.getenv("STALL_DOCTOR_CHILD_SLO_SECS", "900"))
ACTION_COOLDOWN_SECS = int(os.getenv("STALL_DOCTOR_COOLDOWN_SECS", "1800"))
# A delivery that FAILED verification proves nothing about the pane; waiting the
# full success-cooldown before the bounded retry (gaika-presentation, 2026-08-15
# 10:28, verify=False then 30 idle minutes) just stretches the stall. Failed
# actions retry on a short clock; the loop guard still caps total attempts.
FAILED_RETRY_COOLDOWN_SECS = int(os.getenv("STALL_DOCTOR_FAILED_RETRY_SECS", "180"))
MAX_ACTIONS_PER_EPISODE = int(os.getenv("STALL_DOCTOR_MAX_ACTIONS", "2"))
# 2026-08-28 gaika-server incident: `may_submit_queued` proves the QUEUED TEXT is
# content-safe, never that it was actually put there by the owner — there is no
# durable record anywhere in this codebase of Owner OS itself staging text into a
# composer (confirmed: agent_send/agent_answer always paste+Enter atomically; the
# one staging primitive, DirectPaneController.replace_pending(submit=False), has
# no production caller). A genuine owner-staged instruction is a rare, one-off
# event per target; Claude Code's own dim "suggested next input" redraw is not —
# it regenerates fresh (different) text after every turn, and MAX_ACTIONS_PER_
# EPISODE never catches it because a new digest each cycle starts a brand-new
# episode with its own zeroed action counter. This is the cross-episode backstop:
# count actual submit_queued DELIVERIES for this target over a rolling window,
# regardless of digest/episode; once the rate is implausible for a human pacing
# real instructions, refuse and escalate instead of guessing at content.
LOST_CONTINUATION_SUBMIT_WINDOW_SECS = int(
    os.getenv("STALL_DOCTOR_LC_SUBMIT_WINDOW_SECS", "3600"))
LOST_CONTINUATION_MAX_SUBMITS_PER_WINDOW = int(
    os.getenv("STALL_DOCTOR_LC_MAX_SUBMITS", "3"))

LOST_CONTINUATION = "LOST_CONTINUATION"
CHILD_WORKFLOW_WAIT = "CHILD_WORKFLOW_WAIT"
INTERNAL_WAIT = "INTERNAL_WAIT"
OWNER_WAIT = "OWNER_WAIT"
# task 211, regression (e): an internal wait whose text NAMES an owner power (prod,
# merge, approval, ...) with no live dialog on screen. This is NOT agent_watch's domain
# (there is no menu/prompt to see) and it is NOT a safe-nudge internal wait either — it
# is a genuine decision only the owner (via ChatGPT) can make. Kept distinct from
# OWNER_WAIT, which stays reserved for an actual dialog/menu on screen.
OWNER_DECISION_WAIT = "OWNER_DECISION_WAIT"
NONE = "NONE"

_CHILD_WAIT_RE = re.compile(
    r"waiting for \d+ (?:dynamic )?workflows?|workflow to finish", re.I)
_CHILD_PROGRESS_RE = re.compile(r"[◯◉]\s*(\S+)\s+(\d+)\s*/\s*(\d+)")
_INTERNAL_WAIT_RE = re.compile(
    r"waiting (?:for|on) (?:the )?(?:gate|test|ci|suite|build|pipeline|results?|"
    r"checks?|verification|scan)", re.I)
# a wait that NAMES an owner power is a real owner decision, not an internal wait
_OWNER_POWER_RE = re.compile(
    r"(owner|approval|approve|permission|decision|разреш|подтверд|прод\b|"
    r"production|\bprod\b|merge|\bdns\b|secret|payment|deploy)", re.I)

# provenance gate for re-submitting an ALREADY-QUEUED line (Enter only).
# EVALUABLE means every character belongs to a script the denylists can read:
# ASCII plus Cyrillic (the owner writes Russian; cw._FORBIDDEN_RE carries the
# Russian destructive stems, so Russian IS evaluable — the second gaika-video
# incident, 2026-08-15, was a benign Russian instruction dead-ending into an
# escalation purely because of the old blanket non-ASCII refusal). CJK or any
# other script stays unevaluable → escalate, never submit.
_EVALUABLE_RE = re.compile(r"^[\x09\x0a\x0d\x20-\x7eЀ-ӿ«»—–…·№]*$")
_DIALOG_ANSWER_RE = re.compile(
    r"^\s*(\d{1,2}[.)]?|y|yes|n|no|ok|okay|да|нет|д|н)\s*[.!]?\s*$", re.I)
# Supplementary stems the watchdog denylist lacks; merge is an owner power.
_EXTRA_FORBIDDEN_RE = re.compile(
    r"(\bmerge\b|мерж|мердж|\bубей|убить|останов(и|ите)\b|выключ|запуш|пушни"
    r"|\bоплат|платеж|платёж|бюджет|потрать|спиши)", re.I)

# authored nudges — composed strictly from the watchdog's closed safe vocabulary
NUDGE_INTERNAL = ("Continue: check the test suite checks and proceed with the "
                  "next safe step only.")
NUDGE_CHILD = ("Continue: check the running task progress and proceed with the "
               "next safe step.")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stall_doctor_state (
    target TEXT PRIMARY KEY,
    shape TEXT, digest TEXT, first_ts REAL,
    actions INTEGER DEFAULT 0, last_action_ts REAL, escalated INTEGER DEFAULT 0,
    last_action_ok INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS stall_doctor_action (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT, shape TEXT, action TEXT, digest TEXT,
    delivered INTEGER, detail TEXT, at TEXT, ts REAL
);
-- 2026-08-28 gaika-server incident: LOST_CONTINUATION auto-submitted Claude
-- Code's own dim "suggested next input" redraw as if it were a real queued
-- owner instruction, in a self-feeding loop (submit -> agent responds ->
-- new suggestion redraws -> submit again). A per-target pause is a durable,
-- reversible, SCOPED safety guard — every other target's behaviour is
-- completely unaffected, and no restart is needed to set or clear it.
CREATE TABLE IF NOT EXISTS stall_doctor_pause (
    target TEXT PRIMARY KEY, reason TEXT, at TEXT, ts REAL, paused_by TEXT
)
"""


def _conn(conn=None):
    conn, own = _c(conn)
    for stmt in _SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    try:  # pre-column deployments carry the old shape
        conn.execute("ALTER TABLE stall_doctor_state ADD COLUMN "
                     "last_action_ok INTEGER DEFAULT 1")
    except Exception:  # noqa: BLE001
        pass
    return conn, own


def may_submit_queued(text: str) -> tuple[bool, str]:
    """Provenance gate for pressing Enter on an already-typed line."""
    from core.agent_continuation_watchdog import _FORBIDDEN_RE
    t = (text or "").strip()
    if not t:
        return False, "empty"
    if len(t) > 400:
        return False, "too_long_to_evaluate"
    if not _EVALUABLE_RE.match(t):
        return False, "unevaluable_script"
    if _DIALOG_ANSWER_RE.match(t):
        return False, "would_answer_a_dialog"
    if _FORBIDDEN_RE.search(t) or _EXTRA_FORBIDDEN_RE.search(t):
        return False, "forbidden_token"
    return True, "queued_line_provenance_safe"


def classify_wait(tail: str, *, state: str = "", pending: str = "") -> dict:
    """(pane tail, inventory state, queued input) -> wait shape + evidence."""
    from core import agent_watch
    st = (state or "").strip()
    region = agent_watch._bottom_region(tail)
    raw = tail or ""
    if st == "waiting_owner" or agent_watch._OWNER_PROMPT_RE.search(region) \
            or agent_watch._MENU_RE.search(region):
        return {"shape": OWNER_WAIT, "evidence": "owner_prompt_or_menu"}
    if st in ("working", "shell_running"):
        return {"shape": NONE, "evidence": f"inventory_state_{st}"}
    child = None
    m = _CHILD_PROGRESS_RE.search(raw)
    if m:
        child = {"name": m.group(1), "done": int(m.group(2)), "total": int(m.group(3))}
    if _CHILD_WAIT_RE.search(raw):
        return {"shape": CHILD_WORKFLOW_WAIT, "child": child,
                "evidence": "child_workflow_wait_text"}
    if (pending or "").strip():
        return {"shape": LOST_CONTINUATION, "evidence": "queued_input_at_rest"}
    m2 = _INTERNAL_WAIT_RE.search(region)
    if m2:
        if _OWNER_POWER_RE.search(region):
            return {"shape": OWNER_DECISION_WAIT,
                    "evidence": "wait_names_owner_power"}
        return {"shape": INTERNAL_WAIT, "evidence": f"internal_wait:{m2.group(0)[:40]}"}
    return {"shape": NONE, "evidence": "no_wait_signal"}


def _digest(tail: str, child: Optional[dict]) -> str:
    """Episode identity. The child counter is deliberately INCLUDED raw (not
    digit-stripped): a moving counter must read as progress, not as the same
    episode."""
    from core import agent_watch
    base = agent_watch.digest_of(agent_watch._bottom_region(tail))
    if child:
        base += f":{child['name']}:{child['done']}/{child['total']}"
    return base


def decide(shape: str, *, pending: str = "", age_secs: float = 0.0,
           actions_used: int = 0, since_last_action: float = 1e9,
           child: Optional[dict] = None, last_action_ok: bool = True,
           recent_lc_submits: int = 0) -> dict:
    """Pure policy. Returns {action, reason} — fully testable.

    `recent_lc_submits`: how many submit_queued deliveries this target has
    already had for LOST_CONTINUATION within LOST_CONTINUATION_SUBMIT_WINDOW_SECS,
    counted ACROSS episodes/digests (unlike `actions_used`, which is per-episode
    and resets whenever the queued text changes). The caller computes this from
    the durable stall_doctor_action log; it is the one loop shape MAX_ACTIONS_
    PER_EPISODE cannot see."""
    if shape in (NONE, OWNER_WAIT):
        return {"action": "none",
                "reason": "not_doctor_domain" if shape == OWNER_WAIT else "no_wait"}
    slo = (CHILD_SLO_SECS if shape == CHILD_WORKFLOW_WAIT
           else QUEUED_SLO_SECS if shape == LOST_CONTINUATION
           else WAIT_SLO_SECS)
    if age_secs < slo:
        return {"action": "none", "reason": f"within_slo_{slo}s"}
    if shape == OWNER_DECISION_WAIT:
        # Never a nudge candidate — there is no safe local step for a wait that names an
        # owner power, so this goes straight to escalation the moment the SLO elapses.
        # scan() guards re-escalation via its own `escalated` flag, so this is safe to
        # return on every subsequent pass without a loop-guard/cooldown of its own.
        return {"action": "escalate", "reason": "internal_wait_names_owner_power"}
    if actions_used >= MAX_ACTIONS_PER_EPISODE:
        return {"action": "escalate", "reason": "loop_guard_exhausted"}
    cooldown = ACTION_COOLDOWN_SECS if last_action_ok else FAILED_RETRY_COOLDOWN_SECS
    if since_last_action < cooldown:
        return {"action": "none",
                "reason": "action_cooldown" if last_action_ok
                else "failed_action_cooldown"}
    if shape == LOST_CONTINUATION:
        if recent_lc_submits >= LOST_CONTINUATION_MAX_SUBMITS_PER_WINDOW:
            return {"action": "escalate",
                   "reason": f"lost_continuation_submit_rate_exceeded:{recent_lc_submits}"}
        ok, why = may_submit_queued(pending)
        if ok:
            return {"action": "submit_queued", "reason": why}
        return {"action": "escalate", "reason": f"queued_line_not_submittable:{why}"}
    if shape == CHILD_WORKFLOW_WAIT:
        return {"action": "nudge", "text": NUDGE_CHILD,
                "reason": "child_workflow_static_past_slo"}
    return {"action": "nudge", "text": NUDGE_INTERNAL,
            "reason": "internal_wait_past_slo"}


def _actuation_allowed(target: str) -> bool:
    a = (ACTUATE or "").strip()
    if not a:
        return False
    if a.lower() == "all":
        return True
    return target in {t.strip() for t in a.split(",") if t.strip()}


# ── per-target pause: a scoped, reversible override independent of ACTUATE ───
def pause_target(target: str, reason: str, *, by: str = "owner", conn=None,
                 now: Optional[float] = None) -> None:
    """Exclude exactly ONE target from every stall_doctor action — read-only
    observation continues (the target still shows up in `skipped`), only
    actuation stops. Durable (a DB row, not an env var), so it takes effect and
    reverses without a service restart, and it never touches ACTUATE or any
    other target's behaviour."""
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        conn.execute(
            "INSERT INTO stall_doctor_pause (target,reason,at,ts,paused_by) VALUES (?,?,?,?,?) "
            "ON CONFLICT(target) DO UPDATE SET reason=excluded.reason, at=excluded.at, "
            "ts=excluded.ts, paused_by=excluded.paused_by",
            (target, reason, now_iso(), now, by))
        conn.commit()
    finally:
        if own:
            conn.close()


def resume_target(target: str, conn=None) -> bool:
    """Reverse `pause_target`. Returns whether a pause actually existed."""
    conn, own = _conn(conn)
    try:
        cur = conn.execute("DELETE FROM stall_doctor_pause WHERE target=?", (target,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        if own:
            conn.close()


def in_declared_external_wait(target: str, *, conn=None) -> bool:
    """Is this target under a LIVE external-wait declaration?

    Delegates to `native_supervisor`, deliberately rather than re-reading the table, so the
    two paths cannot drift apart on what "declared" means. Fail-open toward doing the normal
    thing: if the lookup is unavailable, the doctor behaves exactly as before.

    An EXPIRED declaration is absent by design, and that is a real limit worth naming: a wait
    on an unbounded natural event outlives its own TTL, after which nothing here suppresses
    the blocker. That is what happened in event 16836 — the declaration lapsed at 01:24:02Z
    while the auction close it was waiting for had not occurred — and it is a policy question
    about TTL semantics, not something this check can decide.
    """
    try:
        from core import native_supervisor as _ns
        return bool(_ns.in_external_wait(target, conn=conn))
    except Exception:  # noqa: BLE001 — unknown never means "suppress"
        return False


def is_paused(target: str, conn=None) -> bool:
    conn, own = _conn(conn)
    try:
        return conn.execute(
            "SELECT 1 FROM stall_doctor_pause WHERE target=?", (target,)
        ).fetchone() is not None
    finally:
        if own:
            conn.close()


def paused_targets(conn=None) -> list:
    """Every currently paused target, for observability — never guessed from logs."""
    conn, own = _conn(conn)
    try:
        rows = conn.execute(
            "SELECT target, reason, at, paused_by FROM stall_doctor_pause "
            "ORDER BY ts").fetchall()
        return [{"target": r[0], "reason": r[1], "at": r[2], "paused_by": r[3]}
                for r in rows]
    finally:
        if own:
            conn.close()


def _deliver(target: str, cwd: str, *, action: str, text: str) -> dict:
    """Hardened transport: lease + deliver_and_verify (five proofs, one bounded
    Enter retry). `submit` presses Enter on the existing line; `deliver` sends
    an authored nudge that must pass the allowlist classifier — verified here
    at runtime, not only at authoring time."""
    from core import agent_continuation_watchdog as cw
    from core.control_plane import api as cp
    if action == "deliver" and not cw.is_safe_continuation(text):
        return {"ok": False, "reason": "nudge_failed_safety_classifier"}
    lease = cp.acquire_lease(f"agent:{target}", "stall_doctor", ttl_secs=120)
    try:
        ctrl = cw.Controller()
        out = cw.deliver_and_verify(ctrl, target=target, cwd=cwd, action=action,
                                    step_text=text, expected_pending=text)
        ok = bool((out.get("verify") or {}).get("ok"))
        return {"ok": ok, "verify": out.get("verify")}
    finally:
        try:
            cp.release_lease(f"agent:{target}", (lease or {}).get("lease_id"))
        except Exception:  # noqa: BLE001
            pass


def scan(*, agents: Optional[list] = None, read_fn: Optional[Callable] = None,
         pending_fn: Optional[Callable] = None, deliver_fn: Optional[Callable] = None,
         emit_fn: Optional[Callable] = None, conn=None,
         now: Optional[float] = None) -> dict:
    """One doctor pass over the live agents. Injectable for tests."""
    now = now if now is not None else now_ts()
    if not ENABLED:
        return {"acted": [], "skipped": [], "reason": "doctor_disabled"}
    if agents is None:
        from core import agent_control
        agents = [a for a in agent_control.agent_list().get("agents", [])
                  if a.get("is_agent") and a.get("alive")]
    if read_fn is None:
        from core import agent_control

        def read_fn(target):  # noqa: F811
            return agent_control.agent_read(target, 60).get("output", "")
    if pending_fn is None:
        from core import agent_control

        def pending_fn(target, tail, cwd):  # noqa: F811
            return agent_control.pending_input_text(target, tail, cwd=cwd) or ""
    if deliver_fn is None:
        deliver_fn = _deliver
    if emit_fn is None:
        from core.control_plane.cto import emit as emit_fn  # noqa: F811

    conn, own = _conn(conn)
    try:
        acted, skipped, pruned = [], [], []
        # Rows for panes that vanished entirely (closed/renamed, never sent NONE/
        # OWNER_WAIT to clear their own row) never get revisited by the loop below —
        # it only walks the CURRENT live `agents` list. Prune them here so state
        # doesn't accumulate forever (payorch-sbp-resumed / payorch-fresh-sonnet,
        # 2026-08-16: rows survived a full day after their panes were gone).
        # Skip only when the live list itself is empty — that's more likely a
        # transient agent_list() hiccup than every pane vanishing at once, and
        # wiping every row on that ambiguity is not a safe bet.
        live_targets = {a.get("target") for a in agents if a.get("target")}
        if live_targets:
            for (t, escalated) in conn.execute(
                    "SELECT target, escalated FROM stall_doctor_state").fetchall():
                if t in live_targets:
                    continue
                if escalated:
                    _retire_stale_escalation(conn, t, now, why="pane_vanished")
                conn.execute("DELETE FROM stall_doctor_state WHERE target=?", (t,))
                self_log(conn, t, "", "prune_vanished", "", True,
                         "pane no longer in live agent list", now)
                conn.commit()
                pruned.append(t)
        for a in agents:
            target = a.get("target") or ""
            if not target:
                continue
            if is_paused(target, conn=conn):
                skipped.append({"target": target, "why": "paused"})
                continue
            if in_declared_external_wait(target, conn=conn):
                # A pane parked on a declared external wait is waiting BY DESIGN. The
                # native supervisor has honoured that since the Auction case; this path
                # never consulted it, so the two disagreed about the same declared state
                # and the doctor could still raise a blocker for an agent everyone else
                # had agreed was parked (event 16836, diamond-auction).
                skipped.append({"target": target, "why": "intentional_external_wait"})
                continue
            cwd = a.get("claude_cwd") or a.get("cwd") or ""
            try:
                tail = read_fn(target)
            except Exception:  # noqa: BLE001
                skipped.append({"target": target, "why": "unreadable"})
                continue
            pending = ""
            try:
                pending = pending_fn(target, tail, cwd)
            except Exception:  # noqa: BLE001
                pass
            c = classify_wait(tail, state=a.get("state", ""), pending=pending)
            shape = c["shape"]
            dg = _digest(tail, c.get("child"))
            row = conn.execute(
                "SELECT shape, digest, first_ts, actions, last_action_ts, escalated,"
                " last_action_ok FROM stall_doctor_state WHERE target=?",
                (target,)).fetchone()
            if shape in (NONE, OWNER_WAIT):
                if row and row[5]:
                    _retire_stale_escalation(conn, target, now,
                                             why=f"shape_now_{shape}")
                conn.execute("DELETE FROM stall_doctor_state WHERE target=?", (target,))
                conn.commit()
                skipped.append({"target": target, "why": f"shape_{shape}"})
                continue
            if row and row[0] == shape and row[1] == dg:
                first_ts, actions, last_ts, escalated = row[2], row[3], row[4] or 0, row[5]
                last_ok = bool(row[6] if row[6] is not None else 1)
            else:
                # new episode OR progress (digest moved — a moving child counter
                # lands here every time it ticks, resetting the clock)
                if row and row[5]:
                    # the escalated episode resolved itself; its owner ping is stale
                    _retire_stale_escalation(conn, target, now, why="episode_moved")
                first_ts, actions, last_ts, escalated = now, 0, 0, 0
                last_ok = True
                conn.execute(
                    "INSERT INTO stall_doctor_state (target, shape, digest, first_ts,"
                    " actions, last_action_ts, escalated, last_action_ok)"
                    " VALUES (?,?,?,?,0,0,0,1)"
                    " ON CONFLICT(target) DO UPDATE SET shape=excluded.shape,"
                    " digest=excluded.digest, first_ts=excluded.first_ts, actions=0,"
                    " last_action_ts=0, escalated=0, last_action_ok=1",
                    (target, shape, dg, now))
                conn.commit()
            recent_lc_submits = 0
            if shape == LOST_CONTINUATION:
                recent_lc_submits = conn.execute(
                    "SELECT COUNT(*) FROM stall_doctor_action WHERE target=? "
                    "AND action='submit_queued' AND delivered=1 AND ts > ?",
                    (target, now - LOST_CONTINUATION_SUBMIT_WINDOW_SECS)).fetchone()[0]
            d = decide(shape, pending=pending, age_secs=now - first_ts,
                       actions_used=actions, since_last_action=now - (last_ts or 0),
                       child=c.get("child"), last_action_ok=last_ok,
                       recent_lc_submits=recent_lc_submits)
            if d["action"] == "none":
                skipped.append({"target": target, "shape": shape, "why": d["reason"]})
                continue
            project = _project_of(a, conn)
            if d["action"] == "escalate":
                if escalated:
                    skipped.append({"target": target, "shape": shape,
                                    "why": "already_escalated"})
                    continue
                # OWNER_DECISION_WAIT names a real owner power (deploy/merge/prod/...) —
                # a distinct event TYPE from a plain blocked-pane ping, so the wake text
                # and any downstream routing can tell "needs a decision" apart from
                # "needs a response". Every other escalate shape keeps the original type.
                etype = ("owner_decision_required" if shape == OWNER_DECISION_WAIT
                         else "agent_waiting_input")
                ev = emit_fn(
                    "stall_doctor", etype, project_id=project,
                    agent_id=target, severity="high", owner_action_required=True,
                    payload={"target": target, "shape": shape, "reason": d["reason"],
                             "pending": (pending or "")[:200], "digest": dg},
                    action_taken=(f"{target}: {shape} needs the owner — {d['reason']} "
                                  f"({(pending or '')[:80]})"),
                    correlation_id=f"waiting:{target}",
                    dedup_key=f"doctor:{target}:{shape}:{dg}",
                    dedup_window_secs=86400, conn=conn)
                conn.execute("UPDATE stall_doctor_state SET escalated=1 WHERE target=?",
                             (target,))
                self_log(conn, target, shape, "escalate", dg, False, d["reason"], now)
                conn.commit()
                acted.append({"target": target, "shape": shape, "action": "escalate",
                              "event_id": (ev or {}).get("event_id")})
                continue
            if not _actuation_allowed(target):
                skipped.append({"target": target, "shape": shape,
                                "why": "actuation_not_allowed"})
                continue
            if d["action"] == "submit_queued":
                res = deliver_fn(target, cwd, action="submit", text=pending)
            else:
                res = deliver_fn(target, cwd, action="deliver", text=d["text"])
            ok = bool(res.get("ok"))
            conn.execute(
                "UPDATE stall_doctor_state SET actions=actions+1, last_action_ts=?,"
                " last_action_ok=? WHERE target=?", (now, int(ok), target))
            self_log(conn, target, shape, d["action"], dg, ok,
                     f"{d['reason']}; verify={res.get('reason') or ok}", now)
            emit_fn(
                "stall_doctor", "stall_doctor_action", project_id=project,
                agent_id=target, severity="info", owner_action_required=False,
                payload={"target": target, "shape": shape, "action": d["action"],
                         "delivered": ok, "reason": d["reason"], "digest": dg,
                         "pending": (pending or "")[:120]},
                action_taken=(f"doctor {d['action']} on {target} ({shape}): "
                              f"delivered={ok}"),
                correlation_id=f"doctor:{target}",
                dedup_key=f"doctor:{target}:{shape}:{dg}:{d['action']}:{int(now)}",
                dedup_window_secs=60, conn=conn)
            conn.commit()
            acted.append({"target": target, "shape": shape, "action": d["action"],
                          "delivered": ok})
        return {"acted": acted, "skipped": skipped, "agents_seen": len(agents),
                "pruned": pruned}
    finally:
        if own:
            conn.close()


def _retire_stale_escalation(conn, target: str, now: float, *, why: str) -> list:
    """Retire doctor escalations contradicted by the episode resolving on its
    own (pane working again, or the composer/digest moved on). Same overlay
    discipline as agent_watch crash reconciliation: the event row is never
    touched — mark_invalid + wake acknowledge, so the owner stops being paged
    for a stall that no longer exists (chemmy-fast, 2026-08-15 17:10, resumed
    minutes after its escalation and the actionable ping stayed live)."""
    retired = []
    try:
        from core import agent_watch, wake_bridge
        agent_watch._conn(conn)  # ensure the invalid-overlay table exists
        rows = conn.execute(
            "SELECT e.id FROM event e LEFT JOIN agent_alert_invalid i "
            "ON i.event_id = e.id WHERE e.source='stall_doctor' "
            "AND e.type IN ('agent_waiting_input','owner_decision_required') "
            "AND e.agent_id=? "
            "AND i.event_id IS NULL ORDER BY e.id DESC LIMIT 5", (target,)).fetchall()
        for (eid,) in rows:
            agent_watch.mark_invalid(
                eid, reason=f"stall episode resolved ({why}) at {now_iso()} — "
                            "pane moved on without the owner",
                by="stall_doctor", conn=conn, now=now)
            try:
                wake_bridge.acknowledge(eid, conn=conn, now=now)
            except Exception:  # noqa: BLE001
                pass
            self_log(conn, target, "", "retire_escalation", "", True,
                     f"{why}; event={eid}", now)
            retired.append(eid)
    except Exception:  # noqa: BLE001 — reconciliation must never break the sweep
        pass
    return retired


def self_log(conn, target, shape, action, digest, delivered, detail, now) -> None:
    conn.execute(
        "INSERT INTO stall_doctor_action (target, shape, action, digest, delivered,"
        " detail, at, ts) VALUES (?,?,?,?,?,?,?,?)",
        (target, shape, action, digest, int(bool(delivered)), (detail or "")[:300],
         now_iso(), now))


def _project_of(agent: dict, conn) -> str:
    try:
        from core import agent_watch
        return agent_watch.project_for(agent, conn=conn)
    except Exception:  # noqa: BLE001
        return ""
