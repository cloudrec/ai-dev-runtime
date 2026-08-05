"""Phase 3 continuation governor — ending the stop-and-wait stall without inventing work.

The failure this governs was captured live: `mess-qa-automation:0.0` sat at `waiting_input`
for 37 minutes holding `[Pasted text #3 +99 lines]`, and nothing moved.

Two rules the tests exist to hold:
  * the governor may submit only what the OWNER already queued, or advance to an item that
    is written down in a durable source — never anything it authored;
  * a queued line is SUBMITTED (Enter), never re-sent. The pane shows a placeholder for a
    paste, so sending that string would type "[Pasted text …]" instead of the real content.
"""
from __future__ import annotations

import os

import pytest

from core import continuation_governor as cg


PASTE = "[Pasted text #3 +99 lines]"


def _cfg(**over):
    base = {"mess-qa-automation:0.0": {
        "project": "mess", "cwd": "/opt/mess",
        "required_sources": [], "authoritative_pointer": "",
        "pointer_section": "", "submit_owner_queued_paste": True, "enabled": True}}
    base["mess-qa-automation:0.0"].update(over)
    return base


# ═════════ 1. queued-input detection ════════════════════════════════════════
def test_detects_a_pasted_block_in_pending():
    d = cg.detect_queued_input(pending=PASTE)
    assert d["queued"] is True and d["kind"] == "paste"


def test_detects_a_pasted_marker_rendered_in_the_pane():
    d = cg.detect_queued_input(tail=f"some output\n{PASTE}\n")
    assert d["queued"] is True and d["evidence"] == "pasted_marker_in_pane"


def test_detects_the_queued_message_hint():
    d = cg.detect_queued_input(tail="Press up to edit queued messages\n")
    assert d["queued"] is True and d["kind"] == "paste"


def test_detects_ordinary_typed_text():
    d = cg.detect_queued_input(pending="continue the next safe step")
    assert d["queued"] is True and d["kind"] == "text"


def test_clean_input_line_is_not_queued():
    assert cg.detect_queued_input(pending="", tail="❯ \nall done\n")["queued"] is False


# ═════════ 2. one-copy submit, by Enter and never by re-sending ═════════════
def test_queued_paste_is_submitted_not_resent():
    """The bug this pins: sending `step_text` would TYPE the placeholder string instead of
    submitting the owner's real pasted content."""
    d = cg.govern("mess-qa-automation:0.0", state="waiting_input", pending=PASTE,
                  config=_cfg())
    assert d["action"] == "submit_queued"
    assert d["mode"] == "enter"
    assert "step_text" not in d, "a queued line must never be re-sent as text"
    assert d["expected_pending"] == PASTE


def test_queued_text_is_also_submitted_by_enter():
    d = cg.govern("mess-qa-automation:0.0", state="waiting_input",
                  pending="continue the next safe step", config=_cfg())
    assert d["action"] == "submit_queued" and d["mode"] == "enter"


def test_paste_is_blocked_where_the_project_does_not_opt_in():
    d = cg.govern("mess-qa-automation:0.0", state="waiting_input", pending=PASTE,
                  config=_cfg(submit_owner_queued_paste=False))
    assert d["action"] == "blocker" and d["owner_blocker"] is True
    assert d["reason"] == "owner_paste_not_auto_submittable"


def test_a_working_pane_is_left_alone_even_with_queued_text():
    d = cg.govern("mess-qa-automation:0.0", state="working", pending=PASTE, config=_cfg())
    assert d["action"] == "skip"


# ═════════ 3. never invent work ═════════════════════════════════════════════
def test_missing_required_source_is_an_owner_blocker_naming_the_file(tmp_path):
    missing = str(tmp_path / "REDESIGN_EXECUTION_QUEUE.md")
    d = cg.govern("mess-qa-automation:0.0", state="idle", stage_complete=True,
                  config=_cfg(required_sources=[missing]))
    assert d["action"] == "blocker" and d["owner_blocker"] is True
    assert d["reason"] == "missing_required_sources"
    assert d["blocker_fields"] == [f"file:{missing}"]


def test_absent_pointer_section_is_a_blocker_naming_the_section(tmp_path):
    p = tmp_path / "STATE.md"
    p.write_text("# Something else\nno pointer here\n")
    d = cg.govern("mess-qa-automation:0.0", state="idle", stage_complete=True,
                  config=_cfg(required_sources=[str(p)], authoritative_pointer=str(p),
                              pointer_section="EXECUTE NEXT"))
    assert d["action"] == "blocker" and d["reason"] == "pointer_section_absent"
    assert "EXECUTE NEXT" in d["blocker_fields"][0]


def test_advance_quotes_the_durable_queue_rather_than_inventing(tmp_path):
    p = tmp_path / "STATE.md"
    p.write_text("# S\n\n## EXECUTE NEXT\nimplement the settings menu per V5 spec\n\n## Later\nx\n")
    d = cg.govern("mess-qa-automation:0.0", state="idle", stage_complete=True,
                  config=_cfg(required_sources=[str(p)], authoritative_pointer=str(p),
                              pointer_section="EXECUTE NEXT"))
    assert d["action"] == "advance_queue"
    assert "implement the settings menu per V5 spec" in d["queue_excerpt"]
    assert "Later" not in d["queue_excerpt"], "must stop at the next heading"


def test_no_advance_when_the_stage_is_not_complete(tmp_path):
    p = tmp_path / "STATE.md"
    p.write_text("## EXECUTE NEXT\nwork\n")
    d = cg.govern("mess-qa-automation:0.0", state="idle", stage_complete=False,
                  config=_cfg(required_sources=[str(p)], authoritative_pointer=str(p),
                              pointer_section="EXECUTE NEXT"))
    assert d["action"] == "skip"


def test_stale_pointer_is_reported_with_its_mtime(tmp_path):
    p = tmp_path / "STATE.md"
    p.write_text("## EXECUTE NEXT\nwork\n")
    ptr = cg.read_pointer("mess-qa-automation:0.0",
                          _cfg(required_sources=[str(p)], authoritative_pointer=str(p),
                               pointer_section="EXECUTE NEXT"))
    assert ptr["ok"] is True and ptr["mtime"] == os.path.getmtime(str(p))


# ═════════ 4. scope: payment excluded, config fail-closed ═══════════════════
def test_payment_is_not_governed():
    assert cg.govern("payment:0.0", state="idle", pending=PASTE,
                     config=_cfg())["reason"] == "project_not_governed"


def test_shipped_config_excludes_payment_and_invents_no_paths():
    cfg = cg.load_config()
    assert "payment:0.0" not in cfg
    for target, entry in cfg.items():
        for src in (entry.get("required_sources") or []):
            assert os.path.isabs(src), (target, src)


def test_unreadable_config_governs_nothing(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("projects: [[[")
    assert cg.load_config(str(p)) == {}


def test_disabled_project_is_skipped():
    d = cg.govern("mess-qa-automation:0.0", state="waiting_input", pending=PASTE,
                  config=_cfg(enabled=False))
    assert d["action"] == "skip" and d["reason"] == "governor_disabled_for_project"


def test_live_shipped_config_flags_the_missing_mess_queue_file():
    """The real gap: the owner named a queue file that does not exist on disk."""
    src = cg.queue_sources("mess-qa-automation:0.0")
    assert src["configured"] is True
    assert any("REDESIGN_EXECUTION_QUEUE" in m for m in src["missing"])
