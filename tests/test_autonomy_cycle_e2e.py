"""The autonomy loop end to end, in a sandbox, through the companion's real delivery path.

    owner spec → dispatch → agent → checks → review event → wake decision →
    route resolution → companion tick → composer submit + verification → ack →
    reviewer decision → next task (continuation) → … → project done → watchdog quiet
    plus: agent stage_done doorbell → wake → delivery; failure → retry; companion restart;
    duplicate doorbells and duplicate decisions.

test_fullcycle_e2e.py already proves the engine loop, but it records the browser leg by
hand (`wb.record_delivery`). Here the leg is the production code: tools/wake_companion.tick
→ tools/cdp_composer.submit_phrase (claim, latch, verify, record) → wb.acknowledge, with
wake_routes resolving the chat per project.

REAL: temp control-plane / agent-control DBs (conftest + fixture), a private tmux socket,
the fake agent, task contracts, git in the fixture repo, wake_bridge, wake_routes,
cdp_composer, wake_companion.tick, tools/stage_done.main, closed_loop_wake.slo_scan.
MOCKED: the ChatGPT page (a fake CDP session; nothing is typed into a browser), the GPT
reviewer (it answers only reviews whose wake was verifiably delivered), the LLM agent.
No live DB, pane, chat, Telegram or production job is touched.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys

import pytest

from core import task_contract as tc
from core import wake_bridge as wb
from core import wake_routes as wr
from core.control_plane import api as cp
from core.fullcycle import engine as fe
from core.fullcycle import spec as specs

_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "tests", "fixtures", "fullcycle_demo"))
import harness as h  # noqa: E402


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# The fake CDP session the delivery-verification tests use; it also loads cdp_composer
# from THIS checkout into sys.modules, which is where the companion imports it from.
_twdv = _load("_twdv_for_cycle", "tests/test_wake_delivery_verification.py")
cdp = _twdv.cdp
_FakePage = _twdv._S

pytestmark = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")

PROJECT_CHAT = "https://chatgpt.com/c/sandbox-tinycalc-chat"
OWNEROS_CHAT = "https://chatgpt.com/c/sandbox-owneros-chat"


class _Browser:
    """The ChatGPT side: a fake page per submission. `fail_next` makes the next N
    submissions fail before anything is typed (send never enabled), which leaves the
    wake unlatched and retryable."""

    def __init__(self, monkeypatch):
        self.submissions = []          # (conversation, phrase)
        self.fail_next = 0
        self.latch_only = 0
        self._turns = 0
        self._asst = 0
        # Sandbox wall: the composer's only ways to the real browser are `_http` (CDP
        # HTTP on 127.0.0.1:9222) and websocket connects. Both raise here, so no code
        # path in this file can open, close or type into a live tab.
        self.live_attempts = []        # the composer swallows errors; the fixture checks this

        def _no_live_browser(*a, **k):
            self.live_attempts.append(repr(a)[:200])
            raise AssertionError("sandbox: live CDP access attempted")
        monkeypatch.setattr(cdp, "_http", _no_live_browser)
        import websocket
        monkeypatch.setattr(websocket, "create_connection", _no_live_browser)
        # ...and process-wide, so even another copy of the composer module (other test
        # files load their own) cannot reach the browser's debugging port.
        import urllib.request
        real_urlopen = urllib.request.urlopen

        def _guarded_urlopen(req, *a, **k):
            url = getattr(req, "full_url", req)
            if ":9222" in str(url):
                self.live_attempts.append(str(url)[:200])
                raise AssertionError(f"sandbox: live CDP access attempted ({url})")
            return real_urlopen(req, *a, **k)
        monkeypatch.setattr(urllib.request, "urlopen", _guarded_urlopen)
        monkeypatch.setattr(cdp, "browser_degraded", self._degraded)
        monkeypatch.setattr(cdp, "page_responsive", lambda t, timeout=8.0: True)
        monkeypatch.setattr(cdp, "find_target", self._find)
        monkeypatch.setattr(cdp, "_Session", self._session)
        self._url = ""

    def _find(self, url):
        self._url = url
        return {"webSocketDebuggerUrl": "ws://sandbox"}

    def _degraded(self, *a, **k):
        # A pre-send failure: the browser is refused before anything is typed or
        # claimed, so the wake must stay pending and be retried.
        if self.fail_next:
            self.fail_next -= 1
            return {"degraded": True, "reason": "too_many_pages:13"}
        return {"degraded": False}

    def _session(self, ws):
        if self.latch_only:
            # The page TOOK the phrase (composer cleared) but no user turn ever renders.
            self.latch_only -= 1
            page = _FakePage([True, True, True, True], [self._turns] * 40)
            orig = page.call

            def call(method, params=None):
                if method == "Input.insertText":
                    self.submissions.append((self._url, (params or {}).get("text")))
                return orig(method, params)
            page.call = call
            return page
        page = _FakePage([True, True, True, True], [self._turns, self._turns + 1])
        self._turns += 1
        self._asst += 1
        orig, orig_count = page.call, page.count

        state = {"answered": False}

        def count(selector):
            # the reply arrives whole: no streaming; one more assistant turn once our
            # message was sent (the composer's start + settle checks see a real answer)
            if selector == cdp.STREAMING_SEL:
                return 0
            if selector == cdp.ASSISTANT_TURN_SEL:
                return self._asst + (1 if state["answered"] else 0)
            return orig_count(selector)
        page.count = count

        def call(method, params=None):
            if method == "Input.insertText":
                self.submissions.append((self._url, (params or {}).get("text")))
                state["answered"] = True   # ChatGPT answers the turn we just sent
            return orig(method, params)
        page.call = call
        return page


@pytest.fixture
def cycle(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "0")
    # no owner push from the sandbox, whatever the parent environment holds
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    # sandbox pacing: no global cooldown or backoff between the test's own wakes
    for knob in ("COOLDOWN_SECS", "ACTIONABLE_COOLDOWN_SECS", "RETRY_BACKOFF_SECS",
                 "TRANSIENT_RETRY_BACKOFF_SECS"):
        monkeypatch.setattr(wb, knob, 0)
    import time as _time
    real_sleep = _time.sleep
    monkeypatch.setattr(cdp.time, "sleep", lambda *_a, **_k: None)
    assert wr.bind_route("owner-os", OWNEROS_CHAT, by="test")["ok"]
    assert wr.bind_route("tinycalc", PROJECT_CHAT, by="test")["ok"]
    # The companion does `from cdp_composer import submit_phrase`; other test files
    # replace sys.modules["cdp_composer"] with their own copy at collection time, so pin
    # the patched one here or the companion would drive an unpatched composer.
    monkeypatch.setitem(sys.modules, "cdp_composer", cdp)
    browser = _Browser(monkeypatch)
    companion = _load("wake_companion", "tools/wake_companion.py")
    root = h.make_project(str(tmp_path))
    specs.create_project("tinycalc", name="tinycalc", root=root, executor="fixture")
    specs.put_spec("tinycalc", h.load_spec(), by="test")
    a, log = h.make_adapter(str(tmp_path), root, {})
    engine = fe.Engine("tinycalc", a, max_attempts=3, holder="sandbox")
    yield {"engine": engine, "adapter": a, "log": log, "root": root, "browser": browser,
           "companion": companion, "tmp": tmp_path, "sleep": real_sleep}
    a.kill()
    assert browser.live_attempts == [], browser.live_attempts


def _deliveries(eid=None):
    conn, _ = cp._c(None)
    try:
        q = "SELECT event_id, delivered, reason FROM wake_delivery"
        rows = conn.execute(q + (" WHERE event_id=?" if eid else "") + " ORDER BY id",
                            (eid,) if eid else ()).fetchall()
    finally:
        conn.close()
    return rows


def _woken_reviewer(c, seen, *, next_tasks=None):
    """One reviewer turn: deliver whatever the bridge has pending through the companion,
    then answer only the reviews whose wake was VERIFIABLY delivered."""
    r = c["companion"].tick(wb)
    delivered = {r["event_id"]} if r.get("acted") and r.get("ok") else set()
    for rv in fe.pending_reviews("tinycalc"):
        if rv["event_id"] not in delivered and rv["event_id"] not in seen["delivered"]:
            continue
        seen["delivered"].add(rv["event_id"])
        key = rv["payload"].get("task") or rv["task_id"].split(":", 1)[1]
        fe.ack_review(rv["review_id"], by="sandbox-gpt")
        choice = "add_tasks" if rv["kind"] == "project" else "approve"
        res = fe.submit_decision(rv["review_id"], choice, key=f"gpt:{rv['review_id']}",
                                 by="sandbox-gpt",
                                 next_tasks=next_tasks if choice == "add_tasks" else None)
        seen["decisions"].append((key, rv["kind"], choice, res.get("ok"), rv["review_id"]))
    return r


def _run_cycle(c, seen, *, until, timeout=60, next_tasks=None):
    import time
    end = time.time() + timeout
    while time.time() < end:
        c["engine"].tick()
        _woken_reviewer(c, seen, next_tasks=next_tasks)
        st = c["engine"].status()
        if until(st):
            return st
        c["sleep"](0.15)
    conn, _ = cp._c(None)
    dbg = conn.execute("SELECT id, kind, decision, applied FROM fc_review").fetchall()
    ev = conn.execute("SELECT id, type, substr(payload,1,160) FROM event ORDER BY id").fetchall()
    conn.close()
    raise AssertionError(f"timeout: {c['engine'].status()} seen={seen} reviews={dbg} ev={ev}")


# ── the whole loop through the real delivery path ───────────────────────────
def test_spec_to_done_every_review_reaches_the_project_chat_verified(cycle):
    c = cycle
    seen = {"delivered": set(), "decisions": []}
    st = _run_cycle(c, seen, until=lambda s: s["done"], next_tasks=[h.CLI_TASK])

    assert [(t["key"], t["state"]) for t in st["tasks"]] == [
        ("add", tc.VERIFIED_DONE), ("mul", tc.VERIFIED_DONE), ("cli", tc.VERIFIED_DONE)]
    # continuation: each next task was decided only after its predecessor's wake landed
    assert [(k, ch) for k, _, ch, ok, _ in seen["decisions"] if ok] == [
        ("add", "approve"), ("mul", "approve"), ("__project__", "add_tasks"), ("cli", "approve")]
    # every wake went to the PROJECT's chat (route resolution), none to owner-os
    urls = {u for u, _ in c["browser"].submissions}
    assert urls == {PROJECT_CHAT}, urls
    # every reviewed event was delivered exactly once, verified, and then acknowledged
    for eid in seen["delivered"]:
        rows = _deliveries(eid)
        assert [d for _, d, _ in rows] == [1], (eid, rows)
    assert not wb.pending_wake().get("pending")
    # one prompt per task reached the agent: no stale or duplicate wake re-dispatched
    assert [x["task_id"] for x in h.agent_log(c["log"])] == [
        "tinycalc:add", "tinycalc:mul", "tinycalc:cli"]


def test_a_failed_delivery_is_retried_and_the_review_waits_for_it(cycle):
    c = cycle
    c["browser"].fail_next = 2
    seen = {"delivered": set(), "decisions": []}
    h.wait_for(lambda: c["engine"].tick() and fe.pending_reviews("tinycalc"), 20, step=0.15)
    (rv,) = fe.pending_reviews("tinycalc")
    eid = rv["event_id"]

    r1 = _woken_reviewer(c, seen)
    r2 = _woken_reviewer(c, seen)
    assert (r1.get("ok"), r2.get("ok")) == (False, False), (r1, r2, _deliveries())
    assert seen["decisions"] == []                 # nobody acted on an undelivered wake
    assert wb.pending_wake()["event_id"] == eid    # still pending, not consumed
    r3 = _woken_reviewer(c, seen)
    assert r3["ok"] and r3["event_id"] == eid, [x.get("reason") for x in (r1, r2, r3)]
    assert [d for _, d, _ in _deliveries(eid)] == [0, 0, 1]
    assert seen["decisions"][0][:3] == ("add", "review", "approve")


def test_a_restarted_companion_never_redelivers_an_acknowledged_wake(cycle):
    c = cycle
    seen = {"delivered": set(), "decisions": []}
    h.wait_for(lambda: c["engine"].tick() and fe.pending_reviews("tinycalc"), 20, step=0.15)
    assert _woken_reviewer(c, seen)["ok"]
    n = len(c["browser"].submissions)
    restarted = _load("wake_companion", "tools/wake_companion.py")   # a fresh process
    for _ in range(3):
        assert restarted.tick(wb)["acted"] is False
    assert len(c["browser"].submissions) == n
    # the reviewer answering the same review again (it was woken twice) is harmless
    rid = seen["decisions"][0][4]
    again = fe.submit_decision(rid, "approve", key=f"gpt:{rid}", by="sandbox-gpt")
    other = fe.submit_decision(rid, "reject", key=f"gpt:{rid}:2", by="sandbox-gpt",
                               feedback="second thoughts")
    assert again.get("duplicate") and other.get("reason") == "review_already_decided"


def test_a_taken_but_unverified_wake_is_never_resent_and_its_watch_is_bounded(cycle):
    """The page took the phrase (latched) but no user turn rendered. Resending would
    duplicate the turn; instead the SLO watchdog re-wakes ONCE, escalates ONCE, then
    stays quiet — no unbounded loop."""
    from core import closed_loop_wake as clw
    from core.control_plane.store import now_ts
    c = cycle
    c["browser"].latch_only = 1
    seen = {"delivered": set(), "decisions": []}
    h.wait_for(lambda: c["engine"].tick() and fe.pending_reviews("tinycalc"), 20, step=0.15)
    eid = fe.pending_reviews("tinycalc")[0]["event_id"]
    r = _woken_reviewer(c, seen)
    assert r["ok"] is False and r["event_id"] == eid
    assert seen["decisions"] == []
    sent = len(c["browser"].submissions)
    for _ in range(3):                                   # latched: never typed again
        c["companion"].tick(wb)
    assert len(c["browser"].submissions) == sent == 1
    emitted = []

    def emit(source, type_, **kw):
        emitted.append(type_)
        return {"event_id": 10_000 + len(emitted)}
    t0 = now_ts()
    for hours in (2, 4, 6, 8, 24):
        clw.slo_scan(now=t0 + hours * 3600, emit_fn=emit)
    conn, _ = cp._c(None)
    watch = conn.execute("SELECT event_id, rewoken, escalated FROM wake_loop_watch").fetchall()
    conn.close()
    assert watch == [(eid, 1, 1)]
    # exactly one re-wake and one escalation over 24h of scans, then silence
    assert emitted == ["wake_loop_no_progress", "wake_loop_stalled"], emitted


# ── the agent's doorbell ────────────────────────────────────────────────────
def _stage_done(c, monkeypatch, *argv):
    monkeypatch.setenv("OWNEROS_AGENT_TARGET", "sandbox-agent:0.0")
    monkeypatch.setenv("AGENT_AUTOMATION_TARGETS", "sandbox-agent")
    monkeypatch.delenv("TMUX_PANE", raising=False)
    sd = _load("stage_done_sandbox", "tools/stage_done.py")
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = sd.main(["--cwd", c["root"], *argv])
    return rc, json.loads(buf.getvalue().strip().splitlines()[-1])


def test_stage_done_wakes_the_tech_lead_once_and_is_delivered_verified(cycle, monkeypatch):
    c = cycle
    rc, out = _stage_done(c, monkeypatch, "--summary", "stage 1 done", "--ref", "b@abc",
                          "--tests", "3 passed", "--next", "review abc")
    assert rc == 0 and out["ok"], out
    assert out["wake"]["decision"] == "wake", out
    eid = out["event_id"]
    p = wb.pending_wake()
    assert p["pending"] and p["event_id"] == eid
    r = c["companion"].tick(wb)
    assert r["ok"] and r["event_id"] == eid
    assert r["conversation"] in (PROJECT_CHAT, OWNEROS_CHAT)
    assert [d for _, d, _ in _deliveries(eid)] == [1]
    # the same doorbell again (agent retried the command) does not re-wake
    rc2, out2 = _stage_done(c, monkeypatch, "--summary", "stage 1 done", "--ref", "b@abc",
                            "--tests", "3 passed", "--next", "review abc")
    assert rc2 == 0
    assert not wb.pending_wake().get("pending")
    assert c["companion"].tick(wb)["acted"] is False
    assert len([s for s in c["browser"].submissions if f"event={eid}" in (s[1] or "")]) == 1


def test_stage_done_refuses_an_agent_without_autonomy(cycle, monkeypatch):
    c = cycle
    monkeypatch.setenv("OWNEROS_AGENT_TARGET", "unpermitted-agent:0.0")
    monkeypatch.setenv("AGENT_AUTOMATION_TARGETS", "someone-else")
    sd = _load("stage_done_sandbox", "tools/stage_done.py")
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = sd.main(["--cwd", c["root"], "--summary", "x"])
    assert rc == 3 and json.loads(buf.getvalue())["reason"] == "agent_not_permitted_for_autonomy"
    assert not wb.pending_wake().get("pending")


# ── watchdog after the project is settled ───────────────────────────────────
def test_a_finished_project_leaves_the_watchdog_quiet(cycle):
    from core import closed_loop_wake as clw
    from core.control_plane.store import now_ts
    c = cycle
    seen = {"delivered": set(), "decisions": []}
    _run_cycle(c, seen, until=lambda s: s["done"], next_tasks=[h.CLI_TASK])
    emitted = []
    out = clw.slo_scan(now=now_ts() + 7 * 24 * 3600,
                       emit_fn=lambda *a, **k: emitted.append((a, k)) or {"event_id": 0})
    assert emitted == [], (out, emitted)
    # and the finished engine stays finished: more ticks emit nothing, deliver nothing
    n = len(c["browser"].submissions)
    for _ in range(3):
        c["engine"].tick()
        assert c["companion"].tick(wb)["acted"] is False
    assert len(c["browser"].submissions) == n
