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


def test_live_shipped_config_points_at_the_real_owner_queue():
    """The queue file was absent when this phase started; the owner's agent created it
    during V8, so the config now points at it and nothing is missing."""
    src = cg.queue_sources("mess-qa-automation:0.0")
    assert src["configured"] is True
    assert src["missing"] == [], src["missing"]
    assert src["pointer"].endswith("REDESIGN_EXECUTION_QUEUE.md")


# ═════════ 5. the REAL owner-authored queue format (machine-readable) ═══════
REAL_QUEUE = "/opt/mess/design/v1/REDESIGN_EXECUTION_QUEUE.md"


def _yaml_queue(tmp_path, pointer="stage_02_invites", status="IN_PROGRESS",
                stages=None, extra=""):
    stages = stages or [{"id": "stage_02_invites", "status": status},
                        {"id": "stage_03_media_voice", "status": "PENDING"}]
    import yaml as _y
    body = _y.safe_dump({"pointer": pointer, "branch": "b", "cwd": "/opt/mess",
                         "deploy_allowed": False, "stages": stages})
    p = tmp_path / "QUEUE.md"
    p.write_text("# Q\n\n## RESUME AFTER `/clear`\n\n> Read the queue.\n> Verify branch.\n\n"
                 "## MACHINE-READABLE STATE\n\n```yaml\n" + body + "```\n" + extra)
    return str(p)


def test_parses_the_machine_readable_block(tmp_path):
    q = cg.parse_queue(_yaml_queue(tmp_path))
    assert q["ok"] and q["format"] == "yaml"
    assert q["pointer"] == "stage_02_invites" and q["current_status"] == "IN_PROGRESS"


def test_resume_instruction_is_quoted_verbatim(tmp_path):
    q = cg.parse_queue(_yaml_queue(tmp_path))
    assert "Read the queue." in q["resume_instruction"]
    assert "Verify branch." in q["resume_instruction"]


def test_pointer_not_among_stages_is_invalid(tmp_path):
    q = cg.parse_queue(_yaml_queue(tmp_path, pointer="stage_99_ghost"))
    assert q["ok"] is False and q["reason"] == "pointer_stage_not_in_stages"


def test_midwrite_yaml_is_a_wait_not_an_improvisation(tmp_path):
    p = tmp_path / "Q.md"
    p.write_text("## MACHINE-READABLE STATE\n\n```yaml\npointer: [unclosed\n```\n")
    q = cg.parse_queue(str(p))
    assert q["ok"] is False and q["reason"].startswith("yaml_invalid")
    d = cg.govern("mess-qa-automation:0.0", state="idle",
                  config=_cfg(authoritative_pointer=str(p), required_sources=[str(p)]))
    assert d["action"] == "skip" and d["reason"].startswith("queue_not_valid")


def test_in_progress_stage_is_left_alone(tmp_path):
    d = cg.govern("mess-qa-automation:0.0", state="idle",
                  config=_cfg(authoritative_pointer=_yaml_queue(tmp_path),
                              required_sources=[]))
    assert d["action"] == "skip" and d["reason"] == "stage_in_progress"


def test_completed_stage_advances_exactly_once_to_the_next_queue_stage(tmp_path):
    path = _yaml_queue(tmp_path, status="DONE")
    d = cg.govern("mess-qa-automation:0.0", state="idle",
                  config=_cfg(authoritative_pointer=path, required_sources=[]))
    assert d["action"] == "advance_queue"
    assert d["stage"] == "stage_02_invites" and d["next_stage"] == "stage_03_media_voice"
    assert d["resume_instruction"]


def test_needs_owner_payload_raises_a_precise_blocker(tmp_path):
    stages = [{"id": "stage_02_invites", "status": "NEEDS_OWNER_PAYLOAD",
               "missing_fields": ["copy.invite_title", "metrics.qr_card",
                                  "states.expired_code"]},
              {"id": "stage_03_media_voice", "status": "PENDING"}]
    path = _yaml_queue(tmp_path, stages=stages, status="NEEDS_OWNER_PAYLOAD")
    d = cg.govern("mess-qa-automation:0.0", state="idle",
                  config=_cfg(authoritative_pointer=path, required_sources=[]))
    assert d["action"] == "blocker" and d["reason"] == "NEEDS_OWNER_PAYLOAD"
    assert d["owner_blocker"] is True
    assert d["blocker_fields"] == ["copy.invite_title", "metrics.qr_card",
                                   "states.expired_code"]


def test_exhausted_queue_does_not_invent_a_next_stage(tmp_path):
    stages = [{"id": "stage_02_invites", "status": "DONE"}]
    path = _yaml_queue(tmp_path, stages=stages, status="DONE")
    d = cg.govern("mess-qa-automation:0.0", state="idle",
                  config=_cfg(authoritative_pointer=path, required_sources=[]))
    assert d["action"] == "skip" and d["reason"] == "queue_exhausted"


@pytest.mark.skipif(not os.path.isfile(REAL_QUEUE), reason="live queue not present")
def test_live_owner_queue_parses_and_validates():
    """The real file the owner's agent authored during V8."""
    q = cg.parse_queue(REAL_QUEUE)
    assert q["ok"] is True and q["format"] == "yaml"
    assert q["pointer"] and q["branch"] and q["cwd"] == "/opt/mess"
    assert q["deploy_allowed"] is False
    assert q["resume_instruction"], "the durable /clear resume text must be extractable"


def test_needs_owner_payload_recorded_in_the_payload_field(tmp_path):
    """The live shape on stage 3: `status: CURRENT` with `payload: NEEDS_OWNER_PAYLOAD`
    and `missing_fields`. Checking `status` alone missed it and idled silently."""
    stages = [{"id": "stage_03_media_voice", "status": "CURRENT",
               "payload": "NEEDS_OWNER_PAYLOAD",
               "missing_fields": ["image/media viewer: title, chrome, actions",
                                  "voice: recording, preview, send copy + metrics"]},
              {"id": "stage_04_security_devices", "status": "PENDING"}]
    path = _yaml_queue(tmp_path, pointer="stage_03_media_voice", stages=stages)
    q = cg.parse_queue(path)
    assert q["needs_owner_payload"] is True
    d = cg.govern("mess-qa-automation:0.0", state="idle",
                  config=_cfg(authoritative_pointer=path, required_sources=[]))
    assert d["action"] == "blocker" and d["reason"] == "NEEDS_OWNER_PAYLOAD"
    assert d["stage"] == "stage_03_media_voice"
    assert d["blocker_fields"] == stages[0]["missing_fields"]


def test_needs_owner_payload_recorded_in_blockers_list(tmp_path):
    stages = [{"id": "stage_03_media_voice", "status": "CURRENT",
               "blockers": ["NEEDS_OWNER_PAYLOAD"], "missing_fields": ["copy.x"]},
              {"id": "stage_04_security_devices", "status": "PENDING"}]
    path = _yaml_queue(tmp_path, pointer="stage_03_media_voice", stages=stages)
    assert cg.parse_queue(path)["needs_owner_payload"] is True


def test_a_specified_payload_is_not_mistaken_for_a_blocker(tmp_path):
    """Anti-overcorrection: a stage whose payload is fully specified must NOT block."""
    stages = [{"id": "stage_03_media_voice", "status": "CURRENT",
               "payload": {"spec": "design/v1/SPEC_V9.md", "copy": "design/v1/COPY_V9.json"}},
              {"id": "stage_04_security_devices", "status": "PENDING"}]
    path = _yaml_queue(tmp_path, pointer="stage_03_media_voice", stages=stages)
    q = cg.parse_queue(path)
    assert q["needs_owner_payload"] is False
    d = cg.govern("mess-qa-automation:0.0", state="idle",
                  config=_cfg(authoritative_pointer=path, required_sources=[]))
    assert d["action"] == "skip" and d["reason"] == "stage_in_progress"


# ═════════ 6. runtime wiring into the autopilot tick ════════════════════════
def test_tick_records_a_governor_blocker_once(tmp_path, monkeypatch):
    """The blocker must reach a durable record and an owner gate — but only ONCE per
    (target, stage, missing-fields). A gate reopening every 60s is noise."""
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import commander_autopilot as ap
    stages = [{"id": "stage_03_media_voice", "status": "CURRENT",
               "payload": "NEEDS_OWNER_PAYLOAD",
               "missing_fields": ["viewer: title", "voice: send copy"]},
              {"id": "stage_04", "status": "PENDING"}]
    path = _yaml_queue(tmp_path, pointer="stage_03_media_voice", stages=stages)
    cfg = _cfg(authoritative_pointer=path, required_sources=[])
    monkeypatch.setattr(cg, "load_config", lambda *a, **k: cfg)

    out1 = ap._governor_pass("mess-qa-automation:0.0", state="idle", tail="", cwd="/opt/mess",
                             ctrl=None, conv="c", evaluate_only=True, conn=None)
    assert out1["decision"] == "governor_blocker"
    assert out1["blocker_fields"] == ["viewer: title", "voice: send copy"]
    ap._governor_pass("mess-qa-automation:0.0", state="idle", tail="", cwd="/opt/mess",
                      ctrl=None, conv="c", evaluate_only=True, conn=None)

    import sqlite3
    # the blocker ledger is control-plane state (it opens an owner gate), so it lives in
    # CONTROL_PLANE_DB alongside cp_action / owner_gate
    conn = sqlite3.connect(str(tmp_path / "cp.db"))
    n = conn.execute("SELECT count(*) FROM governor_blocker").fetchone()[0]
    conn.close()
    assert n == 1, "the same blocker must not be recorded twice"


def test_tick_leaves_ungoverned_projects_to_the_normal_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import commander_autopilot as ap
    monkeypatch.setattr(cg, "load_config", lambda *a, **k: {})
    assert ap._governor_pass("payment:0.0", state="idle", tail="", cwd="/x", ctrl=None,
                             conv="c", evaluate_only=True, conn=None) is None


def test_governor_submit_is_owner_gated_outside_the_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import commander_autopilot as ap
    from core.control_plane import actuator as act
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset())     # nothing allowlisted
    monkeypatch.setattr(cg, "load_config", lambda *a, **k: _cfg())
    monkeypatch.setattr("core.agent_control.pending_input_text", lambda *a, **k: PASTE)
    out = ap._governor_pass("mess-qa-automation:0.0", state="waiting_input", tail="",
                            cwd="/opt/mess", ctrl=None, conv="c", evaluate_only=False,
                            conn=None)
    assert out["decision"] == "governor_submit_owner_gated"


def test_governor_skip_falls_through_to_normal_evaluation(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import commander_autopilot as ap
    monkeypatch.setattr(cg, "load_config", lambda *a, **k: _cfg())
    monkeypatch.setattr("core.agent_control.pending_input_text", lambda *a, **k: "")
    out = ap._governor_pass("mess-qa-automation:0.0", state="working", tail="", cwd="/opt/mess",
                            ctrl=None, conv="c", evaluate_only=True, conn=None)
    assert out is None, "a skip must not short-circuit the ordinary autopilot logic"


def test_governor_never_runs_on_a_pane_that_is_actually_progressing(tmp_path, monkeypatch):
    """A pane can read `idle` while a background subagent works. Governing there would
    raise a blocker over live work — caught by the adversarial suite, not by mine."""
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import commander_autopilot as ap
    monkeypatch.setattr(cg, "load_config", lambda *a, **k: _cfg())
    busy = "✻ Waiting for 1 background agent to finish\n"
    assert ap._governor_pass("mess-qa-automation:0.0", state="idle", tail=busy,
                             cwd="/opt/mess", ctrl=None, conv="c", evaluate_only=True,
                             conn=None) is None


def test_recorded_missing_fields_are_a_gap_even_with_a_payload_path(tmp_path):
    """Live stage 5: `status: CURRENT`, `payload: design/v1/screens/CALLS_AND_STATES_V3.json`
    — a real file — yet three `missing_fields` were still recorded. The token-only check
    read that as fully specified, so an idle agent parked on it was never surfaced: the
    stop-and-wait stall, reintroduced. Recorded missing fields ARE the gap."""
    stages = [{"id": "stage_05_live_calls", "status": "CURRENT",
               "payload": "design/v1/screens/CALLS_AND_STATES_V3.json",
               "missing_fields": ["participant picker: metrics and state copy",
                                  "active call: control layout, reconnect copy"]},
              {"id": "stage_06", "status": "PENDING"}]
    path = _yaml_queue(tmp_path, pointer="stage_05_live_calls", stages=stages)
    q = cg.parse_queue(path)
    assert q["needs_owner_payload"] is True
    d = cg.govern("mess-qa-automation:0.0", state="idle",
                  config=_cfg(authoritative_pointer=path, required_sources=[]))
    assert d["action"] == "blocker" and d["reason"] == "NEEDS_OWNER_PAYLOAD"
    assert d["blocker_fields"] == stages[0]["missing_fields"]


def test_empty_missing_fields_list_is_not_a_gap(tmp_path):
    """Anti-overcorrection: an empty or whitespace-only list must not raise a blocker."""
    stages = [{"id": "stage_05_live_calls", "status": "CURRENT",
               "payload": "design/v1/screens/SPEC.json", "missing_fields": ["", "  "]},
              {"id": "stage_06", "status": "PENDING"}]
    path = _yaml_queue(tmp_path, pointer="stage_05_live_calls", stages=stages)
    assert cg.parse_queue(path)["needs_owner_payload"] is False


def test_governor_reads_pending_through_the_injected_controller(tmp_path, monkeypatch):
    """It previously went straight to agent_control, so the governor read the LIVE tmux
    pane during tests and the suite depended on what a real canary was showing."""
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import commander_autopilot as ap
    monkeypatch.setattr(cg, "load_config", lambda *a, **k: _cfg())

    def _boom(*a, **k):
        raise AssertionError("live pane must not be read when a controller is injected")
    monkeypatch.setattr("core.agent_control.pending_input_text", _boom)

    class Ctrl:
        def snapshot(self, target, cwd):
            return {"pending": PASTE, "tail": "", "state": "waiting_input"}

    out = ap._governor_pass("mess-qa-automation:0.0", state="waiting_input", tail="",
                            cwd="/opt/mess", ctrl=Ctrl(), conv="c", evaluate_only=True,
                            conn=None)
    assert out is None or out["decision"].startswith("governor")


def test_a_refused_paste_is_not_labelled_a_missing_payload(tmp_path, monkeypatch):
    """Live 13:00:57: an opaque paste on arbitrage2 was refused (correct), but the gate was
    written as `owner_payload_missing` / "NEEDS_OWNER_PAYLOAD at -" with empty fields. A
    refused paste is not a missing payload; mislabelling buries the real payload gaps."""
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import commander_autopilot as ap
    cfg = {"arbitrage2-opus:0.0": {"cwd": "/opt/arbitrage2", "required_sources": [],
                                   "authoritative_pointer": "", "pointer_section": "",
                                   "submit_owner_queued_paste": False, "enabled": True}}
    monkeypatch.setattr(cg, "load_config", lambda *a, **k: cfg)

    class Ctrl:
        def snapshot(self, target, cwd):
            return {"pending": PASTE, "tail": "", "state": "waiting_input"}

    out = ap._governor_pass("arbitrage2-opus:0.0", state="waiting_input", tail="",
                            cwd="/opt/arbitrage2", ctrl=Ctrl(), conv="c",
                            evaluate_only=True, conn=None)
    assert out["decision"] == "governor_blocker"
    assert out["note"] == "owner_paste_not_auto_submittable"
    assert out["blocker_detail"], "a fieldless blocker must still record what it saw"

    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "cp.db"))
    kinds = [r[0] for r in conn.execute("SELECT kind FROM owner_gate")]
    conn.close()
    assert "owner_payload_missing" not in kinds, kinds
    assert any("paste" in k for k in kinds), kinds


# ═════════ 7. project-role isolation ════════════════════════════════════════
ISO_CFG = {
    "mess-qa-automation:0.0": {"project": "mess", "role": "messenger_ui_redesign",
                               "cwd": "/opt/mess", "enabled": True,
                               "allowed_scopes": ["mess_ui"],
                               "forbidden_scopes": ["payment", "jobhunter", "live_trading"],
                               "submit_owner_queued_paste": True,
                               "required_sources": [], "authoritative_pointer": ""},
    "arbitrage2-opus:0.0": {"project": "arbitrage2", "role": "paper_only_research",
                            "cwd": "/opt/arbitrage2", "enabled": True,
                            "allowed_scopes": ["arb_paper"],
                            "forbidden_scopes": ["live_trading", "payment", "jobhunter",
                                                 "mess_ui"],
                            "submit_owner_queued_paste": True,
                            "required_sources": [], "authoritative_pointer": ""},
}


@pytest.mark.parametrize("target,text,scope", [
    ("mess-qa-automation:0.0", "run the payment payout reconciliation", "payment"),
    ("mess-qa-automation:0.0", "post the JobHunter vacancy microtask", "jobhunter"),
    ("arbitrage2-opus:0.0", "place order on the venue with the exchange key", "live_trading"),
    ("arbitrage2-opus:0.0", "rebuild the messenger redesign screen spec", "mess_ui"),
])
def test_cross_project_work_is_refused_with_the_right_label(target, text, scope):
    r = cg.check_project_isolation(target, text, ISO_CFG)
    assert r["allowed"] is False
    assert r["reason"] == "cross_project_work_refused" and r["scope"] == scope


@pytest.mark.parametrize("target,text", [
    ("mess-qa-automation:0.0", "continue the next safe internal qa audit step"),
    ("arbitrage2-opus:0.0", "continue the next safe read-only audit step"),
])
def test_in_role_work_is_allowed(target, text):
    assert cg.check_project_isolation(target, text, ISO_CFG)["allowed"] is True


def test_queued_paste_is_refused_when_it_is_another_projects_work():
    """Even the owner's OWN queued line is refused if it drags the project out of role."""
    d = cg.govern("arbitrage2-opus:0.0", state="waiting_input",
                  pending="place order on the venue using the exchange key",
                  config=ISO_CFG)
    assert d["action"] == "blocker" and d["reason"] == "cross_project_work_refused"
    assert d["scope"] == "live_trading"


def test_payment_can_never_be_governed_at_all():
    assert cg.check_project_isolation("payment:0.0", "anything", ISO_CFG)["allowed"] is False
    assert cg.govern("payment:0.0", state="idle", config=ISO_CFG)["reason"] == \
        "project_not_governed"


def test_shipped_config_declares_roles_and_forbidden_scopes():
    cfg = cg.load_config()
    assert "payment:0.0" not in cfg
    for target, e in cfg.items():
        assert e.get("role"), target
        assert e.get("forbidden_scopes"), target
    assert "live_trading" in cfg["arbitrage2-opus:0.0"]["forbidden_scopes"]
    assert "payment" in cfg["mess-qa-automation:0.0"]["forbidden_scopes"]


# ═════════ 8. artefact-driven advancement (canary harness) ══════════════════
def _artefact_queue(tmp_path, artefact="reports/A.md", nxt_instruction="write B"):
    import yaml as _y
    stages = [{"id": "stage_a", "status": "CURRENT", "artefact": artefact,
               "next_stage": "stage_b"},
              {"id": "stage_b", "status": "PENDING", "instruction": nxt_instruction,
               "next_stage": None}]
    body = _y.safe_dump({"pointer": "stage_a", "cwd": str(tmp_path),
                         "deploy_allowed": False, "stages": stages})
    p = tmp_path / "Q.md"
    p.write_text("## RESUME AFTER `/clear`\n\n> Read the queue.\n\n"
                 "## MACHINE-READABLE STATE\n\n```yaml\n" + body + "```\n")
    return str(p)


def _canary_cfg(tmp_path, path):
    return {"cp-canary:0.0": {"project": "cp-canary", "role": "disposable_canary",
                              "cwd": str(tmp_path), "enabled": True,
                              "allowed_scopes": ["canary_file"],
                              "forbidden_scopes": ["payment", "publication"],
                              "submit_owner_queued_paste": False,
                              "required_sources": [], "authoritative_pointer": path}}


def test_stage_is_incomplete_until_its_artefact_exists(tmp_path):
    path = _artefact_queue(tmp_path)
    d = cg.govern("cp-canary:0.0", state="idle", config=_canary_cfg(tmp_path, path))
    assert d["action"] == "skip" and d["reason"] == "stage_in_progress"


def test_artefact_present_advances_once_with_the_verbatim_instruction(tmp_path):
    path = _artefact_queue(tmp_path)
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "A.md").write_text("done")
    d = cg.govern("cp-canary:0.0", state="idle", config=_canary_cfg(tmp_path, path))
    assert d["action"] == "advance_queue"
    assert d["reason"] == "artefact_present_stage_complete"
    assert d["next_stage"] == "stage_b" and d["step_text"] == "write B"


def test_advance_blocks_when_the_next_stage_has_no_instruction(tmp_path):
    path = _artefact_queue(tmp_path, nxt_instruction="")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "A.md").write_text("done")
    d = cg.govern("cp-canary:0.0", state="idle", config=_canary_cfg(tmp_path, path))
    assert d["action"] == "blocker" and d["reason"] == "NEEDS_OWNER_PAYLOAD"
    assert d["blocker_fields"] == ["instruction for stage_b"]


def test_advance_refuses_a_next_stage_outside_the_project_role(tmp_path):
    path = _artefact_queue(tmp_path, nxt_instruction="publish the release to production")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "A.md").write_text("done")
    d = cg.govern("cp-canary:0.0", state="idle", config=_canary_cfg(tmp_path, path))
    assert d["action"] == "blocker" and d["reason"] == "cross_project_work_refused"


def test_idle_agent_on_an_unstarted_stage_gets_its_instruction_once(tmp_path):
    """Bootstrap: the first stage has no artefact yet, so artefact-completion cannot fire.
    An idle agent on a stage that declares an instruction must receive it verbatim."""
    path = _artefact_queue(tmp_path)
    import yaml as _y
    data = _y.safe_load(open(path).read().split("```yaml")[1].split("```")[0])
    data["stages"][0]["instruction"] = "append one dated line to reports/A.md"
    body = _y.safe_dump(data)
    open(path, "w").write("## MACHINE-READABLE STATE\n\n```yaml\n" + body + "```\n")
    d = cg.govern("cp-canary:0.0", state="idle", config=_canary_cfg(tmp_path, path))
    assert d["action"] == "advance_queue" and d["reason"] == "stage_not_started"
    assert d["step_text"] == "append one dated line to reports/A.md"


def test_a_working_agent_on_an_unstarted_stage_is_left_alone(tmp_path):
    path = _artefact_queue(tmp_path)
    import yaml as _y
    data = _y.safe_load(open(path).read().split("```yaml")[1].split("```")[0])
    data["stages"][0]["instruction"] = "append one dated line to reports/A.md"
    open(path, "w").write("## MACHINE-READABLE STATE\n\n```yaml\n" + _y.safe_dump(data) + "```\n")
    d = cg.govern("cp-canary:0.0", state="working", config=_canary_cfg(tmp_path, path))
    assert d["action"] == "skip"


def test_stage_without_an_instruction_is_not_invented(tmp_path):
    """A stage with neither artefact-completion nor an instruction must NOT be filled in."""
    path = _artefact_queue(tmp_path)
    d = cg.govern("cp-canary:0.0", state="idle", config=_canary_cfg(tmp_path, path))
    assert d["action"] == "skip" and d["reason"] == "stage_in_progress"


def test_advance_is_delivered_not_merely_reported(tmp_path, monkeypatch):
    """Reporting `advance_queue` without delivering left the agent with neither an
    autopilot poke (short-circuited by the governor) nor a governor step — a stall the
    governor itself introduced."""
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import commander_autopilot as ap
    from core.control_plane import actuator as act
    monkeypatch.setattr(act, "ENABLED", True)
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset({"cp-canary:0.0"}))

    path = _artefact_queue(tmp_path, nxt_instruction="continue with the next safe step")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "A.md").write_text("done")
    monkeypatch.setattr(cg, "load_config", lambda *a, **k: _canary_cfg(tmp_path, path))

    delivered = {}
    monkeypatch.setattr(ap, "deliver_next_step",
                        lambda t, step, **k: delivered.update(target=t, step=step)
                        or {"acted": True, "verified": True})
    out = ap._governor_pass("cp-canary:0.0", state="idle", tail="", cwd=str(tmp_path),
                            ctrl=None, conv="c", evaluate_only=False, conn=None)
    assert out["decision"] == "governor_advanced" and out["delivered"] is True
    # The DELIVERED text is the project's own classifier-safe nudge, never the queue's
    # instruction — see test_queue_instruction_is_never_typed_into_the_pane.
    assert ap.classify_safety(delivered["step"]) == "autonomous_safe"
    assert out["queue_step"].startswith("continue with the next safe step")


def test_advance_outside_the_allowlist_is_owner_gated(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import commander_autopilot as ap
    from core.control_plane import actuator as act
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset())
    path = _artefact_queue(tmp_path, nxt_instruction="continue with the next safe step")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "A.md").write_text("done")
    monkeypatch.setattr(cg, "load_config", lambda *a, **k: _canary_cfg(tmp_path, path))
    out = ap._governor_pass("cp-canary:0.0", state="idle", tail="", cwd=str(tmp_path),
                            ctrl=None, conv="c", evaluate_only=False, conn=None)
    assert out["decision"] == "governor_advance_owner_gated"


def test_an_unsafe_queue_instruction_is_never_delivered(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import commander_autopilot as ap
    from core.control_plane import actuator as act
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset({"cp-canary:0.0"}))
    path = _artefact_queue(tmp_path, nxt_instruction="git push and deploy to prod")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "A.md").write_text("done")
    monkeypatch.setattr(cg, "load_config", lambda *a, **k: _canary_cfg(tmp_path, path))
    out = ap._governor_pass("cp-canary:0.0", state="idle", tail="", cwd=str(tmp_path),
                            ctrl=None, conv="c", evaluate_only=False, conn=None)
    assert out["decision"] in ("governor_step_unsafe", "governor_blocker")


def test_queue_instruction_is_never_typed_into_the_pane(tmp_path, monkeypatch):
    """Live 14:09:29: the governor tried to deliver the queue's own rich instruction
    ("append one dated line to reports/ACCEPTANCE_A.md") and the safety classifier refused
    it — `governor_step_unsafe`. Correct refusal, wrong design: the queue's text is domain
    content for the AGENT to read, not something the governor may type. The governor now
    delivers only a classifier-safe continuation nudge."""
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import commander_autopilot as ap
    from core.control_plane import actuator as act
    monkeypatch.setattr(act, "ENABLED", True)
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset({"cp-canary:0.0"}))

    rich = "append one dated line to reports/ACCEPTANCE_A.md describing this stage"
    path = _artefact_queue(tmp_path, nxt_instruction=rich)
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "A.md").write_text("done")
    monkeypatch.setattr(cg, "load_config", lambda *a, **k: _canary_cfg(tmp_path, path))
    monkeypatch.setattr(ap, "load_registry", lambda *a, **k: {
        "cp-canary:0.0": {"next_step": "continue with the next safe canary note"}})

    sent = {}
    monkeypatch.setattr(ap, "deliver_next_step",
                        lambda t, step, **k: sent.update(step=step)
                        or {"acted": True, "verified": True})
    out = ap._governor_pass("cp-canary:0.0", state="idle", tail="", cwd=str(tmp_path),
                            ctrl=None, conv="c", evaluate_only=False, conn=None)
    assert out["decision"] == "governor_advanced"
    assert sent["step"] == "continue with the next safe canary note"
    assert rich not in sent["step"], "the queue's rich text must never be typed"
    assert ap.classify_safety(sent["step"]) == "autonomous_safe"
    assert out["queue_step"].startswith("append one dated line"), \
        "the queue step is still recorded for audit, just not delivered"


def test_a_stage_is_nudged_once_then_suppressed(tmp_path, monkeypatch):
    """Live: two `governor_advanced` for stage_a_write_note 70s apart. The
    stage_not_started path re-delivered on every tick — exactly-once violated."""
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import commander_autopilot as ap
    from core.control_plane import actuator as act
    monkeypatch.setattr(act, "ENABLED", True)
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset({"cp-canary:0.0"}))
    path = _artefact_queue(tmp_path, nxt_instruction="do the thing")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "A.md").write_text("done")
    monkeypatch.setattr(cg, "load_config", lambda *a, **k: _canary_cfg(tmp_path, path))
    monkeypatch.setattr(ap, "load_registry", lambda *a, **k: {
        "cp-canary:0.0": {"next_step": "continue with the next safe step"}})
    calls = []
    monkeypatch.setattr(ap, "deliver_next_step",
                        lambda t, step, **k: calls.append(step)
                        or {"acted": True, "verified": True})

    first = ap._governor_pass("cp-canary:0.0", state="idle", tail="", cwd=str(tmp_path),
                              ctrl=None, conv="c", evaluate_only=False, conn=None)
    assert first["decision"] == "governor_advanced"
    for _ in range(5):                       # several later ticks
        later = ap._governor_pass("cp-canary:0.0", state="idle", tail="", cwd=str(tmp_path),
                                  ctrl=None, conv="c", evaluate_only=False, conn=None)
        # This queue's pointer still names the finished stage, so the repeat ticks report
        # the stall rather than a tidy "suppressed" — see the stale-pointer tests below.
        assert later["decision"] == "governor_queue_pointer_stale"
    assert len(calls) == 1, calls


def test_a_different_stage_is_still_nudged(tmp_path, monkeypatch):
    """Anti-overcorrection: the guard is per-stage, so real progress is never blocked."""
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import commander_autopilot as ap
    g1 = ap._stage_delivery_gate(None, "cp-canary:0.0", "stage_a")
    g2 = ap._stage_delivery_gate(None, "cp-canary:0.0", "stage_a")
    g3 = ap._stage_delivery_gate(None, "cp-canary:0.0", "stage_b")
    assert g1["allow"] is True and g2["allow"] is False and g3["allow"] is True


def test_stage_nudges_stop_after_the_attempt_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import commander_autopilot as ap
    t0 = 1_000_000.0
    for i in range(ap.GOVERNOR_STAGE_MAX_ATTEMPTS):
        g = ap._stage_delivery_gate(None, "x:0.0", "s", now=t0 + i * 100000)
        assert g["allow"] is True
    g = ap._stage_delivery_gate(None, "x:0.0", "s", now=t0 + 900000)
    assert g["allow"] is False and g["reason"] == "stage_nudge_cap_reached"


def test_a_new_conversation_reopens_the_same_stage(tmp_path, monkeypatch):
    """After a real `/clear` the stage is unchanged but the agent has lost every instruction
    it was ever given. Suppressing there would turn the anti-over-nudge guard into a stall —
    the exact failure it exists to prevent, only quieter."""
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import commander_autopilot as ap
    a = ap._stage_delivery_gate(None, "cp-canary:0.0", "stage_a", conv="conv-1")
    b = ap._stage_delivery_gate(None, "cp-canary:0.0", "stage_a", conv="conv-1")
    c = ap._stage_delivery_gate(None, "cp-canary:0.0", "stage_a", conv="conv-2")
    assert a["allow"] is True
    assert b["allow"] is False, "same conversation must stay suppressed"
    assert c["allow"] is True and c["reason"] == "stage_nudge_allowed_new_conversation"
    assert c["attempts"] == 1, "the counter restarts for the new conversation"


def test_an_unknown_conversation_id_never_resets_the_guard(tmp_path, monkeypatch):
    """Fail-closed: an empty/unreadable conversation id proves nothing. If it reset the
    counter, an unobservable pane would buy an unlimited nudge budget."""
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import commander_autopilot as ap
    assert ap._stage_delivery_gate(None, "y:0.0", "s", conv="conv-1")["allow"] is True
    for bad in ("", "   ", None):
        g = ap._stage_delivery_gate(None, "y:0.0", "s", conv=bad or "")
        assert g["allow"] is False, f"unknown conv {bad!r} must not reset the guard"
    # ...and a known-but-identical id is likewise no reset.
    assert ap._stage_delivery_gate(None, "y:0.0", "s", conv="conv-1")["allow"] is False


def test_the_cap_still_binds_within_one_conversation(tmp_path, monkeypatch):
    """The conversation reset must not become an escape hatch from the attempt cap."""
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import commander_autopilot as ap
    t0 = 1_000_000.0
    for i in range(ap.GOVERNOR_STAGE_MAX_ATTEMPTS):
        assert ap._stage_delivery_gate(None, "z:0.0", "s", now=t0 + i * 100000,
                                       conv="same")["allow"] is True
    g = ap._stage_delivery_gate(None, "z:0.0", "s", now=t0 + 900000, conv="same")
    assert g["allow"] is False and g["reason"] == "stage_nudge_cap_reached"


def test_rows_written_before_the_conv_column_existed_still_work(tmp_path, monkeypatch):
    """Live rows already exist without `conv` (the deployed guard wrote one for the canary).
    The migration must neither crash nor silently treat the legacy row as a new conversation."""
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    cp = str(tmp_path / "cp.db")
    monkeypatch.setenv("CONTROL_PLANE_DB", cp)
    import sqlite3
    import time as _t
    conn = sqlite3.connect(cp)
    conn.execute("""CREATE TABLE governor_stage_delivery (
        target TEXT, stage TEXT, attempts INTEGER, last_ts REAL, last_at TEXT,
        PRIMARY KEY (target, stage))""")          # the pre-fix schema, verbatim
    conn.execute("INSERT INTO governor_stage_delivery VALUES (?,?,?,?,?)",
                 ("cp-canary:0.0", "stage_a_write_note", 1, _t.time(), "legacy"))
    conn.commit()
    conn.close()
    from core import commander_autopilot as ap
    g = ap._stage_delivery_gate(None, "cp-canary:0.0", "stage_a_write_note", conv="conv-new")
    assert g["allow"] is False, ("a legacy row has no recorded conversation, so a new id "
                                 "cannot be PROVEN different — it must stay suppressed")
    assert g["reason"] == "stage_nudge_cooldown"


# ═════════ 9. a completed stage that is still the queue's pointer ═══════════
def _advanced_pointer_queue(tmp_path):
    """The same queue after its owner did advance the pointer — stage_a DONE, pointer on b."""
    import yaml as _y
    stages = [{"id": "stage_a", "status": "DONE", "artefact": "reports/A.md",
               "next_stage": "stage_b"},
              {"id": "stage_b", "status": "CURRENT", "instruction": "write B",
               "artefact": "reports/B.md", "next_stage": None}]
    body = _y.safe_dump({"pointer": "stage_b", "cwd": str(tmp_path),
                         "deploy_allowed": False, "stages": stages})
    p = tmp_path / "Q.md"
    p.write_text("## RESUME AFTER `/clear`\n\n> Read the queue.\n\n"
                 "## MACHINE-READABLE STATE\n\n```yaml\n" + body + "```\n")
    return str(p)


def test_a_finished_stage_still_named_by_the_pointer_is_flagged(tmp_path):
    """The agent reads the FILE. If the pointer still names a stage whose artefact exists,
    the agent redoes finished work no matter what the control plane concluded — observed
    live as a second 'repeat run' line appended to the canary's ACCEPTANCE_A.md."""
    path = _artefact_queue(tmp_path)
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "A.md").write_text("done")
    d = cg.govern("cp-canary:0.0", state="idle", config=_canary_cfg(tmp_path, path))
    assert d["action"] == "advance_queue"
    assert d.get("pointer_stale") is True


def test_an_advanced_pointer_is_not_flagged_stale(tmp_path):
    """Anti-overcorrection: once the queue owner advances the pointer, nothing is stale."""
    path = _advanced_pointer_queue(tmp_path)
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "A.md").write_text("done")
    d = cg.govern("cp-canary:0.0", state="idle", config=_canary_cfg(tmp_path, path))
    assert not d.get("pointer_stale"), d
    assert d["action"] == "advance_queue" and d["reason"] == "stage_not_started"


def test_the_stall_is_raised_to_the_owner_not_recorded_as_suppressed(tmp_path, monkeypatch):
    """A suppressed row reads as 'already handled'. When the pointer is stale it means the
    opposite: work is being repeated every tick and nothing will ever advance it."""
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    cp = str(tmp_path / "cp.db")
    monkeypatch.setenv("CONTROL_PLANE_DB", cp)
    from core import commander_autopilot as ap
    from core.control_plane import actuator as act
    monkeypatch.setattr(act, "ENABLED", True)
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset({"cp-canary:0.0"}))
    path = _artefact_queue(tmp_path, nxt_instruction="do the thing")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "A.md").write_text("done")
    monkeypatch.setattr(cg, "load_config", lambda *a, **k: _canary_cfg(tmp_path, path))
    monkeypatch.setattr(ap, "load_registry", lambda *a, **k: {
        "cp-canary:0.0": {"next_step": "continue with the next safe step"}})
    calls = []
    monkeypatch.setattr(ap, "deliver_next_step",
                        lambda t, step, **k: calls.append(step)
                        or {"acted": True, "verified": True})

    ap._governor_pass("cp-canary:0.0", state="idle", tail="", cwd=str(tmp_path),
                      ctrl=None, conv="c", evaluate_only=False, conn=None)
    out = ap._governor_pass("cp-canary:0.0", state="idle", tail="", cwd=str(tmp_path),
                            ctrl=None, conv="c", evaluate_only=False, conn=None)
    assert out["decision"] == "governor_queue_pointer_stale"
    assert out["stage"] == "stage_a"
    import sqlite3
    rows = sqlite3.connect(cp).execute(
        "SELECT stage,fields FROM governor_blocker WHERE target='cp-canary:0.0'").fetchall()
    assert rows, "the stall must be durable, not just a log line"
    assert "stage_a" in rows[0][0] and "pointer" in rows[0][1]
    assert len(calls) == 1, "and it must still not re-nudge"


def test_the_governor_never_rewrites_the_projects_queue(tmp_path, monkeypatch):
    """The fix for a stale pointer is the queue OWNER advancing it. A control plane that
    edits a project's durable queue is authoring project state — the one thing the governor
    must never do, however convenient."""
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import commander_autopilot as ap
    from core.control_plane import actuator as act
    monkeypatch.setattr(act, "ENABLED", True)
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset({"cp-canary:0.0"}))
    path = _artefact_queue(tmp_path, nxt_instruction="do the thing")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "A.md").write_text("done")
    before = open(path, encoding="utf-8").read()
    monkeypatch.setattr(cg, "load_config", lambda *a, **k: _canary_cfg(tmp_path, path))
    monkeypatch.setattr(ap, "load_registry", lambda *a, **k: {
        "cp-canary:0.0": {"next_step": "continue with the next safe step"}})
    monkeypatch.setattr(ap, "deliver_next_step",
                        lambda t, step, **k: {"acted": True, "verified": True})
    for _ in range(3):
        ap._governor_pass("cp-canary:0.0", state="idle", tail="", cwd=str(tmp_path),
                          ctrl=None, conv="c", evaluate_only=False, conn=None)
    assert open(path, encoding="utf-8").read() == before, "the queue file must be untouched"


# ═════════ 10. scope enforcement gaps found by probing the SHIPPED config ═══
def test_every_forbidden_scope_in_the_shipped_config_is_enforceable():
    """The dangerous class: `forbidden_scopes: [orders, keys, venue_adapters]` READS like a
    guarantee, but the matcher iterated the MARKER table, so a scope nobody had written a
    regex for refused nothing. Found by probing the live config; the suite had missed it
    because its fixtures only used scopes that happened to have markers."""
    cfg = cg.load_config()
    assert cfg, "shipped config must load"
    for target in cfg:
        assert cg.unenforceable_scopes(target, cfg) == [], target


def test_a_forbidden_scope_with_no_marker_is_still_enforced_by_its_name():
    cfg = {"x:0.0": {"project": "x", "role": "r", "forbidden_scopes": ["quantum_widgets"]}}
    r = cg.check_project_isolation("x:0.0", "build the quantum widgets pipeline", cfg)
    assert r["allowed"] is False and r["scope"] == "quantum_widgets"


@pytest.mark.parametrize("text", [
    "enable live_trading and submit real orders",   # plural broke `\breal order\b`
    "place orders on the venue",
    "rotate the exchange api keys",
    "patch the venue-adapter timeout",              # hyphen broke the word boundary
])
def test_arbitrage2_stays_paper_only_against_the_shipped_config(text):
    """Every one of these was ALLOWED live before the fix."""
    r = cg.check_project_isolation("arbitrage2-opus:0.0", text, cg.load_config())
    assert r["allowed"] is False, text
    assert r["reason"] == "cross_project_work_refused"


def test_underscored_scope_names_are_matched():
    """`_` is a word character, so `\\bmess\\b` never matched "mess_ui" and arbitrage2 could
    have been handed messenger work."""
    r = cg.check_project_isolation("arbitrage2-opus:0.0", "update the mess_ui invites screen",
                                   cg.load_config())
    assert r["allowed"] is False and r["scope"] == "mess_ui"


@pytest.mark.parametrize("target,text", [
    ("mess-qa-automation:0.0", "redesign the invites screen rows and copy"),
    ("cp-canary:0.0", "append one dated line to reports/ACCEPTANCE_A.md"),
    ("arbitrage2-opus:0.0", "summarise paper research findings in a report"),
])
def test_in_role_work_survives_the_broader_markers(target, text):
    """Anti-overcorrection: widening the markers must not start refusing each project's own
    work. These are the real in-role instructions from the live queues."""
    assert cg.check_project_isolation(target, text, cg.load_config())["allowed"] is True


def test_widening_a_scope_never_narrows_another(tmp_path):
    """Regression from this very fix: `deploy` was moved OUT of the publication marker into
    its own scope, which silently un-protected every project that forbids publication but
    not deploy. Scopes may overlap; a token must not vanish from one by being added to
    another."""
    cfg = {"x:0.0": {"project": "x", "role": "r", "forbidden_scopes": ["publication"]}}
    for text in ("git push and deploy to prod", "publish the build", "cut a release",
                 "start the rollout"):
        r = cg.check_project_isolation("x:0.0", text, cfg)
        assert r["allowed"] is False, text
