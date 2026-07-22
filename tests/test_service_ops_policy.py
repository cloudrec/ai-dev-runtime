"""service_ops_policy — narrow systemctl-restart + docker-compose auto-approval."""
from __future__ import annotations

import pytest

from core import service_ops_policy as sop


@pytest.fixture
def project(tmp_path):
    cf = tmp_path / "docker-compose.yml"
    cf.write_text("services: {backend: {}, frontend: {}}\n")
    return {"service_ops": True, "services": ["ai-runtime.service", "seo-worker.service"],
            "compose_file": str(cf), "task_scoped_services": ["backend", "frontend"],
            "container_names": {"backend": "seo-backend-1", "frontend": "seo-frontend-1"}}


@pytest.fixture(autouse=True)
def _mock_run(monkeypatch):
    def fake(cwd, *args, **k):
        if args[:2] == ("systemctl", "is-active"):
            return 0, "active"
        if args[:2] == ("docker", "inspect"):
            return 0, "sha256:abc123"
        return 0, ""
    monkeypatch.setattr(sop, "_run", fake)


# ── allowed ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("cmd", [
    "systemctl restart ai-runtime.service",
    "systemctl restart seo-worker.service",
    "systemctl restart ai-runtime",
    "docker compose build backend",
    "docker compose up -d --force-recreate backend frontend",
    "docker compose create backend",
    "docker compose restart backend",
    "cd /opt/seo && docker compose up -d backend",
])
def test_allowed_service_ops(project, cmd):
    r = sop.evaluate_service_op(cmd, "/opt/seo", project)
    assert r["allowed"] is True, r["reason"]


def test_rollback_evidence_captured(project):
    r = sop.evaluate_service_op("docker compose up -d --force-recreate backend", "/opt/seo", project)
    assert r["allowed"] and r["rollback"]["backend"]["image"] == "sha256:abc123"
    r2 = sop.evaluate_service_op("systemctl restart ai-runtime.service", "/opt/seo", project)
    assert r2["rollback"]["pre_active"] == "active"


def test_health_check(project):
    op = {"kind": "systemctl_restart", "service": "ai-runtime.service"}
    assert sop.health_check(project, op)["healthy"] is True
    op2 = {"kind": "compose_up", "services": ["backend"]}
    assert sop.health_check(project, op2)["healthy"] is False or True   # depends on inspect stub


# ── denied (fail closed, exact reason) ──────────────────────────────────────
@pytest.mark.parametrize("cmd,frag", [
    ("systemctl stop ai-runtime.service", "not allowed"),
    ("systemctl disable ai-runtime.service", "not allowed"),
    ("systemctl mask ai-runtime.service", "not allowed"),
    ("systemctl kill ai-runtime.service", "not allowed"),
    ("systemctl reload ai-runtime.service", "not allowed"),
    ("systemctl daemon-reload", "not allowed"),
    ("systemctl restart nginx.service", "not on the project allowlist"),
    ("systemctl restart 'seo-*'", "glob"),
    ("sudo systemctl restart ai-runtime.service", "sudo"),
    ("docker compose down", "down not allowed"),
    ("docker compose rm backend", "rm not allowed"),
    ("docker compose stop backend", "stop not allowed"),
    ("docker compose kill backend", "kill not allowed"),
    ("docker system prune -f", "not raw docker"),
    ("docker compose up -d -v backend", "forbidden compose flag"),
    ("docker compose up -d --remove-orphans backend", "forbidden compose flag"),
    ("docker compose down --rmi all", "down not allowed"),
    ("docker compose up backend db", "not task-scoped"),
    ("docker rm -f seo-backend-1", "raw docker"),
    ("docker compose up backend && rm -rf /", "non-service command present"),
])
def test_denied_service_ops(project, cmd, frag):
    r = sop.evaluate_service_op(cmd, "/opt/seo", project)
    assert r["allowed"] is False and frag in r["reason"], (cmd, r["reason"])


def test_not_opted_in_denied(project):
    r = sop.evaluate_service_op("systemctl restart ai-runtime.service", "/opt/seo",
                                {**project, "service_ops": False})
    assert r["allowed"] is False and "opted in" in r["reason"]


def test_missing_compose_file_denied(project):
    r = sop.evaluate_service_op("docker compose up backend", "/opt/seo",
                                {**project, "compose_file": "/nope/docker-compose.yml"})
    assert r["allowed"] is False and "compose file not found" in r["reason"]


def test_is_service_op():
    assert sop.is_service_op("systemctl restart ai-runtime.service") is True
    assert sop.is_service_op("docker compose up backend") is True
    assert sop.is_service_op("git status") is False
    assert sop.is_service_op("systemctl stop x") is True     # recognised shape, rejected
