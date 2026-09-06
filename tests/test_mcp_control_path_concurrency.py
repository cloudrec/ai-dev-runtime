"""The MCP control path must not serialise on the API's event loop.

`agent_status` / `agent_read` / `agent_send` / `agent_answer` (and `agent_list`,
which shares the path and is by far its highest-volume caller) all shell out to
tmux with a blocking `subprocess.run`, and the delivery path additionally sleeps
0.4s to let the pane redraw. Declared `async def`, that work runs on the single
uvicorn event loop and stalls every other request on the process; upstream that
reads as an intermittent JSON-RPC failure, because the client times out while
the server is still busy and eventually answers 200.

Two things are pinned here:

1. the five handlers are plain `def`, so Starlette runs them in its threadpool;
2. deliveries stay mutually exclusive once they can genuinely run in parallel —
   the event loop used to provide that for free.
"""
from __future__ import annotations

import inspect
import threading
import time

import pytest

from api import v1
from core import agent_control as ac

MCP_CONTROL_HANDLERS = ("agents_list", "agents_status", "agents_read",
                        "agents_send", "agents_answer")


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "agent_control.db"))
    monkeypatch.setenv("AGENT_CONTROL_AUDIT", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AGENT_CONTROL_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.delenv("AGENT_CONTROL_ALLOWED_SESSIONS", raising=False)


# ── 1. the handlers stay off the event loop ─────────────────────────────────
@pytest.mark.parametrize("name", MCP_CONTROL_HANDLERS)
def test_control_path_handler_is_not_a_coroutine(name):
    """`async def` here would put a 15s blocking tmux call on the event loop."""
    handler = getattr(v1, name)
    assert not inspect.iscoroutinefunction(handler), (
        f"{name} is `async def`, so its blocking tmux work runs on the single "
        f"event loop and stalls every other request. Declare it `def` and let "
        f"Starlette's threadpool run it.")


def test_the_route_table_serves_the_sync_handlers():
    """Pin the wiring too: a rename must not quietly orphan the assertion above."""
    by_path = {}
    for route in v1.router.routes:
        by_path.setdefault(route.path, []).append(route)
    for path in ("/api/v1/agents", "/api/v1/agents/status", "/api/v1/agents/read",
                 "/api/v1/agents/send", "/api/v1/agents/answer"):
        routes = by_path.get(path)
        assert routes, f"{path} is no longer registered"
        for route in routes:
            assert route.endpoint.__name__ in MCP_CONTROL_HANDLERS
            assert not inspect.iscoroutinefunction(route.endpoint)


# ── 2. deliveries stay mutually exclusive in the threadpool ─────────────────
# The commands that make up one delivery. Two deliveries interleaving inside
# this set is the failure: a half-pasted message with another message's Enter.
_DELIVERY_CMDS = ("load-buffer", "paste-buffer", "send-keys", "delete-buffer")


class _SlowTmux:
    """A tmux seam slow enough that two threads would overlap if unguarded."""

    def __init__(self, panes, delay=0.02):
        self.delay = delay
        self.panes = panes
        self.inside = 0
        self.max_inside = 0
        self.pastes = 0
        self._lock = threading.Lock()

    def __call__(self, args, stdin=None):
        cmd = args[0]
        if cmd == "list-panes":
            return (0, self.panes, "")
        counted = cmd in _DELIVERY_CMDS
        with self._lock:
            if counted:
                self.inside += 1
                self.max_inside = max(self.max_inside, self.inside)
            if cmd == "paste-buffer":
                self.pastes += 1
        try:
            time.sleep(self.delay)
            if cmd == "capture-pane":
                # A changing pane, so delivery is proven rather than refused.
                return (0, f"tail-{time.perf_counter()}", "")
            return (0, "", "")
        finally:
            with self._lock:
                if counted:
                    self.inside -= 1


@pytest.fixture
def slow_tmux(monkeypatch):
    fake = _SlowTmux(_PANES)
    monkeypatch.setattr(ac, "_tmux", fake)
    monkeypatch.setattr(ac, "find_claude_in_pane", lambda pid: (
        {"pid": 2000 + pid, "cmdline": "claude --resume abc", "cwd": "/opt/safeguard"}
        if 1001 <= pid <= 1008 else None))
    # The 0.4s redraw wait in `_deliver` is deliberately NOT stubbed out. It is
    # part of what the lock has to hold across, and stubbing it would have to go
    # through `ac.time`, which is the `time` module itself — patching it there
    # also silences the delay in `_SlowTmux` below, and then nothing overlaps
    # even with the lock removed and the removal proof passes vacuously.
    return fake


# Distinct panes, so the anti-queue guard (which refuses a second message to a
# pane that is already mid-turn) does not decide the outcome of a test about
# interleaving. `_targets` is used only where every thread must actually deliver.
_PANES = "\n".join(
    f"safeguard{i}\t0\t0\t%{i}\t{1001 + i}\t/opt/safeguard\tnode\t0\tclaude"
    for i in range(8)) + "\n"


def _targets(i):
    return f"safeguard{i}:0.0"


def _deliver_many(count, key_for, target="safeguard0:0.0", target_for=None):
    results: list[dict] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def one(i):
        try:
            dest = target_for(i) if target_for else target
            r = ac.agent_send(dest, f"payload {i}", idempotency_key=key_for(i))
        except BaseException as exc:                     # noqa: BLE001 - reported below
            with lock:
                errors.append(exc)
            return
        with lock:
            results.append(r)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    return results


def test_concurrent_deliveries_never_overlap(slow_tmux):
    """Distinct keys, six threads: the pastes must still happen one at a time."""
    results = _deliver_many(6, key_for=lambda i: f"distinct-{i}", target_for=_targets)
    assert len(results) == 6
    assert slow_tmux.pastes == 6
    assert slow_tmux.max_inside == 1, (
        f"{slow_tmux.max_inside} deliveries were in flight at once — they are "
        f"interleaving, so two pastes can land in one pane mid-message")


def test_one_idempotency_key_delivers_once_under_concurrency(slow_tmux):
    """The check-then-act gap is only reachable once callers run in parallel."""
    results = _deliver_many(6, key_for=lambda _i: "same-key")
    delivered = [r for r in results if not r.get("duplicate")]
    duplicates = [r for r in results if r.get("duplicate")]
    assert len(delivered) == 1, (
        f"{len(delivered)} of 6 callers replaying one idempotency key were let "
        f"through; the pane received the message more than once")
    assert len(duplicates) == 5
    assert slow_tmux.pastes == 1
    assert all(r["delivered"] is False for r in duplicates)


def test_the_delivery_lock_is_released_when_delivery_raises(slow_tmux, monkeypatch):
    """A refusal must not strand the lock and wedge every later delivery."""
    with pytest.raises(ac.AgentControlError):
        ac.agent_send("safeguard0:0.0", "x" * (ac.MAX_MESSAGE_BYTES + 1))
    # RLock is reentrant, so this thread could re-acquire a lock it still holds.
    # Probe from another thread, where a stranded lock really would block.
    free: list[bool] = []

    def probe_lock():
        got = ac._DELIVER_LOCK.acquire(blocking=False)
        free.append(got)
        if got:
            ac._DELIVER_LOCK.release()   # RLock ownership is per-thread

    probe = threading.Thread(target=probe_lock)
    probe.start(); probe.join()
    assert free == [True], "the delivery lock was stranded by the refusal"
    ok = ac.agent_send("safeguard0:0.0", "after the refusal")
    assert ok["delivered"] is True


def test_a_second_concurrent_send_to_one_pane_is_still_refused(monkeypatch):
    """The anti-queue guard must survive the move into the threadpool.

    `agent_send` refuses when the pane is mid-turn, because Claude Code queues
    such a message instead of running it. The check and the delivery are two
    steps; on the event loop they could not interleave, so a second caller
    always observed the first delivery. In the threadpool both callers can read
    "not busy" at once, and the second message stacks behind the turn the first
    just started -- which is exactly what the guard exists to prevent.
    """
    panes = "safeguard0\t0\t0\t%0\t1001\t/opt/safeguard\tnode\t0\tclaude\n"
    delivered_count = {"n": 0}
    state = threading.Lock()

    def fake_tmux(args, stdin=None):
        cmd = args[0]
        if cmd == "list-panes":
            return (0, panes, "")
        if cmd == "capture-pane":
            # Once a message has landed, the pane reads as mid-turn.
            with state:
                busy = delivered_count["n"] > 0
            return (0, "✻ Working… (esc to interrupt)" if busy
                    else f"idle-{time.perf_counter()}", "")
        if cmd == "paste-buffer":
            time.sleep(0.05)
            with state:
                delivered_count["n"] += 1
        return (0, "", "")

    monkeypatch.setattr(ac, "_tmux", fake_tmux)
    monkeypatch.setattr(ac, "find_claude_in_pane", lambda pid: (
        {"pid": 2001, "cmdline": "claude --resume abc", "cwd": "/opt/safeguard"}
        if pid == 1001 else None))

    results = _deliver_many(4, key_for=lambda i: f"same-pane-{i}",
                            target="safeguard0:0.0")
    refused = [r for r in results if r.get("refused")]
    assert delivered_count["n"] == 1, (
        f"{delivered_count['n']} messages were pasted into one pane; the second "
        f"and later ones queue behind the active turn instead of executing")
    assert len(refused) == 3
    assert all(r["refused"] == "working" for r in refused)


# ── 3. the read side is attributable, so a supervisor's look is evidence ────
def test_status_and_read_record_who_asked(slow_tmux, tmp_path, monkeypatch):
    """Without this, "the supervisor read the pane before continuing it" can only
    ever be inferred from a continuation that happens to name the event."""
    import json

    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AGENT_CONTROL_AUDIT", str(audit_path))

    ac.agent_status("safeguard0:0.0", actor="api:bearer", source="172.20.0.2:1 ua=httpx")
    ac.agent_read("safeguard0:0.0", 50, actor="api:bearer", source="172.20.0.2:2 ua=httpx")
    ac.agent_status("safeguard1:0.0")          # an internal worker, positionally

    lines = [json.loads(l) for l in audit_path.read_text().splitlines() if l.strip()]
    by_action = {}
    for entry in lines:
        by_action.setdefault(entry["action"], []).append(entry)

    status_ext = [e for e in by_action["agent_status"] if e["target"] == "safeguard0:0.0"]
    read_ext = [e for e in by_action["agent_read"] if e["target"] == "safeguard0:0.0"]
    assert status_ext and status_ext[-1]["actor"] == "api:bearer"
    assert status_ext[-1]["source"].startswith("172.20.0.2")
    assert read_ext and read_ext[-1]["actor"] == "api:bearer"

    # An internal caller must keep writing exactly the line it wrote before: the
    # keys are absent, not present-and-null, so existing log readers are unaffected.
    internal = [e for e in by_action["agent_status"] if e["target"] == "safeguard1:0.0"]
    assert internal, "the internal status call was not audited"
    assert "actor" not in internal[-1] and "source" not in internal[-1]


def test_the_read_routes_pass_the_caller_through(monkeypatch):
    """Pin the wiring: the handlers must actually thread attribution down."""
    seen = {}

    def fake_status(target, *, actor=None, source=None):
        seen["status"] = (target, actor, source)
        return {"target": target}

    def fake_read(target, lines, *, actor=None, source=None):
        seen["read"] = (target, lines, actor, source)
        return {"target": target}

    monkeypatch.setattr(v1.agent_control, "agent_status", fake_status)
    monkeypatch.setattr(v1.agent_control, "agent_read", fake_read)

    class _Req:
        state = type("S", (), {"auth_method": "bearer"})()
        client = type("C", (), {"host": "172.20.0.2", "port": 4242})()
        headers = {"user-agent": "python-httpx/0.27.0"}

    v1.agents_status(target="safeguard0:0.0", request=_Req(),
                     x_runtime_actor=None, _=True)
    v1.agents_read(target="safeguard0:0.0", request=_Req(), lines=50,
                   x_runtime_actor=None, _=True)

    assert seen["status"][1] == "api:bearer"
    assert seen["status"][2].startswith("172.20.0.2:4242")
    assert seen["read"][2] == "api:bearer"
    assert seen["read"][1] == 50
