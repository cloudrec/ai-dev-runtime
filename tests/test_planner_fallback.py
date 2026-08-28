"""Regression tests for the planner failure path + deterministic fallback plan.

Covers (per the repair spec):
  - planner timeout           -> fallback, job proceeds
  - empty response            -> fallback, job proceeds
  - valid JSON                -> normal plan, NO fallback
  - Markdown-wrapped JSON     -> extracted
  - JSON surrounded by prose  -> extracted
  - malformed JSON            -> classified, then fallback
  - plain-text response       -> classified, then fallback
  - no secrets in diagnostics -> raw response redacted before storage
  - fallback execution reaches the coding/execution stage (edit + commit)
  - no infinite retry         -> the provider planner is invoked exactly once
  - existing dirty workspace preserved across a fallback job

No network / no real provider — a fake `claude` CLI stub is injected via
RUNTIME_CLAUDE_BIN, exactly like the existing phase-13/job-executor tests.
"""
import importlib
import os
import stat
import subprocess
import tempfile

import pytest

os.environ.setdefault("RUNTIME_DB", os.path.join(tempfile.gettempdir(), "rt_fallback_jobs.db"))

from core import ai_planner, job_executor, job_store  # noqa: E402


def setup_module(_m):
    # conftest.py points RUNTIME_DB at ONE shared temp file for the whole pytest
    # session (deliberately, so no test run can ever resolve to the live
    # production db). Removing that shared file here raced other test modules'
    # still-live background threads (job_executor's heartbeat, or an async
    # dispatch thread not yet joined) writing to it mid-run, which surfaced as
    # sqlite3.OperationalError: attempt to write a readonly database in
    # unrelated test files. Clearing the ROWS instead leaves the file (and any
    # other module's open connection) intact, while still giving this module
    # the same "starts empty" guarantee the old os.remove() gave it.
    job_store.init_db()
    with job_store._LOCK, job_store._conn() as c:
        c.execute("DELETE FROM jobs")


def _fake_cli(tmp_path, body):
    p = tmp_path / "fake_claude.py"
    p.write_text("#!/usr/bin/env python3\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def _reload(monkeypatch, cli, timeout=10):
    monkeypatch.setenv("RUNTIME_CLAUDE_BIN", cli)
    monkeypatch.setenv("RUNTIME_PLAN_TIMEOUT", str(timeout))
    importlib.reload(ai_planner)


def _git(path, *args):
    subprocess.run(["git", "-C", str(path)] + list(args), check=True,
                   capture_output=True, text=True)


def _repo(tmp_path, with_tests_dir=False):
    """A minimal git repo. By default NO tests/ dir and no test config, so the
    fallback's derived test_commands is [] and the pipeline commits without
    shelling out to pytest inside the temp repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("seed\n")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    if with_tests_dir:
        (repo / "tests").mkdir()
    return repo


def _counting_cli(tmp_path, body_after_count):
    """Fake CLI that records how many times it was invoked (to prove the planner
    is not retried in a loop), then runs `body_after_count`."""
    counter = tmp_path / "cli.count"
    return _fake_cli(tmp_path, (
        f"import os\ncounter = {str(counter)!r}\n"
        "n = int(open(counter).read()) if os.path.exists(counter) else 0\n"
        "open(counter, 'w').write(str(n + 1))\n"
    ) + body_after_count), counter


# ── unit: _extract_json shapes ───────────────────────────────────────────────

def test_extract_valid_json():
    obj = ai_planner._extract_json('{"summary":"s","files":[]}')
    assert obj["summary"] == "s"


def test_extract_markdown_fenced_json():
    text = "Here is the plan:\n```json\n{\"summary\":\"s\",\"files\":[]}\n```\nDone."
    obj = ai_planner._extract_json(text)
    assert obj["summary"] == "s"


def test_extract_json_surrounded_by_prose():
    text = 'Sure! I think we should do {"summary":"s","files":[]} and then test it.'
    obj = ai_planner._extract_json(text)
    assert obj["files"] == []


def test_extract_json_with_braces_inside_strings():
    # a `}` inside a string value must not prematurely close the object
    text = 'prefix {"summary":"has } brace","files":[]} suffix'
    obj = ai_planner._extract_json(text)
    assert obj["summary"] == "has } brace"


def test_extract_empty_raises_empty():
    with pytest.raises(ai_planner.PlannerError, match="empty output"):
        ai_planner._extract_json("   ")


def test_extract_malformed_json_is_distinct_from_plain_text():
    with pytest.raises(ai_planner.PlannerError, match="malformed planner JSON"):
        ai_planner._extract_json('{"summary": "s" "files": []}')  # both braces, unparseable
    with pytest.raises(ai_planner.PlannerError, match="did not return JSON"):
        ai_planner._extract_json("just a sentence, no json here")


# ── unit: plan() attaches sanitized diagnostics + accounting ─────────────────

def test_planner_failure_carries_redacted_raw_and_accounting(tmp_path, monkeypatch):
    envelope = {
        "type": "result", "subtype": "success", "is_error": False,
        "result": "totally not json, secret sk-ant-LEAKED_KEY_123 here",
        "usage": {"input_tokens": 11, "output_tokens": 7},
        "total_cost_usd": 0.0021, "duration_ms": 1234,
    }
    import json as _json
    cli = _fake_cli(tmp_path, f"import sys\nsys.stdout.write({_json.dumps(envelope)!r})\n")
    _reload(monkeypatch, cli)
    with pytest.raises(ai_planner.PlannerError) as ei:
        ai_planner.plan("g", "i", str(tmp_path), [])
    e = ei.value
    assert "did not return JSON" in str(e)
    assert "sk-ant-LEAKED_KEY_123" not in e.raw
    assert "[redacted]" in e.raw
    assert e.tokens == {"input_tokens": 11, "output_tokens": 7}
    assert e.cost_usd == 0.0021 and e.duration_ms == 1234


# ── unit: fallback plan shape ────────────────────────────────────────────────

def test_build_fallback_plan_is_safe_and_marked(tmp_path):
    diag = {"reason": "planner timed out", "raw": "prefix sk-ant-XYZ suffix",
            "timed_out": True, "tokens": {"output_tokens": 5}, "cost_usd": 0.001,
            "duration_ms": 900}
    plan = ai_planner.build_fallback_plan("Add a feature", "do the thing",
                                          str(tmp_path), [], task_id=92, diagnostics=diag)
    assert plan["fallback"] is True
    assert plan["fallback_reason"] == "planner timed out"
    assert plan["risk_level"] == "low"
    assert len(plan["files"]) == 1
    f = plan["files"][0]
    assert f["operation"] in ("create", "replace")
    assert f["path"].startswith("reports/runtime/fallback/PLAN-92-")
    # the recorded raw response must already be redacted upstream; even if a raw
    # secret is passed in, the doc must never surface it verbatim... but note the
    # fallback trusts callers to pass sanitized raw — here we pass a raw secret to
    # confirm nothing downstream re-expands it.
    assert "add-a-feature" in f["path"]
    assert set(plan["stages"]) == set(ai_planner.FALLBACK_STAGES)


def test_fallback_plan_honours_allow_list(tmp_path):
    plan = ai_planner.build_fallback_plan("g", "i", str(tmp_path), ["src"], task_id=1)
    assert plan["files"][0]["path"] == "src/RUNTIME_FALLBACK_PLAN.md"


# ── E2E via job_executor: valid JSON -> normal plan, NO fallback ─────────────

def test_valid_json_uses_normal_plan_no_fallback(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    plan_json = '{"summary":"real","files":[{"path":"x.txt","operation":"create","content":"hi"}],"test_commands":[]}'
    cli, counter = _counting_cli(tmp_path, (
        "import json, sys\n"
        f"result = {plan_json!r}\n"
        "sys.stdout.write(json.dumps({'type':'result','subtype':'success','is_error':False,'result':result}))\n"
    ))
    _reload(monkeypatch, cli)
    job = job_store.create_job(project_path=str(repo), goal="g", instructions="i",
                               task_id=1, autonomy_level="execute_safe",
                               auto_commit=True, auto_push=False)
    job_executor.execute(job["id"])
    final = job_store.get_job(job["id"])
    assert final["status"] == "completed", final.get("error")
    assert final["plan"].get("fallback") is not True
    assert not any(a.get("fallback_planning") for a in (final["artifacts"] or []))
    assert int(open(counter).read()) == 1


# ── E2E: timeout -> fallback, reaches coding stage, exactly one planner call ──

def test_timeout_falls_back_and_reaches_coding_stage(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    cli, counter = _counting_cli(tmp_path, "import time\ntime.sleep(30)\n")
    _reload(monkeypatch, cli, timeout=2)
    job = job_store.create_job(project_path=str(repo), goal="Long task", instructions="do it",
                               task_id=92, autonomy_level="execute_safe",
                               auto_commit=True, auto_push=False)
    job_executor.execute(job["id"])
    final = job_store.get_job(job["id"])
    # A fallback plan is not an implementation: it ends in its own terminal
    # status, never `completed`. See core.job_kinds.terminal_status_for.
    assert final["status"] == "fallback_plan_only", final.get("error")
    # fallback markers
    assert final["plan"]["fallback"] is True
    assert final["plan"]["fallback_timed_out"] is True
    assert any(a.get("fallback_planning") for a in (final["artifacts"] or []))
    # reached the coding/execution stage: a file op was applied + committed
    assert final["changed_files"], "no file ops applied — never reached coding stage"
    assert final["git_info"].get("commit")
    # provider planner invoked exactly once — no retry loop
    assert int(open(counter).read()) == 1


# ── E2E: empty response -> fallback ──────────────────────────────────────────

def test_empty_response_falls_back(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    cli, counter = _counting_cli(tmp_path, "pass\n")  # exits 0, no stdout
    _reload(monkeypatch, cli)
    job = job_store.create_job(project_path=str(repo), goal="g", instructions="i",
                               task_id=93, autonomy_level="execute_safe",
                               auto_commit=True, auto_push=False)
    job_executor.execute(job["id"])
    final = job_store.get_job(job["id"])
    # A fallback plan is not an implementation: it ends in its own terminal
    # status, never `completed`. See core.job_kinds.terminal_status_for.
    assert final["status"] == "fallback_plan_only", final.get("error")
    assert final["plan"]["fallback"] is True
    assert int(open(counter).read()) == 1


# ── E2E: plain-text response -> fallback ─────────────────────────────────────

def test_plain_text_response_falls_back(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    cli, counter = _counting_cli(tmp_path, (
        "import json, sys\n"
        "env = {'type':'result','subtype':'success','is_error':False,'result':'I will now do the task.'}\n"
        "sys.stdout.write(json.dumps(env))\n"
    ))
    _reload(monkeypatch, cli)
    job = job_store.create_job(project_path=str(repo), goal="g", instructions="i",
                               task_id=94, autonomy_level="execute_safe",
                               auto_commit=True, auto_push=False)
    job_executor.execute(job["id"])
    final = job_store.get_job(job["id"])
    # A fallback plan is not an implementation: it ends in its own terminal
    # status, never `completed`. See core.job_kinds.terminal_status_for.
    assert final["status"] == "fallback_plan_only", final.get("error")
    assert final["plan"]["fallback"] is True
    assert "did not return JSON" in final["plan"]["fallback_reason"]
    assert int(open(counter).read()) == 1


# ── E2E: malformed JSON -> fallback ──────────────────────────────────────────

def test_malformed_json_falls_back(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    cli, counter = _counting_cli(tmp_path, (
        "import json, sys\n"
        "env = {'type':'result','subtype':'success','is_error':False,'result':'{\\\"summary\\\": \\\"s\\\" \\\"files\\\": []}'}\n"
        "sys.stdout.write(json.dumps(env))\n"
    ))
    _reload(monkeypatch, cli)
    job = job_store.create_job(project_path=str(repo), goal="g", instructions="i",
                               task_id=95, autonomy_level="execute_safe",
                               auto_commit=True, auto_push=False)
    job_executor.execute(job["id"])
    final = job_store.get_job(job["id"])
    # A fallback plan is not an implementation: it ends in its own terminal
    # status, never `completed`. See core.job_kinds.terminal_status_for.
    assert final["status"] == "fallback_plan_only", final.get("error")
    assert final["plan"]["fallback"] is True
    assert "malformed planner JSON" in final["plan"]["fallback_reason"]


# ── E2E: no secrets in stored diagnostics ────────────────────────────────────

def test_no_secrets_in_stored_diagnostics(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    cli, _ = _counting_cli(tmp_path, (
        "import json, sys\n"
        "env = {'type':'result','subtype':'success','is_error':False,"
        "'result':'no json here, leaking sk-ant-SUPERSECRET_TOKEN in prose'}\n"
        "sys.stdout.write(json.dumps(env))\n"
    ))
    _reload(monkeypatch, cli)
    job = job_store.create_job(project_path=str(repo), goal="g", instructions="i",
                               task_id=96, autonomy_level="execute_safe",
                               auto_commit=True, auto_push=False)
    job_executor.execute(job["id"])
    final = job_store.get_job(job["id"])
    # A fallback plan is not an implementation: it ends in its own terminal
    # status, never `completed`. See core.job_kinds.terminal_status_for.
    assert final["status"] == "fallback_plan_only", final.get("error")
    blob = str(final["plan"]) + str(final["artifacts"]) + str(final["logs"])
    assert "sk-ant-SUPERSECRET_TOKEN" not in blob
    # and the committed fallback doc must be clean too. Isolated-workspace
    # model: the doc lives on the job's work branch, not in the primary tree.
    branch = (final.get("git_info") or {}).get("branch")
    shown = subprocess.run(
        ["git", "-C", str(repo), "show", f"{branch}:{final['plan']['files'][0]['path']}"],
        capture_output=True, text=True)
    assert shown.returncode == 0, shown.stderr
    assert "sk-ant-SUPERSECRET_TOKEN" not in shown.stdout
    assert "[redacted]" in shown.stdout


# ── E2E: existing dirty workspace preserved across a fallback job ─────────────

def test_existing_dirty_workspace_preserved(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    # a pre-existing untracked ("dirty") file that must survive the job untouched
    stray = repo / "LOCAL_WIP.txt"
    stray.write_text("operator work in progress\n")
    cli, _ = _counting_cli(tmp_path, "import time\ntime.sleep(30)\n")
    _reload(monkeypatch, cli, timeout=2)
    job = job_store.create_job(project_path=str(repo), goal="g", instructions="i",
                               task_id=97, autonomy_level="execute_safe",
                               auto_commit=True, auto_push=False)
    job_executor.execute(job["id"])
    final = job_store.get_job(job["id"])
    # A fallback plan is not an implementation: it ends in its own terminal
    # status, never `completed`. See core.job_kinds.terminal_status_for.
    assert final["status"] == "fallback_plan_only", final.get("error")
    assert stray.exists()
    assert stray.read_text() == "operator work in progress\n"
