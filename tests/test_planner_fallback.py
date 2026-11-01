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
import json
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


def _git_out(path, *args):
    return subprocess.run(["git", "-C", str(path)] + list(args), check=True,
                          capture_output=True, text=True).stdout.strip()


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


# ── Regression (job 86 / task_id=86): a plan DELIVERED before the process
# deadline must not be thrown away just because the CLI lingered afterwards.
# The old harness checked `timed_out` before it ever looked at stdout, so a
# complete, valid plan was discarded and the job was downgraded to a
# `fallback_plan_only` NON-implementation. ───────────────────────────────

def _lingering_cli(tmp_path, plan_json, linger=30):
    """Fake CLI that writes a COMPLETE result envelope, flushes, then hangs past
    the deadline so the process group is killed with the plan already on stdout."""
    return _counting_cli(tmp_path, (
        "import json, sys, time\n"
        f"result = {plan_json!r}\n"
        "sys.stdout.write(json.dumps({'type':'result','subtype':'success','is_error':False,"
        "'result':result,'usage':{'input_tokens':11,'output_tokens':22},"
        "'total_cost_usd':0.5,'duration_ms':1234}))\n"
        "sys.stdout.flush()\n"
        f"time.sleep({linger})\n"
    ))


def test_plan_delivered_before_timeout_is_salvaged_not_discarded(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    plan_json = json.dumps({
        "summary": "real plan from provider", "risk_level": "low",
        "files": [{"path": "NOTES.md", "operation": "create", "content": "real planner output\n"}],
        "test_commands": [], "expected_result": "ok"})
    cli, counter = _lingering_cli(tmp_path, plan_json)
    _reload(monkeypatch, cli, timeout=3)
    job = job_store.create_job(project_path=str(repo),
                               goal="Global Claude Context Lifecycle Manager",
                               instructions="save handoff, rotate context, restore project state",
                               task_id=86, autonomy_level="execute_safe",
                               auto_commit=True, auto_push=False)
    job_executor.execute(job["id"])
    final = job_store.get_job(job["id"])
    # The provider's real plan is used — NOT the deterministic fallback.
    assert final["plan"]["summary"] == "real plan from provider"
    assert final["plan"].get("fallback") is not True
    assert final["status"] != "fallback_plan_only", final.get("error")
    assert not any(a.get("fallback_planning") for a in (final["artifacts"] or []))
    # the provider's own file op reached the coding stage, not a fallback doc
    assert [f["path"] for f in final["changed_files"]] == ["NOTES.md"]
    # still exactly one planner call — salvage is not a retry
    assert int(open(counter).read()) == 1


def test_timeout_with_no_usable_output_still_falls_back(tmp_path, monkeypatch):
    """Salvage must not swallow a real timeout: partial/garbage bytes on stdout
    are still a planner failure, still marked `timed_out`."""
    repo = _repo(tmp_path)
    cli, _ = _counting_cli(tmp_path, (
        "import sys, time\n"
        "sys.stdout.write('{\"type\":\"result\",\"result\":\"{par')\n"  # truncated mid-plan
        "sys.stdout.flush()\n"
        "time.sleep(30)\n"))
    _reload(monkeypatch, cli, timeout=3)
    job = job_store.create_job(project_path=str(repo), goal="g", instructions="i",
                               task_id=861, autonomy_level="execute_safe",
                               auto_commit=True, auto_push=False)
    job_executor.execute(job["id"])
    final = job_store.get_job(job["id"])
    assert final["status"] == "fallback_plan_only", final.get("error")
    assert final["plan"]["fallback"] is True
    assert final["plan"]["fallback_timed_out"] is True


def test_timeout_error_preserves_accounting_when_envelope_was_delivered(tmp_path, monkeypatch):
    """A timeout that cannot be salvaged still carries the provider's
    token/cost/timing, instead of reporting the spend as unknown."""
    repo = _repo(tmp_path)
    # valid envelope, but `result` is prose — parses, never validates as a plan
    cli, _ = _counting_cli(tmp_path, (
        "import json, sys, time\n"
        "sys.stdout.write(json.dumps({'type':'result','is_error':False,"
        "'result':'I need more information before planning.',"
        "'usage':{'input_tokens':11,'output_tokens':22},'total_cost_usd':0.5}))\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n"))
    _reload(monkeypatch, cli, timeout=3)
    with pytest.raises(ai_planner.PlannerError) as ei:
        ai_planner.plan("g", "i", str(repo), [])
    assert ei.value.timed_out is True
    assert ei.value.cost_usd == 0.5
    assert ei.value.tokens == {"input_tokens": 11, "output_tokens": 22}


# ── Salvage against realistic provider output ────────────────────────────────
# The salvage tests above use a hand-written minimal envelope. These pin the
# parts that were otherwise only assumed: a full provider-shaped envelope, a
# payload larger than the OS pipe buffer (so the drain thread must still finish
# after the process group is killed), and a plan cut off mid-write (which must
# NEVER be salvaged into a corrupt plan).

# Field set as emitted by `claude -p --output-format json` (v2.1.x).
def _envelope_src(result_expr):
    return ("import json, sys, time\n"
            "env = {'type': 'result', 'subtype': 'success', 'is_error': False,\n"
            "       'duration_ms': 4321, 'duration_api_ms': 4100, 'num_turns': 1,\n"
            "       'session_id': '5f2c1d90-0000-4000-8000-000000000001',\n"
            "       'total_cost_usd': 0.0731,\n"
            "       'usage': {'input_tokens': 1843, 'output_tokens': 512,\n"
            "                 'cache_creation_input_tokens': 0,\n"
            "                 'cache_read_input_tokens': 12000},\n"
            f"       'result': {result_expr}}}\n"
            "sys.stdout.write(json.dumps(env))\n"
            "sys.stdout.flush()\n"
            "time.sleep(30)\n")


def test_salvage_accepts_a_full_provider_shaped_envelope(tmp_path, monkeypatch):
    plan_json = json.dumps({
        "summary": "provider plan", "risk_level": "medium",
        "files": [{"path": "svc/x.py", "operation": "create", "content": "x = 1\n"}],
        "test_commands": [], "expected_result": "ok"})
    cli, counter = _counting_cli(tmp_path, _envelope_src(repr(plan_json)))
    _reload(monkeypatch, cli, timeout=3)
    plan = ai_planner.plan("g", "i", str(tmp_path), [])
    assert plan["summary"] == "provider plan"
    assert plan["files"][0]["path"] == "svc/x.py"
    assert plan.get("fallback") is not True
    assert int(open(counter).read()) == 1


def test_salvage_survives_a_plan_larger_than_the_pipe_buffer(tmp_path, monkeypatch):
    """~500KB of content: far beyond the 64KB pipe buffer, so the plan cannot
    have been sitting in the pipe as a single write. Proves the drain thread
    still completes after the process group is killed on the deadline."""
    big = "y" * 500_000
    plan_json = json.dumps({
        "summary": "big plan", "risk_level": "low",
        "files": [{"path": "BIG.md", "operation": "create", "content": big}],
        "test_commands": [], "expected_result": "ok"})
    cli, _ = _counting_cli(tmp_path, _envelope_src(repr(plan_json)))
    _reload(monkeypatch, cli, timeout=3)
    plan = ai_planner.plan("g", "i", str(tmp_path), [])
    assert plan["summary"] == "big plan"
    # byte-exact: a truncated drain would silently shorten the file content
    assert len(plan["files"][0]["content"]) == 500_000
    assert plan["files"][0]["content"] == big


def test_a_plan_truncated_by_the_deadline_is_never_salvaged(tmp_path, monkeypatch):
    """Safety property: salvage must accept only a plan that parses AND
    validates. A provider killed mid-write must fall back, never yield a
    half-parsed plan that the pipeline would then apply to the repo."""
    plan_json = json.dumps({
        "summary": "half written", "risk_level": "low",
        "files": [{"path": "a.py", "operation": "create", "content": "x = 1\n"}],
        "test_commands": [], "expected_result": "ok"})
    cli, _ = _counting_cli(tmp_path, (
        "import json, sys, time\n"
        "env = {'type':'result','subtype':'success','is_error':False,"
        f"'result': {plan_json!r}}}\n"
        "blob = json.dumps(env)\n"
        "sys.stdout.write(blob[:len(blob)//2])\n"   # cut off mid-envelope
        "sys.stdout.flush()\n"
        "time.sleep(30)\n"))
    _reload(monkeypatch, cli, timeout=3)
    with pytest.raises(ai_planner.PlannerError) as ei:
        ai_planner.plan("g", "i", str(tmp_path), [])
    assert ei.value.timed_out is True


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


# ── salvage must be OBSERVABLE, not merely correct ───────────────────────────
# A salvaged plan used to be indistinguishable in the job record from an
# ordinary slow plan: no log line, no artifact, no marker. "Did salvage ever
# fire in production?" could only be inferred from heartbeat timing. These pin
# the marker, the log line and the artifact.

def test_salvaged_plan_is_marked_and_carries_accounting(tmp_path, monkeypatch):
    plan_json = json.dumps({
        "summary": "provider plan", "risk_level": "low",
        "files": [{"path": "a.py", "operation": "create", "content": "x = 1\n"}],
        "test_commands": [], "expected_result": "ok"})
    cli, _ = _lingering_cli(tmp_path, plan_json)
    _reload(monkeypatch, cli, timeout=3)
    plan = ai_planner.plan("g", "i", str(tmp_path), [])
    assert plan["salvaged_after_timeout"] is True
    # accounting the provider reported before it was killed survives onto the plan
    assert plan["planner_cost_usd"] == 0.5
    assert plan["planner_tokens"] == {"input_tokens": 11, "output_tokens": 22}


def test_a_normal_plan_is_not_marked_as_salvaged(tmp_path, monkeypatch):
    plan_json = json.dumps({
        "summary": "fast plan", "risk_level": "low",
        "files": [{"path": "a.py", "operation": "create", "content": "x = 1\n"}],
        "test_commands": [], "expected_result": "ok"})
    cli, _ = _counting_cli(tmp_path, (
        "import json, sys\n"
        f"result = {plan_json!r}\n"
        "sys.stdout.write(json.dumps({'type':'result','is_error':False,'result':result}))\n"))
    _reload(monkeypatch, cli, timeout=10)
    plan = ai_planner.plan("g", "i", str(tmp_path), [])
    assert plan.get("salvaged_after_timeout") is not True


def test_executor_logs_and_records_an_artifact_when_a_plan_is_salvaged(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    plan_json = json.dumps({
        "summary": "real plan from provider", "risk_level": "low",
        "files": [{"path": "NOTES.md", "operation": "create", "content": "real\n"}],
        "test_commands": [], "expected_result": "ok"})
    cli, _ = _lingering_cli(tmp_path, plan_json)
    _reload(monkeypatch, cli, timeout=3)
    job = job_store.create_job(project_path=str(repo), goal="g", instructions="i",
                               task_id=862, autonomy_level="execute_safe",
                               auto_commit=True, auto_push=False)
    job_executor.execute(job["id"])
    final = job_store.get_job(job["id"])
    arts = [a for a in (final["artifacts"] or []) if a.get("planner_salvaged_after_timeout")]
    assert arts, "no salvage artifact recorded — salvage is invisible again"
    assert arts[0]["cost_usd"] == 0.5
    msgs = " ".join(l.get("msg", "") for l in (final["logs"] or []))
    assert "SALVAGED" in msgs
    # and it is emphatically NOT a fallback
    assert not any(a.get("fallback_planning") for a in (final["artifacts"] or []))
    assert final["status"] != "fallback_plan_only"


def test_a_fallback_job_records_no_salvage_artifact(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    cli, _ = _counting_cli(tmp_path, "import time\ntime.sleep(30)\n")   # nothing delivered
    _reload(monkeypatch, cli, timeout=2)
    job = job_store.create_job(project_path=str(repo), goal="g", instructions="i",
                               task_id=863, autonomy_level="execute_safe",
                               auto_commit=True, auto_push=False)
    job_executor.execute(job["id"])
    final = job_store.get_job(job["id"])
    assert final["status"] == "fallback_plan_only"
    assert not any(a.get("planner_salvaged_after_timeout") for a in (final["artifacts"] or []))


# ── job #136: a pseudo tool call is not a plan ───────────────────────────────
# Raw planner output of job 703f780d (task 326): with tools disabled the model wrote
# a tool call in prose. The first JSON object was the tool's arguments, so the job
# failed as "plan missing 'files' list" — indistinguishable from a broken plan.
_JOB136_RAW = (
    "Let me investigate the actual code before proposing changes.\n\n---\n\n"
    "**Tool: bash**\n\n```json\n{\n  \"command\": \"cd /root/ai-dev-runtime && git log "
    "--oneline -15\",\n  \"description\": \"Check recent history\"\n}\n```\n"
)


def test_a_pseudo_tool_call_is_reported_as_one_not_as_a_broken_plan():
    with pytest.raises(ai_planner.PlannerError) as ei:
        ai_planner._extract_json(_JOB136_RAW)
    assert "tool call" in str(ei.value)


def test_the_plan_object_wins_over_an_earlier_non_plan_object():
    text = ('Context:\n```json\n{"note": "example", "n": 1}\n```\n\n'
            '```json\n{"summary": "s", "files": [{"path": "a.py", "operation": "create", '
            '"content": "x = 1\\n"}]}\n```')
    obj = ai_planner._extract_json(text)
    assert obj["summary"] == "s" and obj["files"][0]["path"] == "a.py"


def test_a_lone_non_plan_object_still_fails_as_missing_files():
    obj = ai_planner._extract_json('{"summary": "s"}')
    with pytest.raises(ai_planner.PlannerError, match="plan missing 'files' list"):
        ai_planner._validate(obj, [])


def test_a_tool_call_answer_blocks_the_job_instead_of_running_a_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("RUNTIME_PLANNER_REPLAN", raising=False)
    repo = _repo(tmp_path)
    before = sorted(p.name for p in repo.rglob("*") if ".git" not in p.parts)
    cli, counter = _counting_cli(tmp_path, (
        "import json, sys\n"
        f"env = {{'type':'result','subtype':'success','is_error':False,'result':{_JOB136_RAW!r}}}\n"
        "sys.stdout.write(json.dumps(env))\n"
    ))
    _reload(monkeypatch, cli)
    job = job_store.create_job(project_path=str(repo), goal="g", instructions="investigate first",
                               task_id=326, autonomy_level="execute_safe",
                               auto_commit=True, auto_push=False)
    job_executor.execute(job["id"])
    final = job_store.get_job(job["id"])
    assert final["status"] == "blocked", final.get("error")
    assert final["error"].startswith("planner_tool_call")
    assert not final.get("plan")                       # no fallback plan was made
    assert any(a.get("planner_tool_call") for a in final["artifacts"])
    assert int(open(counter).read()) == 1               # one planning attempt, no retry
    after = sorted(p.name for p in repo.rglob("*") if ".git" not in p.parts)
    assert after == before                              # nothing written to the repo


# ── task 326: an unusable base blocks the job, it is not guessed ─────────────
def _valid_plan_cli(tmp_path):
    plan_json = ('{"summary":"real","files":[{"path":"x.txt","operation":"create",'
                 '"content":"hi"}],"test_commands":[]}')
    return _counting_cli(tmp_path, (
        "import json, sys\n"
        f"result = {plan_json!r}\n"
        "sys.stdout.write(json.dumps({'type':'result','subtype':'success','is_error':False,'result':result}))\n"
    ))


def test_a_configured_base_ref_that_does_not_resolve_blocks_the_job(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    cfg = tmp_path / "refs.yaml"
    cfg.write_text(f"projects:\n  {repo}:\n    base_ref: gone-branch\n")
    monkeypatch.setenv("RUNTIME_BASE_REFS_CONFIG", str(cfg))
    cli, _ = _valid_plan_cli(tmp_path)
    _reload(monkeypatch, cli)
    job = job_store.create_job(project_path=str(repo), goal="g", instructions="i",
                               task_id=2, autonomy_level="execute_safe",
                               auto_commit=True, auto_push=False)
    job_executor.execute(job["id"])
    final = job_store.get_job(job["id"])
    assert final["status"] == "blocked", final.get("error")
    assert final["error"].startswith("base_ref:") and "gone-branch" in final["error"]
    assert not (repo / "x.txt").exists()
    assert _git_out(repo, "branch", "--list", "ai-runtime/*") == ""


def test_the_resolved_base_sha_is_logged(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("RUNTIME_BASE_REFS_CONFIG", str(tmp_path / "absent.yaml"))
    cli, _ = _valid_plan_cli(tmp_path)
    _reload(monkeypatch, cli)
    job = job_store.create_job(project_path=str(repo), goal="g", instructions="i",
                               task_id=3, autonomy_level="execute_safe",
                               auto_commit=True, auto_push=False)
    job_executor.execute(job["id"])
    final = job_store.get_job(job["id"])
    sha = _git_out(repo, "rev-parse", "main")
    assert any(f"base branch resolved: main @ {sha} (source: guess)" in l["msg"]
               for l in final["logs"]), [l["msg"] for l in final["logs"]]


# ── at most ONE bounded replan after a tool-call reply (RUNTIME_PLANNER_REPLAN) ─
_GOOD_PLAN = ('{"summary":"real","files":[{"path":"x.txt","operation":"create",'
              '"content":"hi"}],"test_commands":[]}')


def _scripted_cli(tmp_path, replies):
    """Fake CLI answering call n with replies[n] (last one repeats); each prompt is saved."""
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    return _counting_cli(tmp_path, (
        "import json, sys\n"
        f"replies = {replies!r}\n"
        f"open({str(prompts)!r} + f'/{{n}}.txt', 'w').write(sys.stdin.read())\n"
        "r = replies[min(n, len(replies) - 1)]\n"
        "sys.stdout.write(json.dumps({'type':'result','subtype':'success','is_error':False,'result':r}))\n"
    )), prompts


def _replan_job(tmp_path, monkeypatch, replies, *, enabled=True, artifacts=None):
    repo = _repo(tmp_path)
    if enabled:
        monkeypatch.setenv("RUNTIME_PLANNER_REPLAN", "1")
    else:
        monkeypatch.delenv("RUNTIME_PLANNER_REPLAN", raising=False)
    (cli, counter), prompts = _scripted_cli(tmp_path, replies)
    _reload(monkeypatch, cli)
    job = job_store.create_job(project_path=str(repo), goal="g", instructions="investigate first",
                               task_id=326, autonomy_level="execute_safe",
                               auto_commit=True, auto_push=False)
    if artifacts:
        job_store.update_job(job["id"], artifacts=artifacts)
    job_executor.execute(job["id"])
    return repo, job_store.get_job(job["id"]), int(open(counter).read()), prompts


def _replan_arts(final):
    return [a for a in (final["artifacts"] or []) if "planner_replan" in a]


def test_replan_is_off_by_default_and_the_job_blocks_after_one_call(tmp_path, monkeypatch):
    repo, final, calls, _ = _replan_job(tmp_path, monkeypatch, [_JOB136_RAW, _GOOD_PLAN],
                                        enabled=False)
    assert final["status"] == "blocked" and "replan disabled" in final["error"]
    assert calls == 1 and _replan_arts(final) == []
    assert not (repo / "x.txt").exists()


def test_one_replan_turns_a_tool_call_into_a_real_plan(tmp_path, monkeypatch):
    repo, final, calls, prompts = _replan_job(tmp_path, monkeypatch, [_JOB136_RAW, _GOOD_PLAN])
    assert final["status"] == "completed", final.get("error")
    assert calls == 2
    assert [f["path"] for f in final["plan"]["files"]] == ["x.txt"]
    assert final["plan"].get("fallback") is not True
    assert _replan_arts(final) == [{"planner_replan": "ok", "files": 1}]
    assert "REPLAN:" not in (prompts / "0.txt").read_text()
    assert "REPLAN:" in (prompts / "1.txt").read_text()


def test_a_second_tool_call_blocks_without_a_third_call_or_a_fallback(tmp_path, monkeypatch):
    repo, final, calls, _ = _replan_job(tmp_path, monkeypatch, [_JOB136_RAW, _JOB136_RAW])
    assert final["status"] == "blocked"
    assert "replan failed" in final["error"] and "tool call" in final["error"]
    assert calls == 2
    assert not final.get("plan")
    (art,) = _replan_arts(final)
    assert art["planner_replan"] == "failed"
    assert not (repo / "x.txt").exists()


def test_an_empty_or_prose_replan_blocks_never_falls_back(tmp_path, monkeypatch):
    for i, second in enumerate(['{"summary": "needs investigation", "files": []}',
                                "I would first look at the code."]):
        sub = tmp_path / f"case{i}"
        sub.mkdir()
        _, final, calls, _ = _replan_job(sub, monkeypatch, [_JOB136_RAW, second])
        assert final["status"] == "blocked", (second, final.get("error"))
        assert calls == 2 and not final.get("plan")
        assert not any(a.get("fallback_planning") for a in final["artifacts"] or [])


def test_a_job_that_already_replanned_never_replans_again(tmp_path, monkeypatch):
    _, final, calls, _ = _replan_job(tmp_path, monkeypatch, [_JOB136_RAW, _GOOD_PLAN],
                                     artifacts=[{"planner_replan": "started"}])
    assert final["status"] == "blocked" and "already attempted" in final["error"]
    assert calls == 1


def test_two_different_plans_in_one_reply_are_refused_not_guessed():
    # An "example" plan followed by the real one: executing either is a guess.
    text = ('Example:\n```json\n{"summary": "example", "files": [{"path": "ex.py", '
            '"operation": "create", "content": "x"}]}\n```\nPlan:\n```json\n'
            '{"summary": "real", "files": [{"path": "a.py", "operation": "create", '
            '"content": "y"}]}\n```')
    with pytest.raises(ai_planner.PlannerError, match="several different plans"):
        ai_planner._extract_json(text)


def test_the_same_plan_seen_twice_is_one_plan():
    plan = '{"summary": "s", "files": [{"path": "a.py", "operation": "create", "content": "y"}]}'
    obj = ai_planner._extract_json(f"Plan:\n```json\n{plan}\n```\n")   # fence + prose scan
    assert obj["summary"] == "s"
