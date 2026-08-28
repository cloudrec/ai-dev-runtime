"""Direct Agent Control Plane tests.

The tmux transport (`agent_control._tmux`) is the single seam these tests
replace: everything above it — parsing, validation, duplicate detection,
bounding, delivery proof, idempotency, refusals — is exercised for real against
recorded tmux output, and no test touches a live tmux server or a real agent.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from core import agent_control as ac

PANE_FIXTURE = (
    "safeguard\t0\t0\t%1\t1001\t/opt/safeguard\tnode\t0\tclaude\n"
    "email\t0\t0\t%2\t1002\t/opt/mess\tbash\t0\tshell\n"
    "dead\t0\t0\t%3\t1003\t/opt/old\tbash\t1\tzombie\n"
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """Never touch the production audit log, idempotency DB or real roots."""
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "agent_control.db"))
    monkeypatch.setenv("AGENT_CONTROL_AUDIT", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AGENT_CONTROL_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.delenv("AGENT_CONTROL_ALLOWED_SESSIONS", raising=False)


class FakeTmux:
    """Records argv lists and replays canned responses."""

    def __init__(self, responses=None, panes=PANE_FIXTURE):
        self.calls: list[list[str]] = []
        self.stdins: list[bytes | None] = []
        self.responses = responses or {}
        self.panes = panes
        self.capture_seq: list[str] = []

    def __call__(self, args, stdin=None):
        self.calls.append(list(args))
        self.stdins.append(stdin)
        cmd = args[0]
        if cmd in self.responses:
            return self.responses[cmd]
        if cmd == "list-panes":
            return (0, self.panes, "")
        if cmd == "capture-pane":
            if self.capture_seq:
                return (0, self.capture_seq.pop(0), "")
            return (0, "line-a\nline-b\n", "")
        return (0, "", "")

    def argv_for(self, cmd):
        return [c for c in self.calls if c and c[0] == cmd]


@pytest.fixture
def tmux(monkeypatch):
    fake = FakeTmux()
    monkeypatch.setattr(ac, "_tmux", fake)
    # Only pane 1001 hosts a Claude agent.
    monkeypatch.setattr(ac, "find_claude_in_pane", lambda pid: (
        {"pid": 2001, "cmdline": "claude --resume abc", "cwd": "/opt/safeguard"}
        if pid == 1001 else None))
    return fake


# ── tmux inventory parsing ──────────────────────────────────────────────────
def test_parse_panes_reads_every_field():
    panes = ac.parse_panes(PANE_FIXTURE)
    assert len(panes) == 3
    first = panes[0]
    assert first["session"] == "safeguard"
    assert first["target"] == "safeguard:0.0"
    assert first["pid"] == 1001
    assert first["cwd"] == "/opt/safeguard"
    assert first["command"] == "node"
    assert first["alive"] is True


def test_parse_panes_marks_dead_pane_and_skips_junk():
    panes = ac.parse_panes(PANE_FIXTURE + "garbage-line\n\n")
    assert len(panes) == 3
    assert panes[2]["alive"] is False


def test_agent_list_handles_no_tmux_server(monkeypatch):
    monkeypatch.setattr(ac, "_tmux", lambda a, stdin=None: (1, "", "no server running on /tmp/tmux-0/default"))
    result = ac.agent_list()
    assert result["tmux_running"] is False
    assert result["agents"] == []


# ── existing-agent detection ────────────────────────────────────────────────
def test_agent_list_detects_claude_agent(tmux):
    result = ac.agent_list()
    agents = {a["target"]: a for a in result["agents"]}
    assert agents["safeguard:0.0"]["is_agent"] is True
    assert agents["safeguard:0.0"]["claude_pid"] == 2001
    assert agents["email:0.0"]["is_agent"] is False


def test_is_claude_cmdline_ignores_version_probe():
    assert ac.is_claude_cmdline("/root/.local/bin/claude --resume abc") is True
    assert ac.is_claude_cmdline("claude --version") is False
    assert ac.is_claude_cmdline("vim claude_notes.md") is False
    assert ac.is_claude_cmdline("") is False


def test_find_live_agent_for_dir_matches_cwd(tmux, tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "find_claude_in_pane", lambda pid: (
        {"pid": 2001, "cmdline": "claude", "cwd": str(tmp_path)} if pid == 1001 else None))
    assert ac.find_live_agent_for_dir(str(tmp_path))["claude_pid"] == 2001
    assert ac.find_live_agent_for_dir("/opt/nowhere") is None


# ── duplicate detection + prevention ────────────────────────────────────────
def test_agent_list_reports_duplicate_agents(monkeypatch):
    panes = ("a\t0\t0\t%1\t1001\t/opt/x\tnode\t0\tw\n"
             "b\t0\t0\t%2\t1002\t/opt/x\tnode\t0\tw\n")
    monkeypatch.setattr(ac, "_tmux", FakeTmux(panes=panes))
    monkeypatch.setattr(ac, "find_claude_in_pane",
                        lambda pid: {"pid": pid + 1000, "cmdline": "claude", "cwd": "/opt/x"})
    dupes = ac.agent_list()["duplicates"]
    assert len(dupes) == 1
    assert dupes[0]["count"] == 2
    assert sorted(dupes[0]["targets"]) == ["a:0.0", "b:0.0"]


def test_resume_refuses_when_live_agent_exists(tmux, tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "find_claude_in_pane", lambda pid: (
        {"pid": 2001, "cmdline": "claude", "cwd": str(tmp_path)} if pid == 1001 else None))
    result = ac.agent_resume(str(tmp_path))
    assert result["resumed"] is False
    assert result["duplicate_created"] is False
    assert result["existing_agent"]["claude_pid"] == 2001
    assert not tmux.argv_for("new-session"), "must not start a second agent"


def test_resume_refuses_when_session_name_taken(tmux, tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "find_claude_in_pane", lambda pid: None)
    tmux.responses["has-session"] = (0, "", "")  # session already exists
    result = ac.agent_resume(str(tmp_path), session_name="taken")
    assert result["resumed"] is False
    assert result["duplicate_created"] is False
    assert not tmux.argv_for("new-session")


def test_resume_creates_session_only_when_none_exists(tmux, tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "find_claude_in_pane", lambda pid: None)
    tmux.responses["has-session"] = (1, "", "can't find session")
    result = ac.agent_resume(str(tmp_path), conversation_id="dead-beef-1234",
                             session_name="fresh")
    assert result["resumed"] is True
    assert result["duplicate_created"] is False
    argv = tmux.argv_for("new-session")[0]
    assert argv[:6] == ["new-session", "-d", "-s", "fresh", "-c", str(tmp_path)]
    assert argv[-2:] == ["--resume", "dead-beef-1234"]


def test_resume_rejects_bad_conversation_id(tmux, tmp_path):
    with pytest.raises(ac.AgentControlError):
        ac.agent_resume(str(tmp_path), conversation_id="abc; rm -rf /")


# ── bounded capture ─────────────────────────────────────────────────────────
def test_read_bounds_line_count_to_ceiling(tmux):
    ac.agent_read("safeguard", lines=99999)
    argv = tmux.argv_for("capture-pane")[0]
    assert argv[-1] == f"-{ac.MAX_CAPTURE_LINES}"


def test_read_returns_at_most_requested_lines(monkeypatch):
    """tmux hands back more than asked (-S -N starts N lines above the pane), so
    the slice is the real bound and the reported count must match the output."""
    fake = FakeTmux()
    fake.capture_seq = ["\n".join(f"line{i}" for i in range(50))]
    monkeypatch.setattr(ac, "_tmux", fake)
    result = ac.agent_read("safeguard", lines=10)
    assert len(result["output"].splitlines()) == 10
    assert result["lines_returned"] == 10
    assert result["lines_available"] == 50
    assert result["truncated"] is True
    assert result["output"].splitlines()[-1] == "line49"


def test_read_not_truncated_when_everything_fits(monkeypatch):
    fake = FakeTmux()
    fake.capture_seq = ["a\nb\nc"]
    monkeypatch.setattr(ac, "_tmux", fake)
    result = ac.agent_read("safeguard", lines=10)
    assert result["lines_returned"] == 3
    assert result["truncated"] is False


def test_read_rejects_bad_line_count(tmux):
    with pytest.raises(ac.AgentControlError):
        ac.agent_read("safeguard", lines=0)
    with pytest.raises(ac.AgentControlError):
        ac.agent_read("safeguard", lines="; whoami")


def test_read_redacts_secrets(monkeypatch):
    fake = FakeTmux()
    fake.capture_seq = ["export API_KEY=supersecretvalue\nsk-ant-abcdef123456\nnormal output"]
    monkeypatch.setattr(ac, "_tmux", fake)
    out = ac.agent_read("safeguard")["output"]
    assert "supersecretvalue" not in out
    assert "sk-ant-abcdef123456" not in out
    assert "normal output" in out


def test_redact_covers_common_credential_shapes():
    assert "ghp_" not in ac.redact("token ghp_abcdefghijklmnop1234")
    assert "AKIAIOSFODNN7EXAMPLE" not in ac.redact("AKIAIOSFODNN7EXAMPLE")
    assert "hunter2hunter2" not in ac.redact("password: hunter2hunter2")
    assert "***REDACTED***" in ac.redact("Authorization: Bearer abcdef123456789")


# ── multiline delivery + delivery proof ─────────────────────────────────────
def test_send_delivers_multiline_via_buffer_not_send_keys(tmux):
    text = "line one\nline two\n\nline four with 'quotes' and $VARS"
    result = ac.agent_send("safeguard", text)
    assert result["delivered"] is True
    assert result["agent_created"] is False
    # The payload travels as buffer stdin — never as send-keys arguments, which
    # would be interpreted as key names and could inject key sequences.
    assert tmux.stdins[tmux.calls.index(["load-buffer", "-b", tmux.argv_for("load-buffer")[0][2], "-"])] == text.encode()
    send_keys = tmux.argv_for("send-keys")
    assert send_keys and send_keys[0][-1] == "Enter"
    assert all(text not in " ".join(c) for c in send_keys)


def test_send_proves_delivery_with_pane_diff(monkeypatch):
    fake = FakeTmux()
    fake.capture_seq = ["before state", "before state\n> my message"]
    monkeypatch.setattr(ac, "_tmux", fake)
    # Stub the state-classification captures so they don't consume the delivery
    # before/after sequence this test asserts on.
    monkeypatch.setattr(ac, "_pane_tail", lambda *a, **k: "idle")
    monkeypatch.setattr(ac, "_pane_pending_input", lambda *a, **k: "")
    monkeypatch.setattr(ac, "find_claude_in_pane", lambda pid: {"pid": 2001, "cmdline": "claude", "cwd": "/opt/safeguard"})
    result = ac.agent_send("safeguard", "my message")
    assert result["pane_changed"] is True
    assert "my message" in result["delivery_evidence"]


def test_send_cleans_up_buffer(tmux):
    ac.agent_send("safeguard", "hi")
    assert tmux.argv_for("delete-buffer"), "staged buffer must not be left behind"


def test_send_never_creates_an_agent(tmux):
    with pytest.raises(ac.AgentControlError, match="refusing to create one implicitly"):
        ac.agent_send("nosuchsession", "hello")
    assert not tmux.argv_for("new-session")


def test_send_refuses_dead_pane(tmux):
    with pytest.raises(ac.AgentControlError, match="dead"):
        ac.agent_send("dead", "hello")


def test_send_rejects_oversized_message(tmux):
    with pytest.raises(ac.AgentControlError, match="over the"):
        ac.agent_send("safeguard", "x" * (ac.MAX_MESSAGE_BYTES + 1))


def test_send_rejects_empty_message(tmux):
    with pytest.raises(ac.AgentControlError):
        ac.agent_send("safeguard", "   ")


def test_answer_delivers_like_send(tmux):
    result = ac.agent_answer("safeguard", "yes\ncontinue")
    assert result["delivered"] is True
    assert result["action"] == "agent_answer"


# ── idempotency ─────────────────────────────────────────────────────────────
def test_send_is_idempotent_per_key(tmux):
    first = ac.agent_send("safeguard", "deploy now", idempotency_key="job-42")
    assert first["delivered"] is True and first["duplicate"] is False

    second = ac.agent_send("safeguard", "deploy now", idempotency_key="job-42")
    assert second["duplicate"] is True
    assert second["delivered"] is False
    assert len(tmux.argv_for("paste-buffer")) == 1, "message must be pasted exactly once"


def test_distinct_keys_deliver_separately(tmux):
    ac.agent_send("safeguard", "one", idempotency_key="k1")
    ac.agent_send("safeguard", "two", idempotency_key="k2")
    assert len(tmux.argv_for("paste-buffer")) == 2


def test_send_generates_key_when_absent(tmux):
    result = ac.agent_send("safeguard", "hello")
    assert result["idempotency_key"]


# ── invalid target rejection ────────────────────────────────────────────────
@pytest.mark.parametrize("target", [
    "sess; rm -rf /",
    "sess$(whoami)",
    "sess`id`",
    "sess name",
    "../../etc/passwd",
    "sess:0.0; touch /tmp/pwned",
    "-Cbad",
    "",
    "x" * 100,
    "sess\nkill-server",
])
def test_invalid_targets_are_rejected(target):
    with pytest.raises(ac.AgentControlError):
        ac.validate_target(target)


@pytest.mark.parametrize("target", ["safeguard", "safeguard:0", "safeguard:0.1", "my-agent_2"])
def test_valid_targets_accepted(target):
    assert ac.validate_target(target) == target


def test_session_allowlist_is_enforced(monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_ALLOWED_SESSIONS", "safeguard,email")
    assert ac.validate_session("safeguard") == "safeguard"
    with pytest.raises(ac.AgentControlError, match="not allowlisted"):
        ac.validate_session("other")


def test_read_rejects_invalid_target_before_touching_tmux(monkeypatch):
    called = []
    monkeypatch.setattr(ac, "_tmux", lambda *a, **k: called.append(a) or (0, "", ""))
    with pytest.raises(ac.AgentControlError):
        ac.agent_read("bad;target")
    assert not called, "validation must happen before any tmux call"


# ── path validation ─────────────────────────────────────────────────────────
def test_project_dir_must_be_inside_allowed_roots(tmp_path):
    assert ac.validate_project_dir(str(tmp_path)) == str(os.path.realpath(tmp_path))
    with pytest.raises(ac.AgentControlError, match="outside the allowed roots"):
        ac.validate_project_dir("/etc")


def test_project_dir_rejects_traversal(tmp_path):
    with pytest.raises(ac.AgentControlError, match="outside the allowed roots"):
        ac.validate_project_dir(str(tmp_path / ".." / ".." / "etc"))


def test_project_dir_rejects_symlink_escape(tmp_path):
    escape = tmp_path / "escape"
    escape.symlink_to("/etc")
    with pytest.raises(ac.AgentControlError, match="outside the allowed roots"):
        ac.validate_project_dir(str(escape))


def test_project_dir_rejects_missing_directory(tmp_path):
    with pytest.raises(ac.AgentControlError, match="does not exist"):
        ac.validate_project_dir(str(tmp_path / "nope"))


def test_report_lists_only_allowlisted_subdirs(tmp_path):
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "a.md").write_text("hello")
    (tmp_path / "secret.md").write_text("not a report")
    result = ac.agent_report(str(tmp_path))
    paths = [r["path"] for r in result["reports"]]
    assert "reports/a.md" in paths
    assert "secret.md" not in paths
    assert result["reports"][0]["modified_at"]


def test_report_read_rejects_path_traversal(tmp_path):
    (tmp_path / "reports").mkdir()
    with pytest.raises(ac.AgentControlError):
        ac.agent_report_read(str(tmp_path), "../../../etc/passwd")
    with pytest.raises(ac.AgentControlError):
        ac.agent_report_read(str(tmp_path), "/etc/passwd")


def test_report_read_redacts_and_bounds(tmp_path):
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "r.md").write_text("token=abcdef123456\n" + "x" * 10)
    result = ac.agent_report_read(str(tmp_path), "reports/r.md")
    assert "abcdef123456" not in result["content"]
    assert result["truncated"] is False


# ── stop confirmation ───────────────────────────────────────────────────────
def test_stop_requires_confirmation(tmux):
    with pytest.raises(ac.AgentControlError, match="confirm=true"):
        ac.agent_stop("safeguard")
    assert not tmux.argv_for("kill-pane")


def test_stop_rejects_truthy_non_true_confirm(tmux):
    with pytest.raises(ac.AgentControlError, match="confirm=true"):
        ac.agent_stop("safeguard", confirm="yes")
    assert not tmux.argv_for("kill-pane")


def test_stop_with_confirmation_kills_only_that_pane(tmux):
    result = ac.agent_stop("safeguard", confirm=True)
    assert result["stopped"] is True
    kills = tmux.argv_for("kill-pane")
    assert kills == [["kill-pane", "-t", "safeguard:0.0"]]
    assert not tmux.argv_for("kill-session"), "must never kill a whole session"
    assert not tmux.argv_for("kill-server")


def test_stop_refuses_ambiguous_target(monkeypatch):
    panes = ("dup\t0\t0\t%1\t1001\t/opt/x\tnode\t0\tw\n"
             "dup\t0\t1\t%2\t1002\t/opt/y\tnode\t0\tw\n")
    fake = FakeTmux(panes=panes)
    monkeypatch.setattr(ac, "_tmux", fake)
    monkeypatch.setattr(ac, "find_claude_in_pane", lambda pid: None)
    with pytest.raises(ac.AgentControlError, match="ambiguous"):
        ac.agent_stop("dup", confirm=True)
    assert not fake.argv_for("kill-pane"), "an ambiguous stop must kill nothing"


def test_stop_refuses_unknown_target(tmux):
    with pytest.raises(ac.AgentControlError, match="no pane matches"):
        ac.agent_stop("ghost", confirm=True)


# ── status is read-only ─────────────────────────────────────────────────────
def test_status_does_not_mutate_the_agent(tmux):
    result = ac.agent_status("safeguard")
    assert result["is_agent"] is True
    assert result["alive"] is True
    assert result["claude_pid"] == 2001
    mutating = {"send-keys", "paste-buffer", "kill-pane", "kill-session", "new-session", "load-buffer"}
    assert not [c for c in tmux.calls if c[0] in mutating], "status must be read-only"


def test_status_refuses_unknown_target(tmux):
    with pytest.raises(ac.AgentControlError, match="no tmux pane matches"):
        ac.agent_status("ghost")


# ── audit ───────────────────────────────────────────────────────────────────
def test_actions_are_audited_with_target_and_key(tmux, tmp_path):
    ac.agent_send("safeguard", "hello", idempotency_key="audit-key-1")
    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    entry = [__import__("json").loads(x) for x in lines if "agent_send" in x][-1]
    assert entry["target"] == "safeguard:0.0"
    assert entry["idempotency_key"] == "audit-key-1"
    assert entry["ts"]


def test_audit_failure_never_breaks_the_action(tmux, monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_AUDIT", "/proc/cannot/write/here.jsonl")
    assert ac.agent_read("safeguard")["output"] is not None


# ── no arbitrary command surface ────────────────────────────────────────────
def test_module_exposes_no_arbitrary_command_tool():
    exported = {n for n in dir(ac) if n.startswith("agent_")}
    assert exported == {"agent_list", "agent_status", "agent_read", "agent_send", "agent_answer",
                        "agent_resume", "agent_report", "agent_report_read", "agent_stop"}


def test_tmux_transport_never_uses_a_shell():
    source = open(ac.__file__).read()
    assert "shell=True" not in source
    assert "os.system" not in source


# ── observable state classification ─────────────────────────────────────────
# Real idle tails captured from live panes (2026-07-20). Each ends at the
# "new task?" rest prompt with a PAST-tense spinner — the exact shapes the old
# classifier mislabelled as working.
IDLE_MESS = ("Копирование в веб-папку добавил.\n\n✻ Worked for 1m 47s\n"
             "                          new task? /clear to save 490.5k tokens\n"
             "❯ глянул, всё ок\n  ⏵⏵ auto mode on (shift+tab to cycle) · ← 5 agents")
IDLE_JOB = ("Отчёт reports/2026-07-17_b8.md. Дальше в очереди: B9.\n\n✻ Brewed for 50m 59s\n"
            "                          new task? /clear to save 339.6k tokens\n"
            "❯ гоу B9\n  ⏵⏵ auto mode on (shift+tab to cycle) · ← 5 agents")
IDLE_SAFEGUARD = ("E_GUARD_REAL_AGENT_INPUT_REQUIRED.\n\n✻ Cogitated for 14m 15s\n"
                  "                          new task? /clear to save 194.5k tokens\n"
                  "❯ Запроси у вендора ключ fbe97a3db1a4cba0\n  ⏵⏵ auto mode on")
ACTIVE = ("● Editing core/foo.py\n\n* Nesting… (1m 2s · ↓ 2.4k tokens)\n"
          "  ⎿  Tip: Use /btw to ask a side question\n  esc to interrupt")


def test_classify_state_dead_and_stale():
    assert ac.classify_state(alive=False, is_agent=True, output_tail="anything") == "dead"
    assert ac.classify_state(alive=True, is_agent=False, output_tail="$ ") == "stale"


def test_idle_new_task_prompt_is_not_working():
    # THE defect: a past-tense spinner + "new task?" is idle, not working.
    assert ac.classify_state(True, True, IDLE_MESS) == "idle"
    assert ac.classify_state(True, True, IDLE_JOB) == "idle"


def test_queued_owner_prompt_is_not_working():
    # A queued "❯ гоу B9" line with no fresh execution evidence is idle.
    assert ac.classify_state(True, True, IDLE_JOB) == "idle"


def test_externally_blocked_from_input_required():
    assert ac.classify_state(True, True, IDLE_SAFEGUARD) == "externally_blocked"
    assert ac.classify_state(True, True, "Waiting for API... retry (429 rate limit)\nnew task?") == "externally_blocked"


def test_commander_events_durable_and_deduped(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("AGENT_CONTROL_AUDIT", str(tmp_path / "audit.jsonl"))
    assert ac.record_commander_event("seo-audit:0.0", "seo", "checkpoint_completed_work_remaining",
                                     {"remaining_id": "part-e"}, dedup_key="part-e") is True
    # identical unacked event within the window → deduped (not re-emitted every sweep).
    assert ac.record_commander_event("seo-audit:0.0", "seo", "checkpoint_completed_work_remaining",
                                     {"remaining_id": "part-e"}, dedup_key="part-e") is False
    # a different task id → a new event.
    assert ac.record_commander_event("seo-audit:0.0", "seo", "checkpoint_completed_work_remaining",
                                     {"remaining_id": "part-f"}, dedup_key="part-f") is True
    evs = ac.list_commander_events(unacked_only=True)
    assert len(evs) == 2 and evs[0]["event_type"] == "checkpoint_completed_work_remaining"
    ac.ack_commander_events([e["id"] for e in evs])
    assert ac.list_commander_events(unacked_only=True) == []


def test_pending_input_text_detects_queued_instruction():
    # non-empty input line = queued instruction → /clear must refuse.
    pane = ("──────\n❯ enable premium for the canary account and test one charge\n──────\n"
            "  ⏵⏵ auto mode on")
    assert ac.pending_input_text("x:0.0", tail=pane) == "enable premium for the canary account and test one charge"
    # empty input line → safe.
    assert ac.pending_input_text("x:0.0", tail="──────\n❯ \n──────\n auto mode on") == ""
    # a numbered menu selection is NOT input-line text.
    assert ac.pending_input_text("x:0.0", tail="Do you want to proceed?\n❯ 1. Yes\n  2. No") == ""


# ── _pane_pending_input dim recall-ghost fix (gaika-server 2026-08-28) ────────
# waiting_transitions/classify_state kept reading Claude Code's own dim recall-
# ghost redraw of its last completed turn — varying text each tick ("check
# status", "check status tomorrow", "next safe roadmap item…") — as a genuine
# staged command, so an intentionally idle/parked agent (pending=null in every
# other consumer) kept getting fresh false `waiting_input`/actionable wakes.

def _dim_pane(content: str) -> str:
    return f"──────\n❯ \x1b[2m{content}\x1b[0m\n──────\n  ⏵⏵ auto mode on"


def test_dim_recall_ghost_matching_last_submitted_is_not_pending(monkeypatch):
    monkeypatch.setattr(ac, "_tmux", lambda a, stdin=None: (0, _dim_pane("check status"), ""))
    monkeypatch.setattr(ac, "last_submitted_text", lambda cwd: "check status")
    assert ac._pane_pending_input("gaika-server:0.0", cwd="/opt/gaika-server") == ""


def test_dim_text_not_matching_last_submitted_is_still_real_pending(monkeypatch):
    # dim rendering alone never decides ghost-vs-staged (mess-qa-automation 2026-08-05):
    # a dim line that does NOT match what was actually last submitted is real input.
    monkeypatch.setattr(ac, "_tmux", lambda a, stdin=None: (0, _dim_pane("continue with slice 2"), ""))
    monkeypatch.setattr(ac, "last_submitted_text", lambda cwd: "an unrelated earlier command")
    assert ac._pane_pending_input("mess-qa-automation:0.0", cwd="/opt/mess") == "continue with slice 2"


def test_no_cwd_keeps_old_behaviour_unchanged(monkeypatch):
    # callers that cannot supply a cwd (e.g. context_budget's /clear-safety check) must see
    # EXACTLY the pre-fix behaviour: a dim ghost still reads as non-empty pending text.
    monkeypatch.setattr(ac, "_tmux", lambda a, stdin=None: (0, _dim_pane("check status"), ""))
    monkeypatch.setattr(ac, "last_submitted_text", lambda cwd: "check status")
    out = ac._pane_pending_input("gaika-server:0.0")
    assert out and out.strip()             # still truthy — no behaviour change without cwd


def test_plain_non_dim_pending_text_unaffected_by_cwd(monkeypatch):
    monkeypatch.setattr(ac, "_tmux", lambda a, stdin=None: (
        0, "──────\n❯ deploy the fix to staging\n──────\n  ⏵⏵ auto mode on", ""))
    monkeypatch.setattr(ac, "last_submitted_text", lambda cwd: "deploy the fix to staging")
    assert ac._pane_pending_input("x:0.0", cwd="/opt/x") == "deploy the fix to staging"


def test_end_to_end_ghost_no_longer_classifies_as_waiting_input(monkeypatch):
    """The exact gaika-server shape: an at-rest pane whose only 'signal' is the dim
    recall-ghost redraw must classify idle, not waiting_input — so
    waiting_transitions never sees an edge into waiting and never re-wakes."""
    monkeypatch.setattr(ac, "_tmux", lambda a, stdin=None: (0, _dim_pane("check status tomorrow"), ""))
    monkeypatch.setattr(ac, "last_submitted_text", lambda cwd: "check status tomorrow")
    pending = ac._pane_pending_input("gaika-server:0.0", cwd="/opt/gaika-server")
    idle_tail = "✻ Sautéed for 24s · done 3:43 PM\n  ⏵⏵ auto mode on"
    assert ac.classify_state(True, True, idle_tail, pending_input=pending) == "idle"


def test_detect_exec_mode():
    assert ac.detect_exec_mode("⏵⏵ auto mode on (shift+tab to cycle) · ← 3 agents") == "auto"
    assert ac.detect_exec_mode("⏵⏵ accept edits on (shift+tab to cycle)") == "accept_edits"
    assert ac.detect_exec_mode("⏸ plan mode on (shift+tab to cycle)") == "plan"
    assert ac.detect_exec_mode("normal footer · shift+tab to cycle") == "normal"
    assert ac.detect_exec_mode("some unrelated pane text") == "unknown"


def test_ensure_auto_mode_noop_when_already_auto(monkeypatch):
    sent = []
    monkeypatch.setattr(ac, "_tmux", lambda a: sent.append(a) or (0, "", ""))
    r = ac.ensure_auto_mode("seo-audit:0.0", tail_fn=lambda: "⏵⏵ auto mode on")
    assert r["action"] == "none" and r["already"] is True
    assert sent == []                                   # no keystroke when already auto


def test_ensure_auto_mode_restores_from_normal(monkeypatch):
    sent = []
    monkeypatch.setattr(ac, "_tmux", lambda a: sent.append(a[-1]) or (0, "", ""))
    # first read = normal; after one BTab the footer reads auto mode on.
    reads = iter(["normal footer · shift+tab to cycle", "⏵⏵ auto mode on"])
    r = ac.ensure_auto_mode("seo-audit:0.0", tail_fn=lambda: next(reads))
    assert r["action"] == "restored" and r["mode"] == "auto"
    assert sent == ["BTab"]                             # exactly one Shift+Tab


def test_ensure_auto_mode_unknown_is_left(monkeypatch):
    sent = []
    monkeypatch.setattr(ac, "_tmux", lambda a: sent.append(a) or (0, "", ""))
    r = ac.ensure_auto_mode("seo-audit:0.0", tail_fn=lambda: "unreadable")
    assert r["action"] == "none" and sent == []        # never guess an unknown mode


def test_permission_dialog_with_external_word_in_command_is_waiting_owner():
    # A permission dialog whose COMMAND contains an external-looking word must be
    # waiting_owner, never mis-escalated as externally_blocked (the 2026-07-22 bug:
    # `timeout 300 npx tsc` matched the external heuristic → false escalation).
    pane = (" Bash command\n   cd /opt/seo; timeout 300 npx tsc --noEmit\n"
            "   TypeScript typecheck\n\n Do you want to proceed?\n ❯ 1. Yes\n   2. No")
    assert ac.classify_state(True, True, pane) == "waiting_owner"
    # the bare `timeout` command is not the phrase "timed out"
    assert ac.classify_state(True, True, "running timeout 300 pytest\nesc to interrupt") == "working"
    # benign shell output ("timed out", "network error") is NOT an external block —
    # that mis-classified a capacity agent running a live shell (owner-confirmed).
    assert ac.classify_state(True, True, "job timed out after 30s\nnew task?") == "idle"
    assert ac.classify_state(True, True, "curl: network error\n❯ ") == "idle"
    # a real agent-level external block still classifies as externally_blocked.
    assert ac.classify_state(True, True, "Blocked: vendor key required to continue\n❯ ") == "externally_blocked"


def test_working_requires_active_execution_evidence():
    assert ac.classify_state(True, True, ACTIVE) == "working"                       # live spinner + esc to interrupt
    assert ac.classify_state(True, True, "streaming ↓ 5.1k tokens") == "working"     # streaming counter
    assert ac.classify_state(True, True, "esc to interrupt") == "working"


def test_stale_spinner_glyph_alone_is_not_working():
    # A bare ✻ glyph or past-tense "Worked for" must not read as working.
    assert ac.classify_state(True, True, "✻ Worked for 3m\nnew task?") == "idle"
    assert ac.classify_state(True, True, "some old output · 12k tokens saved\nnew task?") == "idle"


def test_output_difference_alone_is_not_working():
    # A changed/stale-cache tail with NO active-execution indicator must not read
    # as working — regression for the false security idle→working transition.
    assert ac.classify_state(True, True, "line A\nline B\nline C", prev_tail="line A") == "idle"
    assert ac.classify_state(True, True, "line A", prev_tail="line A") == "idle"
    assert ac.classify_state(True, True, "totally new text", prev_tail="old cached text") == "idle"


# Exact live capture (2026-07-20): security finished its report and parked at an
# empty "❯" prompt — a PAST-tense "Brewed for 11m 24s" spinner, no "new task?"
# line, no active indicator. This was falsely classified "working" via the old
# progression-vs-stale-cache path.
SECURITY_DONE_REPORT = (
    "  Коммиты (локальный master, remote нет)\n\n"
    "  ee8fc1b Report: identity meta-layer for\n  \"who are you?\"\n"
    "  Другие проекты не трогал. Дерево чистое.\n  Отчёт:\n"
    "  reports/IDENTITY_META_LAYER_20260720.md.\n\n"
    "✻ Brewed for 11m 24s\n\n"
    "──────────────────────────────────────────\n❯\xa0\n"
    "──────────────────────────────────────────\n"
    "  [CAVEMAN]\n  ⏵⏵ auto mode on (shift+tab to cycle) ·")


def test_finished_report_empty_prompt_is_idle_not_working():
    assert ac.classify_state(True, True, SECURITY_DONE_REPORT) == "idle"
    # Even with a stale differing prior sample, it stays idle (never "working").
    assert ac.classify_state(True, True, SECURITY_DONE_REPORT, prev_tail="some older pane text") == "idle"


def test_waiting_owner_when_claude_asks():
    assert ac.classify_state(True, True, "Do you want me to proceed? (y/n)") == "waiting_owner"


def test_agent_states_vocabulary_is_stable():
    assert set(ac.AGENT_STATES) == {"working", "shell_running", "waiting_input", "idle",
                                    "waiting_owner", "externally_blocked", "completed",
                                    "dead", "stale"}


def test_shell_running_and_waiting_input_and_no_false_external():
    # a live shell command in the pane → shell_running (work), not idle/blocked.
    assert ac.classify_state(True, True, "some scrollback\n❯ ", shell_running=True) == "shell_running"
    # a typed/pasted but unsubmitted command → waiting_input (never lost as idle).
    assert ac.classify_state(True, True, "prev output\n", pending_input="[Pasted text #1 +9 lines]") == "waiting_input"
    assert ac.classify_state(True, True, "prev output\n", pending_input="  ") == "idle"   # empty/ghost
    # active run beats the new signals.
    assert ac.classify_state(True, True, "esc to interrupt", shell_running=True,
                             pending_input="x") == "working"


# ── monitoring-only session must not read as a stall ────────────────────────
# Owner OS repeatedly raised `agent_waiting_input`/idle for owner-os-opus-windows
# while that session's whole job was running live background monitors. `idle` is a
# poke/continuation candidate, so a healthy watcher looked stalled. The footer mode
# line carries a live "· N monitors ·" counter, which is the same class of evidence
# as the "· N shells ·" marker the active-run regex already knows.
MONITOR_AT_REST = (
    "  Repo untouched. Silent until something real.\n\n"
    "✻ Brewed for 3m 58s · done 11:43 PM\n\n"
    "─" * 40 + "\n"
    "❯ \n"
    + "─" * 40 + "\n"
    "  [CAVEMAN]\n"
    "  ⏵⏵ auto mode on · 2 monitors · ← 3 agents\n"
)


def test_active_monitors_with_idle_prompt_are_not_a_stall():
    # (1) live monitors + an EMPTY composer is work in progress, not idle and never
    # a false waiting_input.
    state = ac.classify_state(True, True, MONITOR_AT_REST, pending_input="", shell_running=False)
    assert state == "shell_running"
    assert state != "waiting_input"
    # singular renders as "1 monitor"
    one = MONITOR_AT_REST.replace("2 monitors", "1 monitor")
    assert ac.classify_state(True, True, one, pending_input="") == "shell_running"


def test_genuine_pending_prompt_still_waiting_input_with_monitors_running():
    # (2) monitors running does NOT mask a real staged line the owner must submit.
    assert ac.classify_state(True, True, MONITOR_AT_REST,
                             pending_input="keep monitoring") == "waiting_input"
    # a real dialog still outranks the monitor signal too
    dialog = MONITOR_AT_REST.replace("❯ \n", "❯ Do you want to proceed?\n")
    assert ac.classify_state(True, True, dialog, pending_input="") == "waiting_owner"


def test_no_monitors_idle_pane_stays_idle():
    # (3) the signal must not leak into ordinary at-rest panes.
    plain = MONITOR_AT_REST.replace(" · 2 monitors", "")
    assert ac.classify_state(True, True, plain, pending_input="") == "idle"
    assert ac.classify_state(True, True, IDLE_MESS, pending_input="") == "idle"


def test_stale_or_crashed_monitor_evidence_does_not_count_as_active():
    # (4) the FROZEN turn-summary line keeps claiming monitors after they have
    # stopped or died. Only the live footer counter counts.
    stale_prose = (
        "  earlier report\n\n"
        "✻ Sautéed for 1m 11s · done 11:46 PM · 2 monitors still running\n\n"
        + "─" * 40 + "\n"
        "❯ \n"
        + "─" * 40 + "\n"
        "  ⏵⏵ auto mode on · ← 3 agents\n"
    )
    assert ac.classify_state(True, True, stale_prose, pending_input="") == "idle"
    # the wrapped form of the same prose (count and word split across lines)
    wrapped = stale_prose.replace("· 2 monitors still running",
                                  "· 2\n  monitors still running")
    assert ac.classify_state(True, True, wrapped, pending_input="") == "idle"
    # every monitor stopped: a zero counter is not work
    assert ac.classify_state(True, True, MONITOR_AT_REST.replace("2 monitors", "0 monitors"),
                             pending_input="") == "idle"
    # a monitors counter buried in deep scrollback is not the current footer
    buried = MONITOR_AT_REST + ("  filler line\n" * 60) + "❯ \n  ⏵⏵ auto mode on\n"
    assert ac.classify_state(True, True, buried, pending_input="") == "idle"


def test_monitor_signal_never_overrides_a_real_external_block():
    blocked = MONITOR_AT_REST.replace("  [CAVEMAN]\n",
                                      "  Blocked: vendor key required to continue\n")
    assert ac.classify_state(True, True, blocked, pending_input="") == "externally_blocked"
