"""The owner hears about a lost ChatGPT session once, and hears when it is back."""
from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

from core import chatgpt_session_watch as w

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import cdp_composer as cc  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "wake_companion_sw", os.path.join(ROOT, "tools", "wake_companion.py"))
wc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wc)


class Run:
    def __init__(self, tmp_path, send_ok=True):
        self.path = str(tmp_path / "state.json")
        self.sent = []
        self.send_ok = send_ok

    def send(self, text):
        self.sent.append(text)
        return (True, "t:1", None) if self.send_ok else (False, None, "chat not found")

    def __call__(self, status, now):
        return w.check(lambda: status, self.send, now=now, path=self.path)


def test_one_bad_probe_is_quiet(tmp_path):
    r = Run(tmp_path)
    r(w.LOGGED_OUT, 0)
    assert r.sent == []


def test_second_bad_probe_alerts_once(tmp_path):
    r = Run(tmp_path)
    for t in (0, 300, 600, 900):
        r(w.LOGGED_OUT, t)
    assert len(r.sent) == 1
    assert "вход" in r.sent[0]


def test_browser_down_has_its_own_text(tmp_path):
    r = Run(tmp_path)
    r(w.BROWSER_DOWN, 0)
    r(w.BROWSER_DOWN, 300)
    assert "браузер" in r.sent[0]


def test_reminder_after_the_interval(tmp_path):
    r = Run(tmp_path)
    r(w.LOGGED_OUT, 0)
    r(w.LOGGED_OUT, 300)
    r(w.LOGGED_OUT, 300 + w.REMINDER_SECS - 1)
    assert len(r.sent) == 1
    r(w.LOGGED_OUT, 300 + w.REMINDER_SECS)
    assert len(r.sent) == 2


def test_recovery_is_said_once(tmp_path):
    r = Run(tmp_path)
    r(w.LOGGED_OUT, 0)
    r(w.LOGGED_OUT, 300)
    r(w.OK, 600)
    r(w.OK, 900)
    assert len(r.sent) == 2
    assert "снова" in r.sent[1]


def test_good_probe_with_no_alert_is_quiet(tmp_path):
    r = Run(tmp_path)
    r(w.LOGGED_OUT, 0)
    r(w.OK, 300)
    r(w.LOGGED_OUT, 600)
    assert r.sent == []


def test_failed_send_is_retried(tmp_path):
    r = Run(tmp_path, send_ok=False)
    r(w.LOGGED_OUT, 0)
    res = r(w.LOGGED_OUT, 300)
    assert res == {"status": w.LOGGED_OUT, "sent": False, "error": "chat not found"}
    r.send_ok = True
    assert r(w.LOGGED_OUT, 600)["sent"] is True
    r(w.LOGGED_OUT, 900)
    assert len(r.sent) == 2


def test_failed_recovery_message_is_retried(tmp_path):
    r = Run(tmp_path)
    r(w.LOGGED_OUT, 0)
    r(w.LOGGED_OUT, 300)
    r.send_ok = False
    r(w.OK, 600)
    r.send_ok = True
    assert r(w.OK, 900)["sent"] is True


def test_unknown_changes_nothing(tmp_path):
    r = Run(tmp_path)
    r(w.LOGGED_OUT, 0)
    r(w.UNKNOWN, 300)
    assert r.sent == []
    r(w.LOGGED_OUT, 600)
    assert len(r.sent) == 1


def test_broken_probe_reads_as_unknown(tmp_path):
    r = Run(tmp_path)

    def boom():
        raise RuntimeError("no")
    assert w.check(boom, r.send, now=0, path=r.path)["status"] == w.UNKNOWN


def test_state_survives_a_restart(tmp_path):
    r = Run(tmp_path)
    r(w.LOGGED_OUT, 0)
    r(w.LOGGED_OUT, 300)
    assert json.load(open(r.path))["alerted"] is True
    Run.__call__(r, w.LOGGED_OUT, 600)
    assert len(r.sent) == 1


def test_damaged_state_file_starts_over(tmp_path):
    r = Run(tmp_path)
    open(r.path, "w").write("[not json")
    r(w.LOGGED_OUT, 0)
    r(w.LOGGED_OUT, 300)
    assert len(r.sent) == 1


class Sess:
    value = "in"
    fail = False
    closed = 0
    exprs = []

    def __init__(self, url):
        if Sess.fail:
            raise OSError("no socket")

    def call(self, method, params=None):
        Sess.exprs.append(params)
        return {"result": {"value": Sess.value}}

    def close(self):
        Sess.closed += 1


@pytest.fixture
def page(monkeypatch):
    Sess.fail, Sess.closed, Sess.exprs = False, 0, []
    monkeypatch.setattr(cc, "_Session", Sess)
    monkeypatch.setattr(cc, "find_chatgpt_page", lambda u=None: {"webSocketDebuggerUrl": "ws://x"})


@pytest.mark.parametrize("word,want", [("in", "ok"), ("out", "logged_out"),
                                       ("unknown", "unknown"), (None, "unknown")])
def test_auth_state_words(page, word, want):
    Sess.value = word
    assert cc.auth_state() == want
    assert Sess.closed == 1


def test_auth_state_waits_for_the_fetch(page):
    Sess.value = "in"
    cc.auth_state()
    assert Sess.exprs[0]["awaitPromise"] is True


def test_the_page_never_hands_over_the_session_body():
    assert "accessToken" not in cc._AUTH_STATE_JS
    assert "JSON.stringify" not in cc._AUTH_STATE_JS


def test_auth_state_tab_failure_is_unknown(page):
    Sess.fail = True
    assert cc.auth_state() == "unknown"


def test_auth_state_no_page_is_unknown(monkeypatch):
    monkeypatch.setattr(cc, "find_chatgpt_page", lambda u=None: None)
    assert cc.auth_state() == "unknown"


def test_auth_state_devtools_silent_is_browser_down(monkeypatch):
    def down(u=None):
        raise OSError("refused")
    monkeypatch.setattr(cc, "find_chatgpt_page", down)
    assert cc.auth_state() == "browser_down"


def test_companion_reports_a_failed_send(monkeypatch, capsys):
    monkeypatch.setattr(w, "check", lambda p: {"status": "logged_out", "sent": False,
                                               "error": "chat not found"})
    wc.watch_chatgpt_session()
    assert "send failed: chat not found" in capsys.readouterr().out


# ── login link (owner opens the wake browser over the tailnet and logs in by hand) ──
LINK = "http://polyinput-server.example.ts.net:6080/vnc.html?autoconnect=1&resize=remote"


def _alert(status, monkeypatch, url):
    if url is None:
        monkeypatch.delenv("CHATGPT_SESSION_LOGIN_URL", raising=False)
    else:
        monkeypatch.setenv("CHATGPT_SESSION_LOGIN_URL", url)
    state = {"streak": w.FAIL_THRESHOLD}
    return w.decide(state, status, 1000.0)


def test_logged_out_alert_carries_the_login_link(monkeypatch):
    msg = _alert(w.LOGGED_OUT, monkeypatch, LINK)
    assert msg.startswith(w._TEXT[w.LOGGED_OUT]) and msg.endswith("Войти: " + LINK)


def test_no_link_configured_keeps_the_old_text(monkeypatch):
    assert _alert(w.LOGGED_OUT, monkeypatch, None) == w._TEXT[w.LOGGED_OUT]
    assert _alert(w.LOGGED_OUT, monkeypatch, "  ") == w._TEXT[w.LOGGED_OUT]


def test_browser_down_gets_no_login_link(monkeypatch):
    """Logging in cannot fix a dead browser; the link would only mislead."""
    assert _alert(w.BROWSER_DOWN, monkeypatch, LINK) == w._TEXT[w.BROWSER_DOWN]


@pytest.mark.parametrize("bad", [
    "http://user:secret@host.example.ts.net:6080/vnc.html",
    "http://host.example.ts.net:6080/vnc.html?password=hunter2",
    "javascript:alert(1)",
    "file:///home/owneros/.vncpasswd",
    "host.example.ts.net:6080",
])
def test_a_link_that_could_carry_a_secret_or_is_not_web_is_dropped(monkeypatch, bad):
    assert _alert(w.LOGGED_OUT, monkeypatch, bad) == w._TEXT[w.LOGGED_OUT]
