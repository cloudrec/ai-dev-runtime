"""Wake companion — submits ONE fixed phrase into the active control chat.

It asks the bridge whether a wake is pending and does exactly what it is told. It makes no
decisions, reads no page content, parses no assistant output, and knows no conversation URL
of its own: the target comes from the rotatable pointer at submission time, so a rebind
between decision and delivery goes to the new chat rather than a stale one.

The ONLY text it can ever type is `wake_bridge.WAKE_PHRASE`, and the ONLY way it can type
it is `cdp_composer.submit_phrase`, which navigates the tab to the BOUND conversation and
verifies delivery structurally. There is deliberately no synthetic-keyboard fallback: that
path typed into whatever window happened to be focused, which is exactly how a phrase could
land in the wrong chat, and it could never verify a keystroke arrived.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, "/root/ai-dev-runtime")
sys.path.insert(0, "/root/ai-dev-runtime/tools")

POLL_SECS = int(os.getenv("COMPANION_POLL_SECS", "20"))
# Inventory the ChatGPT tabs every N ticks (default ~5 minutes at 20s polls).
DISCOVERY_EVERY_TICKS = int(os.getenv("COMPANION_DISCOVERY_TICKS", "15"))
CDP_LIST_URL = os.getenv("COMPANION_CDP_LIST_URL", "http://127.0.0.1:9222/json/list")
# The control-plane guard runs BEFORE the agent watch, every tick: if the tmux socket has
# been lost, the repair happens in time for this same tick's inventory instead of a
# blackout that lasts until a human notices (2026-08-30: 100 minutes).
CONTROL_GUARD_ENABLED = os.getenv("COMPANION_CONTROL_GUARD", "1") not in ("0", "false", "no")
# The NATIVE supervisor: continue managed agents from Claude Code's own lifecycle hooks
# instead of from a scraped pane. Runs alongside the tmux watchers, which stay as the
# fallback for crashes, silent failures and any case where a hook never fires.
NATIVE_SUPERVISOR_ENABLED = os.getenv("COMPANION_NATIVE_SUPERVISOR", "1") not in ("0", "false", "no")


def discover_chats() -> dict:
    """Inventory the ChatGPT conversations visible as browser tabs.

    Reads ONLY the CDP page list (URL + title) — the same surface the delivery path
    already touches; no history, nothing outside chatgpt.com. Discovered chats are
    upserted into the chat registry; a route is auto-bound only when
    `chat_registry.consider_auto_bind` finds deterministic evidence, and every refusal
    reason is its answer, not an exception.
    """
    import json as _json
    import urllib.request
    from core import chat_registry as cr
    try:
        with urllib.request.urlopen(CDP_LIST_URL, timeout=8) as r:
            tabs = _json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"cdp_list_unavailable:{type(e).__name__}"}
    res = cr.discover_from_tabs(tabs)
    candidates = [(d["conversation"],
                   next((t.get("title") or "" for t in tabs
                         if (t.get("url") or "").split("?")[0].rstrip("/") ==
                         d["conversation"]), ""))
                  for d in res.get("discovered", [])]
    # Account-visible surface: the sidebar of the logged-in page lists conversations the
    # ACCOUNT has, including chats created on the owner's other devices that never had a
    # server tab. Read-only DOM scan; failure is an answer, never a crash.
    sidebar = {"ok": False, "reason": "not_attempted", "conversations": []}
    try:
        from cdp_composer import list_sidebar_conversations
        sidebar = list_sidebar_conversations()
    except Exception as e:  # noqa: BLE001
        sidebar = {"ok": False, "reason": f"cdp_unavailable:{type(e).__name__}",
                   "conversations": []}
    if sidebar.get("ok"):
        side = cr.discover_from_links(sidebar["conversations"])
        candidates += [(d["conversation"], d.get("title") or "")
                       for d in side.get("discovered", [])]
    for conversation, title in candidates:
        b = cr.consider_auto_bind(conversation, title=title)
        if b.get("bound"):
            kind = "auto-rebound (continuation)" if b.get("continuation") else "auto-bound"
            print(f"{kind} route {b['route_key']} -> {b['conversation']}", flush=True)
    return {"ok": True, "discovered": [c for c, _ in candidates],
            "sidebar": sidebar.get("ok"), "sidebar_reason": sidebar.get("reason")}


def tick(wb) -> dict:
    """One poll: ask the bridge, deliver if told to, acknowledge ONLY on verified delivery.

    Split from the loop so the behaviour is testable. Returns what happened, always with a
    reason — the old text claimed "no browser window" for every failure, including a plain
    cooldown refusal, which sent three investigations the wrong way.
    """
    p = wb.pending_wake()
    if not p.get("pending"):
        if p.get("reason") == "retry_backoff_pending":
            lane = "transient composer" if p.get("transient_retry") else "standard"
            print(f"event {p.get('event_id')} benched ({lane} backoff, "
                  f"attempt {p.get('attempt')}, next retry in "
                  f"{p.get('next_retry_in_secs')}s)", flush=True)
        return {"acted": False, "reason": p.get("reason", "nothing_pending")}
    # The conversation used is the one the bridge resolved THIS tick from the rotatable
    # pointer. It is never cached across ticks, so a rebind takes effect on the next poll.
    res = {"ok": False, "reason": "not_attempted"}
    try:
        from cdp_composer import submit_phrase
        res = submit_phrase(p["conversation"], p["phrase"],
                            source="companion", event_id=p["event_id"],
                            actionable=bool(p.get("actionable")),
                            route_key=p.get("route_key", ""))
        ok = bool(res.get("ok"))
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "reason": f"cdp_unavailable:{type(e).__name__}"}
        ok = False
    if ok:
        # Acknowledge ONLY on verified delivery. Anything else leaves the wake pending,
        # which is what makes a failed submission retryable instead of silently consumed.
        wb.acknowledge(p["event_id"])
        if p.get("actionable"):
            # SLO watchdog (task 211): start tracking THIS confirmed real user turn so
            # a re-wake/escalation fires if the agent never moves. Best-effort — the
            # companion's core loop must never break because of the watchdog.
            try:
                from core import closed_loop_wake
                closed_loop_wake.register_delivery(
                    event_id=p["event_id"], target=p.get("agent_id", ""),
                    project_id=p.get("route_key", ""),
                    event_type=p.get("event_type", ""))
            except Exception:  # noqa: BLE001
                pass
        print(f"delivered wake for event {p['event_id']} "
              f"[route {p.get('route_key', '?')}] to {p['conversation']}: "
              f"{res.get('reason')}", flush=True)
    else:
        print(f"not delivered for event {p['event_id']}; stays pending "
              f"({res.get('reason')})", flush=True)
    return {"acted": True, "ok": ok, "event_id": p["event_id"],
            "conversation": p["conversation"], "route_key": p.get("route_key", ""),
            "reason": res.get("reason")}


AGENT_WATCH_ENABLED = os.getenv("AGENT_WATCH_ENABLED", "1") not in ("0", "", "false")
RUNTIME_WATCH_ENABLED = os.getenv("RUNTIME_WATCH_ENABLED", "1") not in ("0", "", "false")
# The runtime watchdog is a minute-scale concern like agent watch, but jobs move
# slower than panes; every 3rd tick (~60s) is plenty and keeps DB churn down.
RUNTIME_WATCH_EVERY_TICKS = int(os.getenv("RUNTIME_WATCH_EVERY_TICKS", "3"))


def watch_agents() -> dict:
    """One agent-watch pass: live tmux agents -> CTO events for owner-relevant
    transitions. Runs at the poll cadence (~20s) because a pane sitting at a permission
    prompt is a minute-scale problem, not a five-minute one."""
    from core import agent_watch
    r = agent_watch.scan()
    for e in r.get("emitted", []):
        print(f"agent-watch: {e['class']} {e['target']} "
              f"[{e['project'] or 'unmapped->owner-os'}] event {e['event_id']}",
              flush=True)
    return r


STALL_DOCTOR_ENABLED = os.getenv("STALL_DOCTOR_ENABLED", "1") not in ("0", "", "false")
CLOSED_LOOP_WATCH_ENABLED = os.getenv("CLOSED_LOOP_WATCH_ENABLED", "1") not in (
    "0", "", "false")


def watch_closed_loop() -> dict:
    """One SLO-watchdog pass: a wake that was delivered (real user turn confirmed) but
    produced no observed progress gets re-woken once, then escalated. Task 211 — the
    part of the closed loop neither the bridge nor the doctor covers alone."""
    from core import closed_loop_wake
    r = closed_loop_wake.slo_scan()
    for e in r.get("deregistered", []):
        print(f"closed-loop-watch: deregistered {e['target'] or '(no target)'} "
              f"for event {e['event_id']} — {e['reason']}", flush=True)
    for e in r.get("rewoken", []):
        print(f"closed-loop-watch: re-woke {e['target']} for stalled event "
              f"{e['event_id']} (new event {e.get('rewoken_event_id')})", flush=True)
    for e in r.get("escalated", []):
        print(f"closed-loop-watch: escalated {e['target']} — no progress after "
              f"re-wake (event {e['event_id']} -> {e.get('escalated_event_id')})",
              flush=True)
    return r


def doctor_agents() -> dict:
    """One stall-doctor pass: at-rest wait shapes -> safe auto-continuation or a
    genuine owner escalation. The owner must never have to read terminals to
    find a pane holding its own next instruction."""
    from core import stall_doctor
    r = stall_doctor.scan()
    for e in r.get("acted", []):
        print(f"stall-doctor: {e['action']} {e['target']} ({e['shape']}) "
              f"delivered={e.get('delivered', '-')}", flush=True)
    return r


def watch_runtime() -> dict:
    """One runtime pass: stalled-job detection (evidence-based, deduped) plus the
    bounded supervisor recovery sweep. Runs HERE, outside the ai-runtime service
    process, precisely so a dead or wedged service cannot take its own watchdog
    down with it — the job store on disk stays readable either way."""
    from core import runtime_supervisor, runtime_watchdog
    r = runtime_watchdog.scan()
    for e in r.get("emitted", []):
        print(f"runtime-watch: {e['reason']} job {e['job_id'][:8]} "
              f"({e['status']}) event {e['event_id']}", flush=True)
    try:
        s = runtime_supervisor.scan()
        for e in s.get("retried", []):
            print(f"runtime-supervisor: retried job {e['job_id'][:8]} "
                  f"as {e.get('retry_job_id', '')[:8]} ({e.get('class')})", flush=True)
    except Exception as e:  # noqa: BLE001 — recovery must never break detection
        print(f"runtime-supervisor error: {str(e)[:160]}", flush=True)
    return r


def watch_control_plane() -> dict:
    """Probe the tmux control plane, repair a deleted socket, and make either outcome
    durable.

    This runs first in the tick because everything after it — the agent watch, the stall
    doctor, the closed-loop watch — reads the fleet THROUGH tmux. On 2026-08-30 that
    transport was gone for 100 minutes and the only trace was this process's stdout:
    no event, no wake, and a managed-agent health surface still reporting ok.
    """
    from core import tmux_control as tc
    r = tc.guard()
    st = r.get("probe") or {}
    if not st.get("healthy"):
        rep = r.get("repair") or {}
        print(f"control-plane: {st.get('reason')} socket={st.get('socket_path')} "
              f"listeners={st.get('listeners')} pids={st.get('listener_pids')} "
              f"repair={rep.get('reason') or 'not_attempted'} "
              f"event={r.get('event_id')}", flush=True)
    return r


def supervise_native() -> dict:
    """Continue agents from native lifecycle events. ChatGPT is not in this path.

    Everything it may do is bounded inside `native_supervisor.scan`: an allowlisted
    target, resolved to exactly one live pane, still at rest, with nothing staged in its
    composer, past its own floor, and only the fail-closed safe step. Anything else is a
    durable refusal.
    """
    from core import native_supervisor as ns
    r = ns.scan()
    for a in r.get("acted", []):
        print(f"native-supervisor: continued {a['target']} from event {a['event_id']} "
              f"(key {a['idempotency_key']}, agent_created={a.get('agent_created')})",
              flush=True)
    return r


def main() -> None:
    from core import wake_bridge as wb
    # Announce this process and the code it started with, so a deploy that
    # restarts the API but not this companion is DETECTABLE instead of silently
    # delivering to the wrong chat with stale routing logic.
    try:
        wb.register_worker("wake_companion")
    except Exception as e:  # noqa: BLE001
        print(f"worker registration failed: {str(e)[:120]}", flush=True)
    n = 0
    while True:
        try:
            tick(wb)
            if CONTROL_GUARD_ENABLED:
                try:
                    watch_control_plane()
                except Exception as e:  # noqa: BLE001 — the guard never breaks the loop
                    print(f"control-plane guard error: {str(e)[:160]}", flush=True)
            if NATIVE_SUPERVISOR_ENABLED:
                try:
                    supervise_native()
                except Exception as e:  # noqa: BLE001 — never breaks the tick
                    print(f"native-supervisor error: {str(e)[:160]}", flush=True)
            if AGENT_WATCH_ENABLED:
                try:
                    watch_agents()
                except Exception as e:  # noqa: BLE001
                    print(f"agent-watch error: {str(e)[:160]}", flush=True)
            if RUNTIME_WATCH_ENABLED and n % RUNTIME_WATCH_EVERY_TICKS == 0:
                try:
                    watch_runtime()
                except Exception as e:  # noqa: BLE001
                    print(f"runtime-watch error: {str(e)[:160]}", flush=True)
            if STALL_DOCTOR_ENABLED and n % RUNTIME_WATCH_EVERY_TICKS == 0:
                try:
                    doctor_agents()
                except Exception as e:  # noqa: BLE001
                    print(f"stall-doctor error: {str(e)[:160]}", flush=True)
            if CLOSED_LOOP_WATCH_ENABLED and n % RUNTIME_WATCH_EVERY_TICKS == 0:
                try:
                    watch_closed_loop()
                except Exception as e:  # noqa: BLE001
                    print(f"closed-loop-watch error: {str(e)[:160]}", flush=True)
            if n % DISCOVERY_EVERY_TICKS == 0:
                discover_chats()
        except Exception as e:  # noqa: BLE001
            print(f"companion error: {str(e)[:160]}", flush=True)
        n += 1
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
