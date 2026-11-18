"""The send-evidence probe, executed as JavaScript against a minimal DOM.

Found live on 2026-11-18, the first send after the 2883d2b release (event 88803): the probe
searched each role match's OWN text, but in the current ChatGPT UI the user role is carried
by a hidden `<h4 data-conversation-role="user">` whose text is "You said:"; the message is
in the turn DIV around it. The marker could therefore never be found, so reconciliation
could prove nothing, and "phrase in the composer and in no user turn" lost its second half.

The DOM here is a stand-in (no browser): elements, document order, containment and
`compareDocumentPosition`; selectors resolve through a table the test supplies.
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
    "cdp_composer_evidence_js", os.path.join(_HERE, "tools", "cdp_composer.py"))
cdp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cdp)

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")

EID = 88803
MARK = f"[Owner OS wake] event={EID} trigger=completion. PHRASE"

_PRELUDE = r"""
const SPEC = JSON.parse(process.argv[process.argv.length - 1]);
const Node = {DOCUMENT_POSITION_PRECEDING: 2, DOCUMENT_POSITION_FOLLOWING: 4,
              DOCUMENT_POSITION_CONTAINS: 8, DOCUMENT_POSITION_CONTAINED_BY: 16};
class El {
  constructor(d) { this.id = d.id; this.tagName = d.tag; this.attrs = d.attrs || {};
                   this.text = d.text || ''; this.children = []; this.parentElement = null; }
  get textContent() { return this.text + this.children.map(c => c.textContent).join(''); }
  hasAttribute(a) { return Object.prototype.hasOwnProperty.call(this.attrs, a); }
  getAttribute(a) { return this.hasAttribute(a) ? this.attrs[a] : null; }
  compareDocumentPosition(o) {
    if (o === this) return 0;
    const inside = (a, b) => { for (let p = b.parentElement; p; p = p.parentElement)
                                 if (p === a) return true; return false; };
    if (inside(this, o)) return 16 | 4;
    if (inside(o, this)) return 8 | 2;
    return ORDER.indexOf(o) > ORDER.indexOf(this) ? 4 : 2;
  }
}
const BY = {};
for (const d of SPEC.nodes) BY[d.id] = new El(d);
for (const d of SPEC.nodes) if (d.parent) { BY[d.id].parentElement = BY[d.parent];
                                            BY[d.parent].children.push(BY[d.id]); }
const ORDER = [];
const walk = e => { ORDER.push(e); e.children.forEach(walk); };
SPEC.nodes.filter(d => !d.parent).forEach(d => walk(BY[d.id]));
const MATCH = {};
for (const [sel, ids] of Object.entries(SPEC.match))
  MATCH[sel] = ids.map(i => BY[i]).sort((a, b) => ORDER.indexOf(a) - ORDER.indexOf(b));
globalThis.document = {querySelectorAll: s => MATCH[s] || [],
                       querySelector: s => (MATCH[s] || [])[0] || null};
globalThis.Node = Node;
"""


def _probe_expression() -> str:
    seen = []

    class _Capture:
        def call(self, method, params=None):
            seen.append(params["expression"])
            return {}
    cdp.send_evidence(_Capture(), EID)
    return seen[0]


def _run(nodes, user=(), assistant=(), composer=()):
    spec = {"nodes": nodes,
            "match": {cdp.USER_TURN_SEL: list(user), cdp.ASSISTANT_TURN_SEL: list(assistant),
                      cdp.COMPOSER_SEL: list(composer)}}
    js = _PRELUDE + "\nconsole.log(" + _probe_expression() + ");\n"
    out = subprocess.run([NODE, "-e", js, json.dumps(spec)], capture_output=True, text=True,
                         timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


def _heading_turn(n, role, text):
    """The 2026-10-29 UI: a turn DIV holding a hidden role heading and the message."""
    return [{"id": f"t{n}", "tag": "DIV", "parent": "main"},
            {"id": f"h{n}", "tag": "H4", "parent": f"t{n}",
             "attrs": {"data-conversation-role": role},
             "text": "You said:" if role == "user" else "ChatGPT said:"},
            {"id": f"b{n}", "tag": "DIV", "parent": f"t{n}", "text": text}]


def _page(*turns, composer_text=""):
    nodes = [{"id": "main", "tag": "MAIN"}]
    for t in turns:
        nodes += t
    nodes.append({"id": "c", "tag": "DIV", "parent": "main", "text": composer_text})
    return nodes


def test_a_heading_ui_turn_carrying_our_marker_with_an_answer_after_it():
    """The live 88803 shape. Fails on the released probe: found False."""
    nodes = _page(_heading_turn(1, "assistant", "earlier answer"),
                  _heading_turn(2, "user", MARK),
                  _heading_turn(3, "assistant", "on it"))
    r = _run(nodes, user=["h2"], assistant=["h1", "h3"], composer=["c"])
    assert r == {"found": True, "answered": True, "in_composer": False}


def test_a_heading_ui_turn_with_no_answer_yet():
    nodes = _page(_heading_turn(1, "assistant", "earlier answer"),
                  _heading_turn(2, "user", MARK))
    r = _run(nodes, user=["h2"], assistant=["h1"], composer=["c"])
    assert r == {"found": True, "answered": False, "in_composer": False}


def test_an_answer_only_before_our_turn_is_not_an_answer():
    nodes = _page(_heading_turn(1, "user", MARK), _heading_turn(2, "assistant", "x"))
    nodes = [nodes[0]] + nodes[4:7] + nodes[1:4] + nodes[7:]   # assistant turn first
    r = _run(nodes, user=["h1"], assistant=["h2"], composer=["c"])
    assert r["found"] is True and r["answered"] is False


def test_the_phrase_still_in_the_composer_and_in_no_turn():
    nodes = _page(_heading_turn(1, "user", "a person's own message"),
                  _heading_turn(2, "assistant", "reply"), composer_text=MARK)
    r = _run(nodes, user=["h1"], assistant=["h2"], composer=["c"])
    assert r == {"found": False, "answered": False, "in_composer": True}


def test_another_events_marker_is_not_ours():
    other = MARK.replace(f"event={EID} ", f"event={EID}1 ")
    nodes = _page(_heading_turn(1, "user", other), _heading_turn(2, "assistant", "r"),
                  composer_text=other)
    r = _run(nodes, user=["h1"], assistant=["h2"], composer=["c"])
    assert r == {"found": False, "answered": False, "in_composer": False}


def test_the_older_attribute_ui_still_works():
    nodes = [{"id": "main", "tag": "MAIN"},
             {"id": "u", "tag": "DIV", "parent": "main",
              "attrs": {"data-message-author-role": "user"}, "text": MARK},
             {"id": "a", "tag": "DIV", "parent": "main",
              "attrs": {"data-message-author-role": "assistant"}, "text": "ok"},
             {"id": "c", "tag": "DIV", "parent": "main"}]
    r = _run(nodes, user=["u"], assistant=["a"], composer=["c"])
    assert r == {"found": True, "answered": True, "in_composer": False}


def test_an_assistant_match_inside_our_own_turn_is_not_an_answer():
    """Only a turn AFTER ours answers it; an element nested in our own turn does not."""
    nodes = _page(_heading_turn(1, "user", MARK))
    nodes.insert(-1, {"id": "nested", "tag": "DIV", "parent": "b1",
                      "attrs": {"data-message-author-role": "assistant"}, "text": "quote"})
    r = _run(nodes, user=["h1"], assistant=["nested"], composer=["c"])
    assert r["found"] is True and r["answered"] is False
