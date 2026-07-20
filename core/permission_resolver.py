"""Conservative permission resolver for owner-approved agents.

A single job: decide whether a shell command an agent is asking permission for is
**provably** a local, read-only, non-destructive action that a
already-owner-approved active task may run without a fresh owner prompt. Anything
not provably safe stays `waiting_owner`.

The safety rule is deny-by-default and fail-closed:
  * a command is safe only if EVERY pipeline segment's leading program is on the
    read-only allowlist AND passes that program's sub-rules;
  * any shell construct that can hide effects — output redirection (`>`/`>>`),
    input redirection that writes, command substitution (`$(…)`, backticks),
    process substitution (`<(…)`), background (`&`), variable expansion (`$VAR`),
    `sudo`, `eval`, `exec`, `xargs` — makes the whole command unsafe;
  * a path that looks like a secret (`.env`, `*.pem`, `id_rsa`, `credentials`, …)
    makes an otherwise-safe read unsafe (no secret access);
  * unknown programs are unsafe.

This module NEVER executes anything. It classifies. Delivery of an approval (a
keystroke to the agent) is a separate, allowlisted, audited step in
`core.agent_control`.
"""
from __future__ import annotations

import hashlib
import re
import shlex
from typing import Optional

# ── shell constructs that defeat static safety analysis → always unsafe ─────
_UNSAFE_CONSTRUCTS = (
    ">", ">>", "<", "`", "$(", "${", "$(", "<(", ">(", "&", "$", "\\\n",
)
# Segment separators that chain commands (each side must be independently safe).
_SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|\||;")

# Leading programs that are read-only and local. Each may have extra sub-rules.
_SAFE_PROGRAMS = {
    # file read / inspect
    "cat", "bat", "less", "more", "head", "tail", "ls", "ll", "dir", "find", "fd",
    "grep", "egrep", "fgrep", "rg", "ag", "wc", "stat", "file", "tree", "realpath",
    "dirname", "basename", "readlink", "cut", "sort", "uniq", "column", "nl", "od",
    "xxd", "hexdump", "strings", "diff", "cmp", "comm", "jq", "yq", "pwd", "tree",
    "sed", "awk", "gawk",   # gated by _check_sed / _check_awk (no in-place / system())
    # hashing / size (read-only)
    "md5sum", "sha1sum", "sha256sum", "cksum", "du", "df",
    # harmless
    "echo", "printf", "true", "false", "date", "whoami", "id", "hostname", "uname",
    "uptime", "which", "type", "command", "test", "[",
    # process / service status
    "ps", "pgrep", "pstree", "top", "htop", "free", "vmstat", "lsof", "who", "w",
    "systemctl", "journalctl", "service",
    # docker (read subcommands only)
    "docker",
    # git (read subcommands only)
    "git",
    # databases (read queries only)
    "psql", "mysql", "sqlite3", "redis-cli",
    # language / tooling read + tests
    "python", "python3", "pytest", "node", "npm", "npx", "pnpm", "yarn", "go",
    "cargo", "make", "tox", "ruff", "mypy", "flake8", "black", "eslint", "tsc",
    "curl",  # gated hard below (only --version / help); real curl denied
}

# Path fragments that indicate secrets — reading them is never auto-approved.
_SECRET_PATH_RE = re.compile(
    r"(\.env|/\.env|credential|secret|\.pem\b|\.key\b|id_rsa|id_ed25519|\.aws|"
    r"\.ssh/|\.npmrc|\.pgpass|\.netrc|token|password|apikey|api[_-]key|private[_-]?key)", re.I)

_GIT_READ = {"status", "diff", "log", "show", "branch", "rev-parse", "ls-files",
             "remote", "config", "describe", "blame", "shortlog", "cat-file",
             "rev-list", "for-each-ref", "reflog", "tag", "stash", "grep", "whatchanged"}
_GIT_WRITE = {"push", "commit", "reset", "rebase", "checkout", "switch", "merge",
              "clean", "rm", "mv", "add", "restore", "cherry-pick", "revert",
              "fetch", "pull", "clone", "init", "gc", "prune", "am", "apply", "worktree"}
_DOCKER_READ = {"ps", "inspect", "logs", "images", "image", "version", "info",
                "top", "stats", "port", "diff", "history", "events", "system",
                "context", "network", "volume", "container", "compose", "stack"}
_DOCKER_WRITE = {"run", "exec", "rm", "rmi", "stop", "start", "restart", "kill",
                 "build", "push", "pull", "create", "commit", "cp", "update",
                 "pause", "unpause", "login", "save", "load", "tag", "prune"}
_DOCKER_COMPOSE_READ = {"ps", "config", "logs", "images", "top", "version", "ls"}
_SYSTEMCTL_READ = {"status", "is-active", "is-enabled", "is-failed", "show",
                   "list-units", "list-unit-files", "list-timers", "cat", "show-environment"}
_SYSTEMCTL_WRITE = {"start", "stop", "restart", "reload", "enable", "disable",
                    "mask", "unmask", "kill", "set-property", "daemon-reload", "isolate"}

# SQL that is read-only. Any write/DDL keyword disqualifies.
_SQL_WRITE_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|"
    r"vacuum|reindex|merge|replace|call|do|set\s+role|begin|commit)\b", re.I)
_SQL_READ_START_RE = re.compile(r"^\s*\(?\s*(select|with|show|explain|table|\\d|\\l|\\dt|\\c\b|\\z|pragma|desc|describe)", re.I)


class _Unsafe(Exception):
    pass


def command_hash(command: str) -> str:
    return hashlib.sha256((command or "").strip().encode()).hexdigest()[:16]


def _has_unsafe_construct(command: str) -> Optional[str]:
    # `$` covers $VAR and $(...); we reject any unescaped $.
    for token in _UNSAFE_CONSTRUCTS:
        if token in command:
            return token
    return None


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        raise _Unsafe("unparseable quoting")


def _leading_program(tokens: list[str]) -> tuple[str, list[str]]:
    """Strip leading VAR=val assignments and return (program, args). An env
    assignment prefix is itself unsafe (it can set anything), so reject it."""
    if not tokens:
        raise _Unsafe("empty segment")
    if "=" in tokens[0] and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        raise _Unsafe("env-assignment prefix")
    prog = tokens[0].rsplit("/", 1)[-1]     # /usr/bin/grep → grep
    return prog, tokens[1:]


def _check_secret_paths(args: list[str]) -> None:
    for a in args:
        if _SECRET_PATH_RE.search(a):
            raise _Unsafe(f"secret path: {a}")


def _check_git(args):
    sub = next((a for a in args if not a.startswith("-")), None)
    if sub in _GIT_WRITE or sub not in _GIT_READ:
        raise _Unsafe(f"git {sub or '?'} not read-only")
    if sub == "config" and any(not a.startswith("-") and "=" in a for a in args[args.index(sub) + 1:]):
        raise _Unsafe("git config write")


def _check_docker(args):
    sub = next((a for a in args if not a.startswith("-")), None)
    if sub == "compose":
        rest = args[args.index("compose") + 1:]
        # A read subcommand token must be present and no write subcommand (skip
        # flag values like `-f docker-compose.yml` by testing membership).
        if any(a in _DOCKER_WRITE or a in ("up", "down", "restart", "start", "stop",
                                           "kill", "rm", "build", "run", "exec", "pull",
                                           "push", "create") for a in rest):
            raise _Unsafe("docker compose write subcommand")
        if not any(a in _DOCKER_COMPOSE_READ for a in rest):
            raise _Unsafe("docker compose: no read subcommand")
        return
    if sub in _DOCKER_WRITE or sub not in _DOCKER_READ:
        raise _Unsafe(f"docker {sub or '?'} not read-only")
    if any(a == "prune" for a in args):
        raise _Unsafe("docker prune")


def _check_systemctl(args):
    sub = next((a for a in args if not a.startswith("-")), None)
    if sub in _SYSTEMCTL_WRITE or (sub is not None and sub not in _SYSTEMCTL_READ):
        raise _Unsafe(f"systemctl {sub} not read-only")
    # `systemctl` with no subcommand lists units (read) — allowed.


def _check_sql_tool(prog, args):
    # Locate the query: the token after -c/--command/-e/--execute/--eval, or after
    # a bundled short-flag cluster ending in c/e (e.g. psql -tAc 'SELECT …').
    query = None
    for i, a in enumerate(args):
        if a in ("-c", "--command", "-e", "--execute", "--eval") or re.match(r"^-[A-Za-z]*[ce]$", a):
            query = args[i + 1] if i + 1 < len(args) else ""
            break
    if query is None:
        # No explicit query flag: only interactive meta-commands are safe.
        joined = " ".join(a for a in args if not a.startswith("-"))
        query = joined
    q = (query or "").strip().strip("'\"")
    if _SQL_WRITE_RE.search(q):
        raise _Unsafe("SQL write/DDL")
    if not _SQL_READ_START_RE.search(q):
        raise _Unsafe("SQL not a read query")


def _check_find(args):
    for bad in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf"):
        if bad in args:
            raise _Unsafe(f"find {bad}")


def _check_sed(args):
    for a in args:
        if a == "-i" or a.startswith("-i") or a in ("--in-place",):
            raise _Unsafe("sed -i (in-place write)")
        if "w" in a and a.startswith("-"):
            pass  # -w handled loosely; the write command 'w' inside script:
    # reject a sed script that writes files or executes: w file, e (execute)
    for a in args:
        if re.search(r"\b[we]\b", a) and not a.startswith("-"):
            if re.search(r"(^|;)\s*w\s+\S+|(^|;)\s*e\b", a):
                raise _Unsafe("sed write/execute command")


def _check_awk(args):
    prog_text = " ".join(args)
    if "system(" in prog_text or re.search(r">\s*\S", prog_text) or "print >" in prog_text or "| \"" in prog_text:
        raise _Unsafe("awk system()/redirect")


def _check_test_runner(prog, args):
    # python: only `-m pytest` / `-m <test>` or a bare test file; deny `-c` exec.
    if prog in ("python", "python3"):
        if "-c" in args:
            raise _Unsafe("python -c inline code")
        m = args.index("-m") if "-m" in args else None
        if m is not None:
            mod = args[m + 1] if m + 1 < len(args) else ""
            if mod not in ("pytest", "unittest", "mypy", "compileall", "json.tool"):
                raise _Unsafe(f"python -m {mod} not an allowed test/inspect module")
        elif not any(a == "--version" for a in args):
            # A bare script (e.g. `manage.py migrate`) can do anything — not safe.
            raise _Unsafe("python running a script is not a test/inspection")
    if prog in ("npm", "pnpm", "yarn"):
        sub = next((a for a in args if not a.startswith("-")), None)
        if sub not in ("test", "run", "ls", "list", "audit", "outdated", "--version", "view"):
            raise _Unsafe(f"{prog} {sub or '?'} not read/test")
        if sub == "run":
            script = args[args.index("run") + 1] if "run" in args and args.index("run") + 1 < len(args) else ""
            if not re.search(r"test|lint|typecheck|check", script or ""):
                raise _Unsafe(f"npm run {script} not a test/lint script")
    if prog in ("go",):
        sub = next((a for a in args if not a.startswith("-")), None)
        if sub not in ("test", "vet", "version", "env", "list"):
            raise _Unsafe(f"go {sub or '?'} not read/test")
    if prog in ("cargo",):
        sub = next((a for a in args if not a.startswith("-")), None)
        if sub not in ("test", "check", "clippy", "--version"):
            raise _Unsafe(f"cargo {sub or '?'} not read/test")
    if prog == "make":
        target = next((a for a in args if not a.startswith("-")), "")
        if target and not re.search(r"test|lint|check|typecheck", target):
            raise _Unsafe(f"make {target} not a test/lint target")


def _check_curl(args):
    # Real curl performs network I/O → deny. Only `--version`/`--help` allowed.
    if not any(a in ("--version", "-V", "--help", "-h", "--manual") for a in args):
        raise _Unsafe("curl performs network I/O")


def _classify_segment(segment: str) -> str:
    tokens = _tokens(segment)
    if not tokens:
        raise _Unsafe("empty")
    prog, args = _leading_program(tokens)
    if prog in ("sudo", "eval", "exec", "xargs", "env", "source", ".", "bash", "sh",
                "zsh", "nohup", "setsid", "watch", "time", "timeout", "nice"):
        # env/eval/exec/sudo/xargs/subshell wrappers hide the real command.
        raise _Unsafe(f"wrapper/privilege: {prog}")
    if prog not in _SAFE_PROGRAMS:
        raise _Unsafe(f"unknown program: {prog}")
    _check_secret_paths(args)
    if prog == "git":
        _check_git(args)
    elif prog == "docker":
        _check_docker(args)
    elif prog in ("systemctl", "service"):
        _check_systemctl(args)
    elif prog in ("psql", "mysql", "sqlite3", "redis-cli"):
        _check_sql_tool(prog, args)
    elif prog in ("find", "fd"):
        _check_find(args)
    elif prog == "sed":
        _check_sed(args)
    elif prog in ("awk", "gawk"):
        _check_awk(args)
    elif prog in ("python", "python3", "npm", "pnpm", "yarn", "go", "cargo", "make", "pytest", "node", "npx"):
        _check_test_runner(prog, args)
    elif prog == "curl":
        _check_curl(args)
    elif prog in ("tee", "dd"):
        raise _Unsafe(f"{prog} writes")
    return prog


def classify_command(command: str) -> dict:
    """Return {safe, category, reason, hash} for a shell command. Fail-closed."""
    cmd = (command or "").strip()
    result = {"command": cmd, "hash": command_hash(cmd), "safe": False,
              "category": "unknown", "reason": ""}
    if not cmd:
        result["reason"] = "empty command"
        return result
    bad = _has_unsafe_construct(cmd)
    if bad:
        result["reason"] = f"unsafe shell construct: {bad!r}"
        result["category"] = "shell_construct"
        return result
    segments = [s.strip() for s in _SEGMENT_SPLIT_RE.split(cmd) if s.strip()]
    if not segments:
        result["reason"] = "no command"
        return result
    progs = []
    try:
        for seg in segments:
            progs.append(_classify_segment(seg))
    except _Unsafe as e:
        result["reason"] = str(e)
        result["category"] = "denied"
        return result
    result["safe"] = True
    result["category"] = "read_only:" + ",".join(sorted(set(progs)))
    result["reason"] = "all segments are allowlisted read-only programs"
    return result


# ── extraction of the pending command from a Claude Code permission dialog ──
# The dialog shows the command under a "Bash command" header and asks
# "Do you want to proceed?" with numbered options.
_PROMPT_MARKER_RE = re.compile(r"(do you want to proceed|Yes, and don.t ask again|❯\s*1\.\s*Yes)", re.I)
_BASH_HEADER_RE = re.compile(r"(Bash command|Bash\b.*command|\$)\s*", re.I)


def is_permission_prompt(pane_tail: str) -> bool:
    return bool(_PROMPT_MARKER_RE.search(pane_tail or ""))


def extract_pending_command(pane_tail: str) -> Optional[str]:
    """Best-effort extraction of the command a permission dialog is asking about.

    Returns None when no command can be confidently isolated — in which case the
    resolver must keep waiting_owner rather than guess.
    """
    if not pane_tail or not is_permission_prompt(pane_tail):
        return None
    lines = [ln.rstrip() for ln in pane_tail.splitlines()]
    # Find the options block; the command is above "Do you want to proceed?".
    proceed_idx = next((i for i, ln in enumerate(lines) if re.search(r"do you want to proceed", ln, re.I)), None)
    if proceed_idx is None:
        return None
    # Find the "Bash command" header (or an inline "$ cmd" line) above the prompt.
    header_idx = None
    for i in range(proceed_idx - 1, max(-1, proceed_idx - 25), -1):
        stripped = lines[i].strip(" │╭╮╰╯─⎿●")
        if re.match(r"^\$\s+\S", stripped):
            return stripped.lstrip("$ ").strip() or None      # "$ <cmd>" inline
        if re.match(r"^Bash command\b", stripped, re.I):
            header_idx = i
            break
    if header_idx is None:
        return None
    # The command is the FIRST non-empty line after the header; any following
    # line is the human description and must be ignored.
    for j in range(header_idx + 1, proceed_idx):
        cmd = lines[j].strip(" │╭╮╰╯─⎿●>").strip()
        if cmd:
            return cmd
    return None
