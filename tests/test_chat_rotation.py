"""A full ChatGPT chat is replaced: a new chat, the routes moved, the owner told once.

Also the state a new chat reads (core.fullcycle.brief) and the composer guard: a send to
the bare site URL matched every ChatGPT tab (a probe on 2026-11-09 landed in the owner-os
chat); only start_new_chat may open a chat, in a tab it created itself.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from core import chat_rotation as rot  # noqa: E402
from core import wake_routes as wr  # noqa: E402
from core.fullcycle import brief  # noqa: E402

OLD = "https://chatgpt.com/c/6ae320ec-4ad0-83ed-806f-cfa868bdcf0f"
NEW = "https://chatgpt.com/c/6af19821-f898-83eb-a5ae-7770af1956f4"
OTHER = "https://chatgpt.com/c/6ae613da-47fc-83eb-b406-cd0d2d5f6905"
BY = "api:client/chatgpt-tech-lead"


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))


class NewChat:
    def __init__(self, result=None):
        self.calls, self.result = [], result or {"ok": True, "url": NEW}

    def __call__(self, msg):
        self.calls.append(msg)
        return self.result


# ── rotation ────────────────────────────────────────────────────────────────────────
def test_a_full_chat_gets_a_new_one_and_every_route_on_it_moves():
    wr.bind_route("owner-os", OLD)
    wr.bind_route("hello-cli", OLD)
    wr.bind_route("gaika", OTHER)
    nc, told = NewChat(), []
    r = rot.rotate(OLD, new_chat=nc, notify=told.append, now=1000.0)
    assert r == {"ok": True, "new": NEW, "moved": ["hello-cli", "owner-os"]}
    routes = {x["route_key"]: x["conversation"] for x in wr.list_routes()}
    assert routes == {"owner-os": NEW, "hello-cli": NEW, "gaika": OTHER}
    assert "techlead_brief" in nc.calls[0] and OLD in nc.calls[0]
    assert len(told) == 1 and NEW in told[0]
    by = {x["route_key"]: x["bound_by"] for x in wr.list_routes()}
    assert by == {"owner-os": "chat-rotation", "hello-cli": "chat-rotation", "gaika": "owner"}


def test_a_second_report_of_the_same_full_chat_opens_no_second_chat():
    wr.bind_route("owner-os", OLD)
    nc = NewChat()
    rot.rotate(OLD, new_chat=nc, now=1000.0)
    wr.bind_route("late-route", OLD)            # bound to the old chat after the move
    r = rot.rotate(OLD, new_chat=nc, now=1010.0)
    assert len(nc.calls) == 1 and r["moved"] == ["late-route"]
    assert {x["conversation"] for x in wr.list_routes()} == {NEW}


def test_a_failed_new_chat_moves_nothing_and_waits_before_trying_again():
    wr.bind_route("owner-os", OLD)
    nc = NewChat({"ok": False, "reason": "new_tab_not_created"})
    assert rot.rotate(OLD, new_chat=nc, now=1000.0)["reason"].startswith("new_chat:")
    assert rot.rotate(OLD, new_chat=nc, now=1000.0 + 60)["reason"] == "rotation_cooldown"
    assert len(nc.calls) == 1
    assert wr.list_routes()[0]["conversation"] == OLD
    nc.result = {"ok": True, "url": NEW}
    assert rot.rotate(OLD, new_chat=nc, now=1000.0 + rot.COOLDOWN_SECS + 1)["ok"]


@pytest.mark.parametrize("bad", [{"ok": True, "url": "https://chatgpt.com/"},
                                 {"ok": True, "url": OLD}, {"ok": True},
                                 {"ok": True, "url": "https://evil.example/c/x"}])
def test_a_new_chat_without_a_real_new_url_moves_nothing(bad):
    wr.bind_route("owner-os", OLD)
    assert rot.rotate(OLD, new_chat=NewChat(bad), now=1000.0)["ok"] is False
    assert wr.list_routes()[0]["conversation"] == OLD


def test_a_chat_nothing_routes_to_is_not_replaced():
    wr.bind_route("gaika", OTHER)
    nc = NewChat()
    assert rot.rotate(OLD, new_chat=nc, now=1000.0)["reason"] == "no_route_on_this_chat"
    assert nc.calls == []


def test_a_raising_new_chat_or_notice_never_escapes():
    wr.bind_route("owner-os", OLD)

    def boom(_):
        raise RuntimeError("cdp down")
    assert rot.rotate(OLD, new_chat=boom, now=1000.0)["ok"] is False

    def bad_notice(_):
        raise OSError("telegram down")
    assert rot.rotate(OLD, new_chat=NewChat(), notify=bad_notice,
                      now=1000.0 + rot.COOLDOWN_SECS + 1)["ok"] is True


def test_not_a_conversation_url_is_refused():
    assert rot.rotate("https://chatgpt.com/", new_chat=NewChat())["ok"] is False


# ── the companion ───────────────────────────────────────────────────────────────────
def _companion():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "wcomp", os.path.join(ROOT, "tools", "wake_companion.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeBridge:
    def __init__(self):
        self.acked = []

    def pending_wake(self):
        return {"pending": True, "conversation": OLD, "phrase": "wake", "event_id": 7,
                "route_key": "owner-os"}

    def acknowledge(self, eid):
        self.acked.append(eid)

    def was_submitted(self, eid):
        return False


@pytest.mark.parametrize("res,rotates", [
    ({"ok": False, "reason": "conversation_full:user_turn_not_observed",
      "conversation_full": True}, True),
    ({"ok": True, "reason": "submitted+conversation_full_notice",
      "conversation_full": True}, True),
    ({"ok": False, "reason": "composer_not_focused"}, False),
    ({"ok": True, "reason": "submitted_and_assistant_started_generating"}, False),
])
def test_the_companion_rotates_only_on_the_cap(monkeypatch, res, rotates):
    import cdp_composer
    wc = _companion()
    seen = []
    monkeypatch.setattr(cdp_composer, "submit_phrase", lambda *a, **k: res)
    monkeypatch.setattr(wc, "rotate_full_chat", lambda conv: seen.append(conv) or {"ok": True})
    b = FakeBridge()
    out = wc.tick(b)
    assert (seen == [OLD]) is rotates
    assert b.acked == ([7] if res["ok"] else [])       # a failed send stays pending


def test_rotate_full_chat_wires_the_real_pieces(monkeypatch):
    import cdp_composer
    from core.control_plane import delivery
    wc = _companion()
    wr.bind_route("owner-os", OLD)
    opened = []
    monkeypatch.setattr(cdp_composer, "start_new_chat",
                        lambda msg, home, source: opened.append((home, source)) or
                        {"ok": True, "url": NEW})
    monkeypatch.setattr(delivery, "_send_owner_push", lambda m: (True, "t:1", None))
    r = wc.rotate_full_chat(OLD)
    assert r["ok"] and opened == [("https://chatgpt.com/", "chat-rotation")]
    assert wr.list_routes()[0]["conversation"] == NEW


# ── the composer ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("url", ["https://chatgpt.com/", "https://chatgpt.com",
                                 "https://chatgpt.com/c/WEB:b535cc94", "https://chatgpt.com/g/x"])
def test_submit_phrase_refuses_anything_but_one_conversation(monkeypatch, url):
    import cdp_composer as c
    monkeypatch.setattr(c, "_attempt", lambda *a, **k: pytest.fail("would have typed"))
    assert c.submit_phrase(url, "x", claim=False) == {"ok": False,
                                                      "reason": "not_a_conversation_url"}


def test_new_chat_home_keeps_a_chatgpt_project():
    import cdp_composer as c
    assert c.new_chat_home(OLD) == "https://chatgpt.com/"
    assert c.new_chat_home("https://chatgpt.com/g/g-p-abc123-x/c/6af1") == \
        "https://chatgpt.com/g/g-p-abc123-x/project"


class _S:
    def __init__(self, hrefs):
        self.hrefs = hrefs

    def __call__(self, ws):
        return self

    def call(self, method, params=None):
        return {"result": {"value": self.hrefs.pop(0) if self.hrefs else ""}}

    def close(self):
        pass


def _new_chat_env(monkeypatch, attempt_res, hrefs):
    import cdp_composer as c
    made, reaped, attempts = [], [], []
    monkeypatch.setattr(c, "_create_tab",
                        lambda url: made.append(url) or {"id": "T1", "webSocketDebuggerUrl": "ws"})
    monkeypatch.setattr(c, "page_responsive", lambda t: True)
    monkeypatch.setattr(c, "_reap", lambda tid: reaped.append(tid) or True)
    monkeypatch.setattr(c, "_attempt",
                        lambda url, phrase, **k: attempts.append(k.get("target")) or attempt_res)
    monkeypatch.setattr(c, "_Session", _S(list(hrefs)))
    return c, made, reaped, attempts


def test_start_new_chat_sends_in_its_own_tab_and_returns_the_new_url(monkeypatch):
    c, made, reaped, attempts = _new_chat_env(
        monkeypatch, {"ok": True, "reason": "submitted"},
        ["https://chatgpt.com/", "https://chatgpt.com/c/WEB:1", NEW + "?model=x"])
    r = c.start_new_chat("hello", sleep=lambda s: None)
    assert r == {"ok": True, "url": NEW, "reason": "submitted"}
    assert made == ["https://chatgpt.com/"] and attempts[0]["id"] == "T1" and reaped == []


def test_start_new_chat_closes_its_tab_when_nothing_was_sent(monkeypatch):
    c, made, reaped, _ = _new_chat_env(monkeypatch, {"ok": False, "reason": "composer_not_focused"},
                                       [])
    r = c.start_new_chat("hello", sleep=lambda s: None)
    assert r["ok"] is False and reaped == ["T1"]


def test_start_new_chat_without_a_url_says_it_was_sent(monkeypatch):
    c, _, reaped, _ = _new_chat_env(monkeypatch, {"ok": True, "reason": "submitted"},
                                    ["https://chatgpt.com/"] * 50)
    r = c.start_new_chat("hello", wait_url_secs=9, sleep=lambda s: None)
    assert r == {"ok": False, "url": None, "reason": "sent_but_no_conversation_url",
                 "sent": True} and reaped == []


def test_start_new_chat_only_from_a_new_chat_page(monkeypatch):
    c, made, _, _ = _new_chat_env(monkeypatch, {"ok": True}, [])
    assert c.start_new_chat("x", home=OLD)["reason"] == "bad_new_chat_home"
    assert made == []


# ── what the new chat reads ─────────────────────────────────────────────────────────
def test_notes_are_kept_bounded_and_shown_in_order():
    for i in range(3):
        brief.add_note(f"t{i}", f"text {i}", by=BY, now=1000.0 + i)
    assert [n["topic"] for n in brief.notes()] == ["t0", "t1", "t2"]


@pytest.mark.parametrize("topic,text,by,err", [
    ("", "t", BY, "topic"), ("x" * 81, "t", BY, "topic"), ("p", "", BY, "text"),
    ("p", "x" * 4001, BY, "text"), ("p", "t", "owner", "authenticated")])
def test_bad_notes_are_refused(topic, text, by, err):
    with pytest.raises(brief.BriefError, match=err):
        brief.add_note(topic, text, by=by)


def test_old_notes_roll_off(monkeypatch):
    monkeypatch.setattr(brief, "NOTES_KEPT", 3)
    for i in range(5):
        brief.add_note("t", f"n{i}", by=BY)
    assert [n["text"] for n in brief.notes(limit=10)] == ["n2", "n3", "n4"]
    conn, own = brief._conn()
    assert conn.execute("SELECT COUNT(*) FROM fc_techlead_note").fetchone()[0] == 3
    conn.close()


def test_the_brief_holds_open_projects_finished_ones_and_notes(tmp_path, monkeypatch):
    from core.fullcycle import engine as fe
    from core.fullcycle import intake
    root = tmp_path / "fc"
    root.mkdir()
    monkeypatch.setenv("FULLCYCLE_INTAKE_ROOT", str(root))
    spec = {"title": "calc", "goal": "a calculator", "acceptance_criteria": ["adds"],
            "tasks": [{"key": "add", "title": "add", "goal": "add()",
                       "acceptance_criteria": ["ok"]}]}
    intake.start("calc", spec, by=BY)
    intake.start("done-one", dict(spec, title="old"), by=BY)
    conn, own = fe.connect()
    conn.execute("INSERT INTO fc_project_done VALUES (?,?,?,?)", ("done-one", 1, "", 5.0))
    conn.commit()
    conn.close()
    brief.add_note("plan", "stage 1 api, stage 2 ui", by=BY)
    b = brief.brief()
    assert [p["project"] for p in b["open_projects"]] == ["calc"]
    assert b["open_projects"][0]["goal"] == "a calculator"
    assert [p["project"] for p in b["recently_finished"]] == ["done-one"]
    assert b["notes"][0]["text"] == "stage 1 api, stage 2 ui"
    assert "techlead_note_add" in b["how_to_continue"]


def test_attempt_uses_the_tab_it_is_given_and_never_looks_one_up(monkeypatch):
    """start_new_chat's whole guarantee: the send goes to the tab it created."""
    import cdp_composer as c
    monkeypatch.setattr(c, "browser_degraded", lambda *a, **k: {"degraded": False})
    monkeypatch.setattr(c, "find_target", lambda url: pytest.fail("looked a tab up"))
    monkeypatch.setattr(c, "find_chatgpt_page", lambda *a: pytest.fail("looked a tab up"))
    monkeypatch.setattr(c, "page_responsive", lambda t: False)
    seen = []
    monkeypatch.setattr(c, "recover_wedged_tab", lambda t, url: seen.append(t["id"]) or None)
    r = c._attempt("https://chatgpt.com/", "x", target={"id": "MINE",
                                                        "webSocketDebuggerUrl": "ws"})
    assert r["reason"] == "renderer_unresponsive" and seen == ["MINE"]
