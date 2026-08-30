"""Agent Fabric v1 (task OWNER-192): unified inventory over tmux + runtime,
fail-closed lifecycle verbs, no-duplicate start semantics."""
import pytest

from core import agent_fabric as af
from core import job_store


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    job_store.init_db()


def _job(**kw):
    base = dict(project_path="/opt/seo", task_id=200, goal="SEO validation",
                status="queued", approval_required=0)
    base.update(kw)
    return job_store.create_job(**base)


# ── refs ────────────────────────────────────────────────────────────────────

def test_parse_ref_shapes():
    assert af.parse_ref("tmux:gaika:0.0") == ("tmux", "gaika:0.0")
    j = "runtime:abc-123"
    assert af.parse_ref(j) == ("runtime", "abc-123")
    for bad in ("", "x", "pane:1", "tmux:", "runtime:"):
        with pytest.raises(af.FabricError):
            af.parse_ref(bad)


# ── unified inventory ───────────────────────────────────────────────────────

def test_list_unifies_tmux_and_runtime(monkeypatch):
    from core import agent_control
    monkeypatch.setattr(agent_control, "agent_list", lambda: {"agents": [
        {"target": "seo-agent:0.0", "is_agent": True, "alive": True,
         "state": "working", "claude_cwd": "/opt/seo"},
        {"target": "not-agent:0.0", "is_agent": False},
    ]})
    j = _job()
    out = af.list_agents()
    refs = {e["ref"] for e in out["agents"]}
    assert "tmux:seo-agent:0.0" in refs
    assert f"runtime:{j['id']}" in refs
    assert "tmux:not-agent:0.0" not in refs
    tmux = next(e for e in out["agents"] if e["kind"] == "tmux")
    assert tmux["fabric_state"] == "WORKING" and tmux["project"] == "seo"
    rt = next(e for e in out["agents"] if e["kind"] == "runtime")
    assert rt["fabric_state"] == "CREATED" and rt["project"] == "seo"


def test_terminal_jobs_hidden_by_default(monkeypatch):
    from core import agent_control
    monkeypatch.setattr(agent_control, "agent_list", lambda: {"agents": []})
    j = _job()
    job_store.update_job(j["id"], status="completed")
    ref = f"runtime:{j['id']}"
    assert ref not in {e["ref"] for e in af.list_agents()["agents"]}
    assert ref in {e["ref"] for e in
                   af.list_agents(include_terminal_jobs=True)["agents"]}


def test_one_source_down_does_not_blind_the_other(monkeypatch):
    from core import agent_control
    def _boom():
        raise RuntimeError("tmux server gone")
    monkeypatch.setattr(agent_control, "agent_list", _boom)
    j = _job()
    out = af.list_agents()
    assert any(e["ref"] == f"runtime:{j['id']}" for e in out["agents"])
    assert any("tmux_inventory_unavailable" in e for e in out["errors"])


def test_runtime_state_mapping():
    cases = {"waiting_approval": "OWNER_DECISION", "failed": "VERIFICATION_FAILED",
             "fallback_plan_only": "BLOCKED", "completed": "AGENT_DONE"}
    for status, want in cases.items():
        assert af._runtime_entry({"id": "x", "status": status})["fabric_state"] == want


# ── lifecycle verbs, fail-closed ────────────────────────────────────────────

def test_start_or_resume_refuses_duplicate(monkeypatch):
    from core import agent_control
    monkeypatch.setattr(agent_control, "find_live_agent_for_dir",
                        lambda d: {"target": "seo-agent:0.0"})
    called = []
    monkeypatch.setattr(agent_control, "agent_resume",
                        lambda *a, **k: called.append(1))
    out = af.start_or_resume("/opt/seo")
    assert out["duplicate_prevented"] is True
    assert out["ref"] == "tmux:seo-agent:0.0"
    assert not called, "must never start a second agent for a live cwd"


def test_start_or_resume_delegates_when_no_duplicate(monkeypatch):
    from core import agent_control
    monkeypatch.setattr(agent_control, "find_live_agent_for_dir", lambda d: None)
    monkeypatch.setattr(agent_control, "agent_resume",
                        lambda d, conversation_id=None: {"ok": True, "target": "t:0.0"})
    out = af.start_or_resume("/opt/seo")
    assert out["ok"] and out["resumed"] and not out["duplicate_prevented"]


def test_send_refuses_runtime_workers():
    j = _job()
    with pytest.raises(af.FabricError, match="no interactive input"):
        af.send(f"runtime:{j['id']}", "hello")


def test_stop_requires_confirm_and_cancels_runtime_job():
    j = _job()
    with pytest.raises(af.FabricError, match="confirm"):
        af.stop(f"runtime:{j['id']}")
    out = af.stop(f"runtime:{j['id']}", confirm=True)
    assert out["ok"] and job_store.get_job(j["id"])["status"] == "cancelled"
    # idempotent: terminal stays terminal
    again = af.stop(f"runtime:{j['id']}", confirm=True)
    assert again["already_terminal"] is True


def test_result_returns_runtime_job_evidence():
    j = _job()
    job_store.update_job(j["id"], status="failed", error="boom",
                         tests={"ok": False})
    out = af.result(f"runtime:{j['id']}")
    assert out["status"] == "failed" and out["error"] == "boom"


def test_status_unknown_job_is_a_refusal():
    with pytest.raises(af.FabricError, match="no such runtime job"):
        af.status("runtime:does-not-exist")
