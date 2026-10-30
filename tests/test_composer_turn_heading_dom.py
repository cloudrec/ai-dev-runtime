"""The 2026-10 ChatGPT DOM: a turn's role is only on its hidden heading.

Read-only, live on 2026-10-29 (owner-os, pl and bitwhite tabs):
`[data-message-author-role]` matched nothing. `ASSISTANT_TURN_SEL` matched only
`<h4 class=sr-only data-conversation-role=assistant>`, whose text is "ChatGPT said:"
(20 characters), so `last_len` read every assistant turn as 20. `_settled`'s
produced-nothing check therefore could never fire. `data-message-id` was gone too; the
turn DIV around the heading carries `data-chatgpt-search-message-ids` instead.

The JavaScript the session sends is the code under test. It is run in node against a
minimal DOM shaped like the live page. Skipped when node is absent.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess

import pytest

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "cdp_composer_heading_dom", os.path.join(_HERE, "tools", "cdp_composer.py"))
cdp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cdp)

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node not installed")

_SHIM = r"""
const dom = JSON.parse(process.argv[1]);
function mk(d) {
  if (!d) return null;
  const a = d.attrs || {};
  return {tagName: d.tag, textContent: d.text, parentElement: mk(d.parent),
          hasAttribute: k => k in a, getAttribute: k => (k in a ? a[k] : null)};
}
const pick = sel => /stop-button|result-streaming|message-streaming/.test(sel) ? []
  : /'assistant'/.test(sel) ? dom.assistant.map(mk)
  : /'user'/.test(sel) ? dom.user.map(mk) : [];
globalThis.document = {querySelectorAll: pick,
                       querySelector: s => pick(s)[0] || null};
const v = eval(process.argv[2]);
process.stdout.write(JSON.stringify(v === undefined ? null : v));
"""


class _NodeSession(cdp._Session):
    """The real `_Session` methods, with `call` evaluated in node instead of a browser."""

    def __init__(self, dom: dict):
        self.dom = json.dumps(dom)
        self._id = 0

    def call(self, method, params=None):
        assert method == "Runtime.evaluate", method
        out = subprocess.run([NODE, "-e", _SHIM, self.dom, params["expression"]],
                             capture_output=True, text=True, timeout=20, check=True).stdout
        return {"result": {"value": json.loads(out)}}


def _heading_turn(role, body, ids=None):
    """As rendered live: <div [ids]><h4 data-conversation-role>LABEL</h4>body</div>."""
    label = "ChatGPT said:ChatGPT" if role == "assistant" else "You said:You"
    unit = {"tag": "DIV", "attrs": {"class": "x"}, "text": label + body}
    if ids:
        unit["attrs"][cdp._TURN_IDS_ATTR] = ids
    return {"tag": "H4", "text": label, "parent": unit,
            "attrs": {"class": "sr-only", "data-conversation-role": role}}


LIVE = {"assistant": [_heading_turn("assistant", "older answer", "a-old a-old"),
                      _heading_turn("assistant", "x" * 1096, "3bf352be-0bea 3bf352be-0bea")],
        "user": [_heading_turn("user", "y" * 683)]}


def test_an_assistant_turn_is_measured_without_its_heading():
    s = _NodeSession(LIVE)
    assert s.last_len(cdp.ASSISTANT_TURN_SEL) == 1096
    assert s.last_len(cdp.USER_TURN_SEL) == 683


def test_the_assistant_turn_id_comes_from_the_turn_around_the_heading():
    s = _NodeSession(LIVE)
    assert s.last_attr(cdp.ASSISTANT_TURN_SEL, "data-message-id") == "3bf352be-0bea 3bf352be-0bea"


def test_a_user_heading_still_reads_no_id():
    """No ids on user turns: '' keeps the callers on the count alone, as before."""
    assert _NodeSession(LIVE).last_attr(cdp.USER_TURN_SEL, "data-message-id") == ""


def test_an_empty_answer_under_a_heading_is_a_failure(monkeypatch):
    """Fails without the fix: the heading's 20 characters read as an answer."""
    monkeypatch.setattr(cdp, "ASSISTANT_SETTLE_SECS", 0)
    dom = {"assistant": [_heading_turn("assistant", "", "a-new")], "user": []}
    r = cdp._settled(_NodeSession(dom), "submitted_and_assistant_started_generating")
    assert r == {"ok": False, "reason": "assistant_started_but_produced_nothing:0"}


def test_a_real_answer_under_a_heading_is_a_delivery(monkeypatch):
    monkeypatch.setattr(cdp, "ASSISTANT_SETTLE_SECS", 0)
    r = cdp._settled(_NodeSession(LIVE), "submitted_and_assistant_started_generating")
    assert r == {"ok": True, "reason": "submitted_and_assistant_started_generating"}


def test_a_new_assistant_turn_moves_the_id_seen_by_advanced():
    """The virtualized, capped chats keep the count flat; the id is the proof."""
    s = _NodeSession({"assistant": [_heading_turn("assistant", "new answer", "a-new")],
                      "user": []})
    assert cdp._assistant_advanced(s, 1, "a-old a-old", False) is True


def test_the_older_dom_is_read_exactly_as_before():
    """A role-carrying message node: its own text and its own id, no heading logic."""
    old = {"assistant": [{"tag": "DIV", "text": "answer text",
                          "attrs": {"data-message-author-role": "assistant",
                                    "data-message-id": "m-1"},
                          "parent": {"tag": "DIV", "text": "wrapper answer text",
                                     "attrs": {cdp._TURN_IDS_ATTR: "other"}}}],
           "user": []}
    s = _NodeSession(old)
    assert s.last_len(cdp.ASSISTANT_TURN_SEL) == len("answer text")
    assert s.last_attr(cdp.ASSISTANT_TURN_SEL, "data-message-id") == "m-1"


def test_no_match_keeps_the_unreadable_values():
    s = _NodeSession({"assistant": [], "user": []})
    assert s.last_len(cdp.ASSISTANT_TURN_SEL) is None
    assert s.last_attr(cdp.ASSISTANT_TURN_SEL, "data-message-id") == ""


def test_an_editor_inside_a_conversation_turn_is_never_the_composer():
    """2026-10-30, live: an assistant turn held an editable `writing-block-editor`
    ProseMirror textbox; the old selector matched 2 on that tab (TaskBoard 75900,
    bitwhite 75652). Each alternative must exclude the turn containers."""
    alts = [a.strip() for a in cdp.COMPOSER_SEL.split(", ")]
    assert len(alts) == 3
    for a in alts:
        assert ":not([data-chatgpt-search-message-ids] *)" in a, a
        assert ":not([data-message-author-role] *)" in a, a
        assert ":not(article *)" in a, a
