"""The writing-block editor must never be the composer, whichever turn marker survives.

2026-10-30: ChatGPT rendered an editable writing block (a second ProseMirror textbox)
inside an assistant turn; COMPOSER_SEL matched 2 and every wake to that chat failed
`composer_ambiguous_or_absent:2` (TaskBoard 75900, bitwhite 75652). 915bf5b excluded
editors inside turn containers.

2026-10-31, measured read-only on all six live tabs: only one of the three original
markers (`data-chatgpt-search-message-ids`) still wraps the writing block — the page has
no `article` and no `data-message-author-role` at all. The fixture below is the live
ancestor chain of that editor (attribute NAMES only, captured structurally; no content).
Removing any single marker must still leave exactly one composer: the form's.

The selector is evaluated by a real CSS engine: a throwaway headless Chrome with its own
temporary profile, never the production browser. Skipped when no Chrome is installed.
"""
from __future__ import annotations

import html
import importlib.util
import os
import re
import shutil
import subprocess
import tempfile

import pytest

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "cdp_composer_writing_block", os.path.join(_HERE, "tools", "cdp_composer.py"))
cdp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cdp)

CHROME = next((p for p in (shutil.which("google-chrome"), shutil.which("chromium-browser"),
                           shutil.which("chromium")) if p), None)
pytestmark = pytest.mark.skipif(not CHROME, reason="no headless Chrome installed")

# Outermost first. Each entry: (marker name or None, attributes of that ancestor).
# Mirrors the live chain of `[data-testid=chatgpt-writing-block] div.ProseMirror`.
_LIVE_CHAIN = [
    (None, {"data-thread-find-target": ""}),
    ("turn-key", {"data-turn-key": "t1"}),
    (None, {"data-content-search-turn-key": "t1"}),
    (None, {"data-talvt-turn-state": "done"}),
    ("search-ids", {"data-chatgpt-search-message-ids": "m1",
                    "data-chatgpt-search-unit-key": "u1"}),
    ("selection-id", {"data-chatgpt-selection-message-id": "m1",
                      "data-chatgpt-selection-conversation-id": "c1"}),
    (None, {"data-dil-source-message-id": "m1", "data-markdown-copy": ""}),
    (None, {"data-dil-message-id": "m1"}),
    ("surface", {"data-oai-writing-block-surface": "", "data-markdown-copy": ""}),
    ("testid", {"data-testid": "chatgpt-writing-block"}),
    (None, {"data-markdown-copy-content": ""}),
]
_EDITOR = '<div class="ProseMirror" contenteditable="true" role="textbox"></div>'
_COMPOSER = ('<form><div class="ProseMirror ProseMirror-focused" contenteditable="true" '
             'role="textbox" id="the-composer"></div></form>')


def _page(drop: set) -> str:
    """The live page shape with the markers in `drop` renamed away."""
    opened = []
    for name, attrs in _LIVE_CHAIN:
        if name == "testid" and "testid" in drop:
            attrs = {"data-testid": "something-else"}
        elif name in drop:
            attrs = {}
        a = "".join(f' {k}="{html.escape(v)}"' for k, v in attrs.items())
        opened.append(f"<div{a}>")
    thread = "".join(opened) + _EDITOR + "</div>" * len(opened)
    return ("<!doctype html><html><body>" + thread + _COMPOSER +
            "<pre id=out></pre><script>"
            f"const sel={_js_str(cdp.COMPOSER_SEL)};"
            "const n=document.querySelectorAll(sel);"
            "const f=document.querySelector(sel);"
            "document.getElementById('out').textContent="
            "'RESULT:'+n.length+':'+(f&&f.id==='the-composer');"
            "</script></body></html>")


def _js_str(s: str) -> str:
    import json
    return json.dumps(s)


def _evaluate(page: str) -> tuple:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "page.html")
        with open(path, "w") as f:
            f.write(page)
        out = subprocess.run(
            [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
             f"--user-data-dir={os.path.join(d, 'profile')}", "--no-first-run",
             "--dump-dom", "file://" + path],
            capture_output=True, text=True, timeout=60)
    m = re.search(r"RESULT:(-?\d+):(true|false)", out.stdout)
    assert m, (out.returncode, out.stderr[-500:])
    return int(m.group(1)), m.group(2) == "true"


@pytest.mark.parametrize("drop", [
    set(),
    {"search-ids"},                       # the one marker 915bf5b still relied on
    {"search-ids", "turn-key"},
    {"search-ids", "turn-key", "selection-id"},
    {"search-ids", "turn-key", "selection-id", "surface"},
    {"search-ids", "turn-key", "selection-id", "testid"},
], ids=lambda d: "drop:" + ("+".join(sorted(d)) or "none"))
def test_the_writing_block_is_never_the_composer(drop):
    n, first_is_form_composer = _evaluate(_page(drop))
    assert n == 1
    assert first_is_form_composer


def test_no_marker_wraps_a_composer_in_a_form():
    """A guard against over-exclusion: the plain composer, alone on the page, matches."""
    page = _page(set()).replace(_EDITOR, "")
    assert _evaluate(page) == (1, True)
