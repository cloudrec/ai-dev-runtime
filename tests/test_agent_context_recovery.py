import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core import agent_context_recovery as acr

FIXED_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def make_project(tmp_path: Path, name: str, *, state=True, handoff=True, convo=True) -> Path:
    root = tmp_path / name
    root.mkdir(parents=True)
    if state:
        (root / "PROJECT_STATE.md").write_text("# state\nworking on X\n", encoding="utf-8")
    if handoff:
        (root / "HANDOFF.md").write_text("# handoff\nnext: Y\n", encoding="utf-8")
    if convo:
        (root / "CONVERSATION_2026-07-01.md").write_text("log\n", encoding="utf-8")
    return root


def entry(name: str, root: Path, protected=True, session=None) -> acr.ProjectEntry:
    return acr.ProjectEntry(name=name, root=str(root), protected=protected, tmux_session=session)


def test_load_registry_falls_back_to_defaults_when_file_absent(tmp_path):
    entries = acr.load_registry(str(tmp_path / "nope.yaml"))
    names = [e.name for e in entries]
    assert names == ["acap", "mess", "email", "JobHunter"]
    assert [e.protected for e in entries] == [True, True, False, False]


def test_scan_healthy_project_is_ok(tmp_path):
    root = make_project(tmp_path, "acap")
    status = acr.scan_project(entry("acap", root, session="acap"), ["acap"])
    assert status.status == acr.STATUS_OK
    assert status.tmux_alive is True
    assert status.blockers == []
    assert "CONVERSATION_2026-07-01.md" in status.conversation_refs


def test_scan_reports_missing_context_files(tmp_path):
    root = make_project(tmp_path, "mess", state=False, handoff=False, convo=False)
    status = acr.scan_project(entry("mess", root, session="mess"), [])
    assert status.status == acr.STATUS_MISSING
    assert status.tmux_alive is False
    joined = " ".join(status.blockers)
    assert "PROJECT_STATE.md" in joined and "HANDOFF.md" in joined
    assert "no conversation references found" in status.blockers
    assert "tmux session 'mess' not visible" in joined


def test_scan_partial_context_is_degraded(tmp_path):
    root = make_project(tmp_path, "email", handoff=False)
    status = acr.scan_project(entry("email", root, protected=False), [])
    assert status.status == acr.STATUS_DEGRADED


def test_scan_missing_root_is_absent(tmp_path):
    status = acr.scan_project(entry("JobHunter", tmp_path / "gone", protected=False), [])
    assert status.status == acr.STATUS_ABSENT
    assert status.root_exists is False
    assert status.blockers and "does not exist" in status.blockers[0]


def test_scan_never_writes_into_project_root(tmp_path):
    root = make_project(tmp_path, "acap")
    before = sorted(p.name for p in root.iterdir())
    acr.scan_project(entry("acap", root), [])
    assert sorted(p.name for p in root.iterdir()) == before


def test_build_report_flags_protected_projects_needing_reconstruction(tmp_path):
    ok = make_project(tmp_path, "acap")
    broken = make_project(tmp_path, "mess", state=False, handoff=False, convo=False)
    report = acr.build_report(
        [entry("acap", ok), entry("mess", broken)],
        live_sessions=[],
        generated_at=FIXED_NOW,
    )
    assert report["summary"]["protected_needing_reconstruction"] == ["mess"]
    assert report["summary"]["ok"] == 1
    assert report["mode"] == "read-only"
    json.dumps(report)  # report must be JSON-serialisable


def test_render_markdown_lists_every_project(tmp_path):
    root = make_project(tmp_path, "acap")
    report = acr.build_report([entry("acap", root)], live_sessions=["acap"], generated_at=FIXED_NOW)
    text = acr.render_markdown(report)
    assert "# Agent context recovery report" in text
    assert "| acap |" in text


def test_draft_marks_unknowns_and_does_not_invent_history(tmp_path):
    root = make_project(tmp_path, "mess", state=False, handoff=False, convo=False)
    status = acr.scan_project(entry("mess", root), [])
    draft = acr.draft_reconstruction(status, FIXED_NOW)
    assert "DRAFT" in draft
    assert "UNKNOWN" in draft
    assert "`PROJECT_STATE.md`: MISSING" in draft


def test_write_outputs_only_touches_output_dir(tmp_path):
    broken = make_project(tmp_path, "mess", state=False, handoff=False, convo=False)
    project_before = sorted(p.name for p in broken.iterdir())
    report = acr.build_report([entry("mess", broken)], live_sessions=[], generated_at=FIXED_NOW)
    out = tmp_path / "out"
    written = acr.write_outputs(report, output_dir=str(out))
    assert all(str(out) in path for path in written)
    assert (out / "AGENT_CONTEXT_RECOVERY_2026-07-16.json").is_file()
    assert (out / "AGENT_CONTEXT_RECOVERY_2026-07-16.md").is_file()
    assert (out / "drafts" / "mess_PROJECT_STATE.draft.md").is_file()
    assert sorted(p.name for p in broken.iterdir()) == project_before


def test_write_outputs_skips_draft_for_healthy_project(tmp_path):
    root = make_project(tmp_path, "acap")
    report = acr.build_report([entry("acap", root)], live_sessions=["acap"], generated_at=FIXED_NOW)
    out = tmp_path / "out"
    acr.write_outputs(report, output_dir=str(out))
    assert not (out / "drafts").exists()


def test_tmux_sessions_returns_list_when_tmux_absent(monkeypatch):
    monkeypatch.setattr(acr.shutil, "which", lambda _: None)
    assert acr.tmux_sessions() == []


def test_main_exit_code_signals_protected_gap(tmp_path, capsys, monkeypatch):
    broken = make_project(tmp_path, "mess", state=False, handoff=False, convo=False)
    monkeypatch.setattr(acr, "tmux_sessions", lambda: [])
    monkeypatch.setattr(acr, "load_registry", lambda _path: [entry("mess", broken)])
    rc = acr.main(["--no-write", "--registry", "ignored"])
    assert rc == 1
    assert "mess" in capsys.readouterr().out
