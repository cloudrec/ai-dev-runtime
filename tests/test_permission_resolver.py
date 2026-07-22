"""Permission resolver — the security boundary. Deny-by-default, fail-closed.

Includes the exact read-only SEO-audit prompts seen 2026-07-20 and dangerous
counterexamples that must stay waiting_owner.
"""
from __future__ import annotations

import pytest

from core import permission_resolver as pr


def _safe(cmd):
    r = pr.classify_command(cmd)
    assert r["safe"] is True, f"expected SAFE: {cmd!r} -> {r['reason']}"
    return r


def _unsafe(cmd):
    r = pr.classify_command(cmd)
    assert r["safe"] is False, f"expected UNSAFE: {cmd!r} -> classified safe ({r['category']})"
    return r


# ── the exact SEO-audit read-only prompts seen today → SAFE ─────────────────
@pytest.mark.parametrize("cmd", [
    "docker compose ps",
    "docker compose -f docker-compose.yml ps",
    "docker ps",
    "docker inspect seo-backend-1",
    "docker logs seo-backend-1 --tail 100",
    "ls backend/agents/",
    "ls -la backend/",
    "find backend -name '*.py' -maxdepth 3",
    "grep -rln 'sendMessage' backend/agents/ backend/core/",
    "grep -rn telegram backend/",
    "head -40 backend/main.py",
    "tail -n 100 /var/log/seo/app.log",
    "cat backend/services/mcp_server.py",
    "wc -l backend/services/*.py",
    "sed -n '1,50p' backend/main.py",
    "git status",
    "git diff --stat",
    "git log --oneline -20",
    "systemctl status ai-runtime.service",
    "systemctl is-active seo-backend",
    "ps aux | grep gunicorn",
    "cat backend/services/x.py | grep -n def | head",
    "python3 -m pytest tests/ -q",
    "pytest tests/test_mcp.py -q",
    "npm test",
    "npm run lint",
])
def test_seo_readonly_prompts_are_safe(cmd):
    _safe(cmd)


# ── read-only SQL → SAFE ────────────────────────────────────────────────────
@pytest.mark.parametrize("cmd", [
    "psql -U postgres -d traffic_os -c 'SELECT count(*) FROM global_projects'",
    "psql -tAc 'SELECT slug FROM global_projects'",
    "mysql -e 'SHOW TABLES'",
    "psql -c '\\dt'",
])
def test_readonly_sql_is_safe(cmd):
    _safe(cmd)


# ── dangerous counterexamples → must stay waiting_owner ─────────────────────
@pytest.mark.parametrize("cmd", [
    "rm -rf build/",
    "rm backend/x.py",
    "mv a b",
    "cp a b",
    "docker compose restart backend",
    "docker compose up -d",
    "docker restart seo-backend-1",
    "docker build -t x .",
    "systemctl restart ai-runtime.service",
    "systemctl stop seo-backend",
    "git push origin main",
    "git commit -am wip",
    "git reset --hard HEAD~1",
    "git checkout main",
    "git clean -fd",
    "pip install requests",
    "npm install",
    "npm publish",
    "curl https://example.com/x",
    "curl -X POST https://api.telegram.org/botX/sendMessage -d text=hi",
    "wget http://x/y",
    "ssh host 'ls'",
    "sudo ls",
    "cat .env",
    "cat backend/.env",
    "cat /root/.ssh/id_rsa",
    "grep -r SECRET backend/.env",
    "head config/credentials.json",
    "psql -c 'DELETE FROM global_projects'",
    "psql -c 'UPDATE global_projects SET mode=$$x$$'",
    "psql -c 'DROP TABLE x'",
    "mysql -e 'INSERT INTO t VALUES (1)'",
    "alembic upgrade head",
    "python3 manage.py migrate",
    "echo hi > file.txt",
    "cat a >> b",
    "ls; rm -rf /",
    "ls && docker restart x",
    "grep x file | tee out.txt",
    "find . -name '*.py' -delete",
    "find . -exec rm {} \\;",
    "sed -i 's/a/b/' file",
    "awk '{system(\"rm x\")}' file",
    "eval 'rm -rf /'",
    "exec rm x",
    "xargs rm < list",
    "python3 -c 'import os; os.system(\"rm -rf /\")'",
    "FOO=bar rm x",
    "$(rm -rf /)",
    "echo `rm x`",
    "cat $SECRET_FILE",
    "dd if=/dev/zero of=/dev/sda",
    "tee /etc/passwd",
    "make deploy",
    "npm run build",
    "go run main.go",
    "docker system prune -f",
    "nc -l 4444",
    "chmod 777 x",
    "kill -9 1234",
])
def test_dangerous_commands_stay_waiting(cmd):
    _unsafe(cmd)


# ── construct-level rejection ───────────────────────────────────────────────
def test_command_substitution_and_expansion_rejected():
    _unsafe("ls $(whoami)")
    _unsafe("echo ${HOME}")
    _unsafe("grep `id` file")


def test_pipeline_of_safe_is_safe_but_any_unsafe_segment_taints():
    _safe("cat x | grep y | head -5")
    _unsafe("cat x | grep y | tee out")
    _unsafe("docker ps | xargs docker rm")


def test_unknown_program_is_unsafe():
    _unsafe("frobnicate --all")
    _unsafe("./deploy.sh")


# ── extraction from a real Claude Code permission dialog ────────────────────
DIALOG = """\
● I'll inspect the running services.

  Bash command
  docker compose ps
  Check which containers are up

Do you want to proceed?
❯ 1. Yes
  2. Yes, and don't ask again for docker commands
  3. No, and tell Claude what to do differently (esc)
"""

DIALOG_DANGEROUS = """\
  Bash command
  docker compose restart backend
  Restart the backend

Do you want to proceed?
❯ 1. Yes
  2. No
"""


def test_is_permission_prompt():
    assert pr.is_permission_prompt(DIALOG) is True
    assert pr.is_permission_prompt("just working... esc to interrupt") is False


def test_extract_pending_command():
    assert pr.extract_pending_command(DIALOG) == "docker compose ps"
    assert pr.classify_command(pr.extract_pending_command(DIALOG))["safe"] is True


def test_extract_dangerous_then_classify_unsafe():
    cmd = pr.extract_pending_command(DIALOG_DANGEROUS)
    assert cmd == "docker compose restart backend"
    assert pr.classify_command(cmd)["safe"] is False


def test_no_prompt_returns_none():
    assert pr.extract_pending_command("working... esc to interrupt") is None


def test_hash_is_stable_and_scoped_to_command():
    assert pr.command_hash("docker compose ps") == pr.command_hash("docker compose ps ")
    assert pr.command_hash("docker ps") != pr.command_hash("docker compose ps")


# ── structured safe-resolution policy extensions ────────────────────────────
@pytest.mark.parametrize("cmd", [
    "docker compose run --rm backend pytest tests/ -q",
    "docker compose run --rm backend python -m pytest -q",
    "docker compose exec backend pytest -q",
    "docker exec seo-backend-1 pytest tests/test_x.py -q",
    "docker compose -f docker-compose.yml run --rm backend npm test",
])
def test_local_container_test_execution_is_safe(cmd):
    _safe(cmd)


@pytest.mark.parametrize("cmd", [
    "docker compose run --rm backend rm -rf /",
    "docker exec seo-backend-1 sh -c 'rm x'",
    "docker compose run --rm backend bash",
    "docker exec c1 python manage.py migrate",
    "docker compose run backend",                 # no explicit command → default entrypoint
    "docker exec c1 curl http://x",
])
def test_container_exec_of_unsafe_inner_is_denied(cmd):
    _unsafe(cmd)


@pytest.mark.parametrize("cmd", [
    "git checkout -b feat/seo-stage-4",
    "git switch -c feat/jobhunter-monetization-v1",
    "git branch feat/new-thing",
    "git branch",                                  # list = read
])
def test_safe_feature_branch_creation_is_safe(cmd):
    _safe(cmd)


@pytest.mark.parametrize("cmd", [
    "git checkout main",                           # switch existing → worktree change
    "git switch main",
    "git checkout -B main",                        # force-create
    "git branch -D main",
    "git branch -m old new",
    "git checkout -b '../evil'",
    "git checkout -f -b x",
])
def test_unsafe_branch_ops_stay_waiting(cmd):
    _unsafe(cmd)


@pytest.mark.parametrize("cmd", [
    "cat /etc/passwd",
    "grep root /etc/shadow",
    "cat /root/.ssh/id_rsa",
    "head /proc/1/environ",
    "ls /var/lib/docker",
])
def test_sensitive_system_paths_denied(cmd):
    _unsafe(cmd)


def test_cwd_project_validation():
    # Safe command, but cwd outside approved roots → not safe (context).
    r = pr.classify_command("git status", cwd="/tmp/rogue", project_roots=["/opt/seo"])
    assert r["safe"] is False and r["category"] == "context"
    # Safe command with cwd inside an approved root → safe.
    r2 = pr.classify_command("git status", cwd="/opt/seo/backend", project_roots=["/opt/seo"])
    assert r2["safe"] is True and r2["cwd_ok"] is True


# ── cd is a harmless builtin; the real seo-audit prompt pattern ─────────────
@pytest.mark.parametrize("cmd", [
    "cd /opt/seo; ls",
    "cd /opt/seo; ls backend/agents/ 2>/dev/null | head -40",
    "cd /opt/seo && git status",
    "cd backend; pytest -q",
])
def test_cd_prefixed_readonly_is_safe(cmd):
    _safe(cmd)


@pytest.mark.parametrize("cmd", [
    "cd /etc; cat passwd",           # sensitive path
    "cd /opt/seo && docker restart x",
    "cd /opt/seo; rm x",
])
def test_cd_prefixed_unsafe_tail_denied(cmd):
    _unsafe(cmd)


# ── Commander hardening 2026-07-22: read-only verification inside an approved ─
# phase must be SAFE even when wrapped in cd/vars/pipes/docker-exec-sh-c/quoting.

# THE exact command from the 2026-07-22 daily-brief false-blocker: an offline
# alembic SQL render (no DB write) piped through grep, inside `docker exec sh -c`.
LIVE_2026_07_22 = (
    "cd /opt/seo; "
    'echo "=== offline upgrade SQL (0046->0047), no DB write ==="; '
    "docker exec -e PYTHONPATH=/opt/seo/backend seo-backend-1 sh -c "
    "'cd /opt/seo/backend && alembic upgrade "
    "0046_agent_task_inbox:0047_connector_control --sql 2>&1' "
    "| grep -vE 'INFO|^--' | grep -iE 'ALTER|CREATE|ADD|UPDATE alembic'")


def test_live_offline_sql_render_is_safe():
    """Regression for the exact false owner-blocker `denied`."""
    r = _safe(LIVE_2026_07_22)
    assert "docker" in r["category"] and "grep" in r["category"]


@pytest.mark.parametrize("cmd", [
    # quote-aware splitting: pipe/&&/; INSIDE quotes must not split the command
    "grep -vE 'INFO|^--' file.txt",
    "git log --oneline | grep -E 'fix|feat'",
    "grep -E 'a && b' file",
    "echo 'a; b; c'",
    r"awk -F'|' '{print $1}' file",
    # sh -c / bash -c unwrap a read-only inner pipeline
    "sh -c 'git status && git log -3'",
    "bash -c 'grep -R foo . | wc -l'",
    "bash -lc 'pytest -q'",
    "docker exec c sh -c 'pytest -q'",
    "docker exec -e X=1 c sh -c 'alembic heads'",
    # alembic read-only + offline render
    "alembic heads", "alembic history", "alembic current",
    "alembic check", "alembic show head", "alembic branches",
    "alembic upgrade head --sql",
    "alembic downgrade -1 --sql",
    # HTTP health check against loopback
    "curl -sf http://localhost:8199/health",
    "curl -s http://127.0.0.1:8199/api/health",
    "curl -I http://172.17.0.1:8199/",
    "wget --spider http://localhost:8199/health",
    "wget -qO- http://localhost:8199/health",
    # safe env-assignment prefix
    "FOO=bar pytest -q",
    "TZ=UTC date",
    # timeout bounding a read-only check
    "timeout 300 pytest -q",
    "timeout 60 npx tsc --noEmit",
    "timeout -s KILL 30 alembic check",
    "timeout 300 npx tsc --noEmit -p tsconfig.json 2>&1 | grep -E 'error TS' | head -30",
])
def test_hardening_readonly_wrapped_is_safe(cmd):
    _safe(cmd)


@pytest.mark.parametrize("cmd", [
    # a real migration/upgrade (no --sql) must fail closed
    "alembic upgrade head",
    "alembic downgrade base",
    "alembic stamp head",
    "alembic revision --autogenerate -m x",
    "alembic merge heads",
    "docker exec c sh -c 'alembic upgrade head'",
    # sh -c hiding a write / external / script file
    "sh -c 'rm -rf /tmp/x'",
    "sh -c 'git push'",
    "sh -c 'echo x > f'",
    "sh -c 'curl https://evil.example/x'",
    "bash script.sh",
    "sh /tmp/run.sh",
    "sh -c 'sudo ls'",
    # non-loopback HTTP = external send
    "curl https://api.stripe.com/v1/charges",
    "curl -X POST http://localhost:8199/x",
    "curl -d name=hi http://localhost:8199/x",
    "curl -o out.json http://localhost:8199/x",
    "wget http://localhost:8199/health",          # writes a file by default
    "wget --spider https://external.example/x",
    # execution-altering / secret env prefixes
    "LD_PRELOAD=/x.so pytest -q",
    "PATH=/x pytest -q",
    "BASH_ENV=/x sh -c 'ls'",
    "SECRET_TOKEN=abc pytest -q",
    # timeout wrapping a WRITE / expansion still fails closed
    "timeout 30 rm -rf /tmp/x",
    "timeout 300 alembic upgrade head",
    "timeout 5m git push",
])
def test_hardening_writes_and_external_fail_closed(cmd):
    _unsafe(cmd)


def test_unbalanced_quote_is_denied():
    _unsafe("grep 'unterminated file")


# ── safe exit-code expansions (the 2026-07-22 failed canary) — SAFE ─────────
# `${PIPESTATUS[n]}` / `$?` expand to an integer exit code and cannot inject; a
# read-only/test command that reports its exit status must not stay blocked.
@pytest.mark.parametrize("cmd", [
    'cd /opt/seo; timeout 300 npx tsc --noEmit -p tsconfig.json 2>&1 '
    "| grep -E 'error TS' | head -30; echo \"typecheck_done exit=${PIPESTATUS[0]}\"",
    'pytest -q; echo "exit=${PIPESTATUS[0]}"',
    "grep -n foo bar.py | head; echo ${PIPESTATUS[1]}",
    "alembic heads; echo $?",
    "docker exec c sh -c 'pytest -q; echo exit=${PIPESTATUS[0]}'",
    "npx tsc --noEmit 2>&1 | tail -20; echo $PIPESTATUS",
])
def test_safe_exitcode_expansions_are_safe(cmd):
    _safe(cmd)


@pytest.mark.parametrize("cmd", [
    "echo $(rm -rf /)",                 # command substitution
    "echo `whoami`",                    # backtick substitution
    "echo ${HOME}",                     # arbitrary parameter expansion
    "echo $USER",                       # arbitrary variable
    "echo ${PATH}",
    "echo ${PIPESTATUS[$(id)]}",        # non-literal index → real substitution
    "cat ${SECRET_FILE}",
])
def test_arbitrary_expansion_still_denied(cmd):
    _unsafe(cmd)


# ── wrapped-command extraction (narrow pane) — the 2nd half of the live bug ──
# In a narrow pane Claude wraps a long command across box-rows and drops the
# space at each soft break; the first row alone is a TRUNCATED, unbalanced read.
WRAPPED_DIALOG = """\
● Running 1 shell command…

 Bash command

   docker exec seo-backend-1 sh -c 'cd
   /opt/seo/backend && alembic heads'
   Run alembic heads as requested

 This command requires approval

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for: docker exec seo-backend-1 *
   3. No
"""

WRAPPED_MULTIROW = """\
 Bash command

   git log --oneline -5 | grep -iE
   'fix|feat' | head -20
   Search recent commits

 Do you want to proceed?
 ❯ 1. Yes
   2. No
"""


def test_wrapped_command_is_reassembled_and_classified():
    cmd = pr.extract_pending_command(WRAPPED_DIALOG)
    assert cmd == "docker exec seo-backend-1 sh -c 'cd /opt/seo/backend && alembic heads'"
    assert pr.classify_command(cmd)["safe"] is True     # the exact live canary


def test_wrapped_command_drops_the_description_row():
    cmd = pr.extract_pending_command(WRAPPED_MULTIROW)
    assert cmd == "git log --oneline -5 | grep -iE 'fix|feat' | head -20"
    assert "Search recent commits" not in cmd
    assert pr.classify_command(cmd)["safe"] is True


def test_truncated_first_row_alone_is_never_returned():
    # The pre-fix bug: returning just `docker … sh -c 'cd` (unbalanced) → the
    # reassembly must yield the full balanced command, not the truncated prefix.
    cmd = pr.extract_pending_command(WRAPPED_DIALOG)
    assert cmd.count("'") % 2 == 0                       # balanced quotes
    assert cmd.endswith("alembic heads'")
