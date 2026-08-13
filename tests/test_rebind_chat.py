"""The rebind CLI: validate, back up, bind through the official API, and PROVE the result.

Every test here runs against a temp control-plane database. The production pointer is what
this script exists to change safely; a test suite that could change it by accident would be
the exact failure it is meant to prevent.
"""
from __future__ import annotations

import io
import os

import pytest

from core import wake_bridge as wb
from tools import rebind_chat as rc

NEW = "https://chatgpt.com/c/6a7d37d0-02dc-83ed-9ef4-d26156937c57"
OLD = "https://chatgpt.com/c/6a7a9736-2f18-83eb-bca5-cc55db60fa7a"


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    """A private database AND a private backup directory, per test."""
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setattr(rc, "_BACKUP_DIR", str(tmp_path / "backups"))
    yield


def _run(*args, **kw):
    """Run the rebind and return (exit_code, printed_output)."""
    out = io.StringIO()
    code = rc.rebind(*args, out=out, **kw)
    return code, out.getvalue()


# ── URL validation: one rule, shared with the bridge ───────────────────────
@pytest.mark.parametrize("url", [
    "https://chatgpt.com/c/6a7d37d0-02dc-83ed-9ef4-d26156937c57",
    "https://chatgpt.com/c/6a7d37d0-02dc-83ed-9ef4-d26156937c57/",
    "https://chat.openai.com/c/6a7d37d0-02dc-83ed-9ef4-d26156937c57",
])
def test_conversation_urls_are_accepted(url):
    assert wb.valid_conversation(url) is True
    assert _run(url)[0] == 0


@pytest.mark.parametrize("url", [
    "",
    "   ",
    "6a7d37d0-02dc-83ed-9ef4-d26156937c57",              # bare id, not a URL
    "http://chatgpt.com/c/abc",                           # not https
    "https://evil.example.com/c/abc",                     # not ChatGPT
    "https://chatgpt.com/c/abc?share=1",                  # query string
    "https://chatgpt.com/c/abc/extra",                    # deeper path
    "javascript:alert(1)",
])
def test_anything_that_is_not_a_conversation_url_is_refused(url):
    """Fail closed. A bad URL must be rejected BEFORE the pointer is touched."""
    code, out = _run(url)
    assert code == 1
    assert "FAIL" in out


def test_the_cli_uses_the_bridges_own_validator(monkeypatch):
    """One rule, not two. A URL the CLI accepts can never be one the bridge later calls
    `active_chat_invalid` — so the CLI must not carry a private copy of the pattern."""
    monkeypatch.setattr(wb, "valid_conversation", lambda u: False)
    assert _run(NEW)[0] == 1


def test_a_refused_url_leaves_the_existing_target_untouched():
    wb.bind_chat(OLD)
    assert _run("https://evil.example.com/c/abc")[0] == 1
    assert wb.active_chat()["conversation"] == OLD


# ── the rebind itself ──────────────────────────────────────────────────────
def test_rebind_moves_the_pointer_and_reports_pass():
    wb.bind_chat(OLD)
    code, out = _run(NEW)
    assert code == 0
    assert "PASS" in out
    assert wb.active_chat()["conversation"] == NEW


def test_the_previous_target_is_shown_and_audited():
    """The old URL must be recoverable afterwards — from the output and from the audit."""
    wb.bind_chat(OLD)
    code, out = _run(NEW, note="rotation")
    assert code == 0
    assert OLD in out                      # visible before/after, not a silent overwrite
    latest = wb.bind_history(1)[0]
    assert latest["conversation"] == NEW
    assert latest["previous"] == OLD
    assert latest["action"] == "rebind"
    assert latest["note"] == "rotation"


def test_rebinding_to_the_same_conversation_is_a_no_op_pass():
    """Re-running the command must be safe: no second audit row, no wasted backup."""
    wb.bind_chat(NEW)
    before = len(wb.bind_history(50))
    code, out = _run(NEW)
    assert code == 0 and "PASS" in out
    assert len(wb.bind_history(50)) == before


def test_a_trailing_slash_is_treated_as_the_same_conversation():
    wb.bind_chat(NEW)
    code, out = _run(NEW + "/")
    assert code == 0
    assert "nothing to change" in out


def test_binding_the_first_target_works_with_nothing_bound():
    assert wb.active_chat()["bound"] is False
    assert _run(NEW)[0] == 0
    assert wb.active_chat()["conversation"] == NEW


# ── verification is a fresh read, not the writer's word ────────────────────
def test_fail_when_the_pointer_does_not_actually_hold_the_new_url(monkeypatch):
    """`bind_chat` reporting success is not evidence. Only re-reading the pointer is.

    This is the failure the script is built to catch: a write that returns ok while the
    row the bridge will read on its next wake says something else.
    """
    monkeypatch.setattr(wb, "bind_chat",
                        lambda url, **kw: {"ok": True, "action": "rebind", "previous": OLD})
    monkeypatch.setattr(wb, "active_chat",
                        lambda **kw: {"bound": True, "conversation": OLD})
    code, out = _run(NEW)
    assert code == 1
    assert "FAIL" in out and "PASS" not in out


def test_fail_when_bind_chat_refuses(monkeypatch):
    """A refusal from the official API ends in FAIL, never a silent pass."""
    monkeypatch.setattr(wb, "bind_chat",
                        lambda url, **kw: {"ok": False, "reason": "not_a_conversation_url"})
    code, out = _run(NEW)
    assert code == 1
    assert "FAIL" in out and "not_a_conversation_url" in out


def test_fail_when_the_pointer_reads_unbound_afterwards(monkeypatch):
    monkeypatch.setattr(wb, "bind_chat", lambda url, **kw: {"ok": True, "action": "bind"})
    monkeypatch.setattr(wb, "active_chat",
                        lambda **kw: {"bound": False, "reason": "no_active_control_chat"})
    code, out = _run(NEW)
    assert code == 1 and "FAIL" in out


# ── backup ─────────────────────────────────────────────────────────────────
def test_a_point_backup_is_written_before_the_pointer_moves(tmp_path):
    wb.bind_chat(OLD)
    code, out = _run(NEW)
    assert code == 0
    d = str(tmp_path / "backups")
    files = os.listdir(d)
    assert len(files) == 1
    body = open(os.path.join(d, files[0]), encoding="utf-8").read()
    # The point of the backup is that the OLD url is recoverable by eye.
    assert OLD in body
    assert "wake_target" in body
    assert d in out


def test_the_backup_holds_only_the_pointer_tables():
    """Not a copy of the whole control plane — that database is live, shared and large."""
    wb.bind_chat(OLD)
    _run(NEW)
    path = rc.backup_pointer()
    body = open(path, encoding="utf-8").read()
    for line in body.splitlines():
        if line.startswith(("CREATE TABLE", "INSERT INTO")):
            assert "wake_target" in line or "wake_bind_audit" in line


def test_no_backup_flag_skips_the_dump(tmp_path):
    wb.bind_chat(OLD)
    assert _run(NEW, do_backup=False)[0] == 0
    assert not os.path.isdir(str(tmp_path / "backups"))


# ── dry run ────────────────────────────────────────────────────────────────
def test_dry_run_validates_but_changes_nothing(tmp_path):
    wb.bind_chat(OLD)
    code, out = _run(NEW, dry_run=True)
    assert code == 0
    assert "dry-run" in out
    assert wb.active_chat()["conversation"] == OLD
    assert not os.path.isdir(str(tmp_path / "backups"))


def test_dry_run_still_refuses_an_invalid_url():
    code, out = _run("not-a-url", dry_run=True)
    assert code == 1 and "FAIL" in out


# ── argument surface ───────────────────────────────────────────────────────
def test_show_does_not_require_a_url(capsys):
    wb.bind_chat(NEW)
    assert rc.main(["--show"]) == 0
    assert NEW in capsys.readouterr().out


def test_a_url_is_required_when_not_showing():
    with pytest.raises(SystemExit):
        rc.main([])


def test_main_returns_the_failure_code_for_a_bad_url(capsys):
    assert rc.main(["https://evil.example.com/c/abc"]) == 1
    assert "FAIL" in capsys.readouterr().out


# ── what the companion will actually read ──────────────────────────────────
def test_the_wake_path_resolves_the_new_target_without_a_restart(monkeypatch):
    """The whole reason no restart is needed: the target is read at wake time.

    `pending_wake()` is the exact call `tools/wake_companion.py` makes each tick, so if it
    reports the new conversation, the running service is already sending there.
    """
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "0")
    wb.bind_chat(OLD)
    d = wb.should_wake(event_id=1, severity="critical", now=1000.0)
    wb.record(d, event_id=1, severity="critical", now=1000.0)
    assert wb.pending_wake()["conversation"] == OLD

    assert _run(NEW)[0] == 0
    assert wb.pending_wake()["conversation"] == NEW
