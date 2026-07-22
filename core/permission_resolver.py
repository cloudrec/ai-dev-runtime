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
# Note: '&&' is a safe command chain (split into segments) — only a lone '&'
# (backgrounding) or a redirection is unsafe. '$' covers $VAR and $(...).
_UNSAFE_CONSTRUCTS = (
    ">", ">>", "<", "`", "$(", "${", "<(", ">(", "$", "\\\n",
)
_BACKGROUND_RE = re.compile(r"(?<!&)&(?!&)")   # a single '&' not part of '&&'
# Harmless redirects that discard/merge streams (write no real file): remove
# them before construct analysis so `2>/dev/null` / `2>&1` don't read as writes.
_SAFE_REDIRECT_RE = re.compile(r"(?:\d*>&\d+|\d*>\s*/dev/null|&>\s*/dev/null|<\s*/dev/null)")
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
    # directory change — no side effect; the sensitive-path checks still apply.
    "cd", "pushd", "popd",
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
    "alembic",  # gated by _check_alembic (heads/history/current/check + upgrade --sql only)
    "timeout",  # gated by _check_timeout: `timeout DURATION <safe-cmd>` bounds a read-only check
    "curl", "wget",  # gated hard below (--version/help or a loopback health check only)
    # POSIX shells — gated by _check_shell_c: only `-c <script>` unwraps to a
    # recursive safe classification; a script FILE or stdin stays unsafe.
    "sh", "bash", "zsh", "dash", "ash", "ksh", "mksh",
}

# Shells whose `-c <script>` argument we recursively re-classify.
_SHELLS = {"sh", "bash", "zsh", "dash", "ash", "ksh", "mksh"}
# Wrappers that hide the real command / escalate — always unsafe.
_HARD_WRAPPERS = {"sudo", "eval", "exec", "xargs", "env", "source", ".",
                  "nohup", "setsid", "watch", "time", "nice"}

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

# Alembic subcommands that never touch the DB or write a file.
_ALEMBIC_READ = {"heads", "history", "current", "check", "show", "branches"}
# A URL whose host is the local machine / docker host bridge — a health check,
# not an external send. Anything else is external network I/O (fail closed).
_LOOPBACK_RE = re.compile(
    r"^https?://(localhost|127(\.\d+){1,3}|\[::1\]|0\.0\.0\.0|"
    r"172\.17\.0\.1|host\.docker\.internal)(:\d+)?(/|$|\?)", re.I)
# Env-var names that can alter execution / inject code — never auto-approved as
# an assignment prefix (LD_PRELOAD, BASH_ENV, PYTHONSTARTUP, NODE_OPTIONS, …).
_UNSAFE_ENV_NAME_RE = re.compile(
    r"^(LD_|BASH_ENV$|ENV$|SHELLOPTS$|BASHOPTS$|PS4$|PROMPT_COMMAND$|IFS$|PATH$|"
    r"GLOBIGNORE$|PERL5OPT$|PYTHONSTARTUP$|PYTHONPATH$|NODE_OPTIONS$)", re.I)

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
    if _BACKGROUND_RE.search(command):
        return "&"
    return None


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        raise _Unsafe("unparseable quoting")


def _check_safe_assignment(tok: str) -> None:
    """A leading `NAME=value` assignment is safe only when NAME cannot alter
    execution (no LD_*/BASH_ENV/PATH/PYTHONPATH/…), is not secret-looking, and the
    value carries no shell expansion."""
    name, _, val = tok.partition("=")
    if _UNSAFE_ENV_NAME_RE.match(name):
        raise _Unsafe(f"unsafe env assignment: {name}")
    if re.search(r"(password|passwd|token|secret|api[_-]?key|private[_-]?key)", name, re.I):
        raise _Unsafe(f"secret env assignment: {name}")
    if re.search(r"[$`]", val):
        raise _Unsafe("env-assignment value has an expansion")


def _leading_program(tokens: list[str]) -> tuple[str, list[str]]:
    """Strip any leading provably-safe `VAR=val` assignments and return
    (program, args). An assignment that could alter execution or leak a secret
    makes the whole segment unsafe."""
    if not tokens:
        raise _Unsafe("empty segment")
    i = 0
    while i < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i]):
        _check_safe_assignment(tokens[i])
        i += 1
    if i >= len(tokens):
        raise _Unsafe("env assignments with no command")
    prog = tokens[i].rsplit("/", 1)[-1]     # /usr/bin/grep → grep
    return prog, tokens[i + 1:]


def _check_secret_paths(args: list[str]) -> None:
    for a in args:
        if _SECRET_PATH_RE.search(a):
            raise _Unsafe(f"secret path: {a}")


# A valid new git branch name (no refspecs, options, or path traversal).
_BRANCH_NAME_RE = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/\-]{1,100}$")
_GIT_FORCE_FLAGS = {"-f", "--force", "-B", "-C", "-M", "-D", "-d", "-m", "-r",
                    "--delete", "--move", "--force-create"}


def _check_git(args):
    sub = next((a for a in args if not a.startswith("-")), None)
    # Safe feature-branch CREATION (after worktree checks by the caller):
    # `git checkout -b NAME` / `git switch -c NAME` / `git branch NAME`.
    if sub in ("checkout", "switch"):
        if any(f in args for f in _GIT_FORCE_FLAGS):
            raise _Unsafe("git checkout/switch with force/delete flag")
        if not ("-b" in args or "-c" in args):
            raise _Unsafe("git checkout/switch without -b/-c changes the worktree")
        flag = "-b" if "-b" in args else "-c"
        name = args[args.index(flag) + 1] if args.index(flag) + 1 < len(args) else ""
        if not _BRANCH_NAME_RE.match(name):
            raise _Unsafe(f"invalid new branch name: {name!r}")
        return  # create-and-switch to a fresh branch — no file/remote change
    if sub == "branch":
        if any(f in args for f in _GIT_FORCE_FLAGS):
            raise _Unsafe("git branch delete/move/force")
        names = [a for a in args[args.index("branch") + 1:] if not a.startswith("-")]
        if names and not all(_BRANCH_NAME_RE.match(n) for n in names):
            raise _Unsafe("git branch: invalid name")
        return  # `git branch` (list = read) or `git branch NAME` (create)
    if sub in _GIT_WRITE or sub not in _GIT_READ:
        raise _Unsafe(f"git {sub or '?'} not read-only")
    if sub == "config" and any(not a.startswith("-") and "=" in a for a in args[args.index(sub) + 1:]):
        raise _Unsafe("git config write")


# Docker subcommands that run a command inside a container. Safe only when the
# INNER command is itself provably safe (recursive classification) — this is
# "local container test execution".
def _inner_command_safe(inner_tokens: list[str]) -> None:
    # Classify the ALREADY-TOKENIZED inner command as one segment. Never re-join
    # into a string (that would corrupt quoted metacharacters like `grep 'a|b'`);
    # a `sh -c '<pipeline>'` inner is unwrapped recursively by _classify_tokens.
    if not inner_tokens:
        raise _Unsafe("container run/exec with no explicit command (runs default entrypoint)")
    try:
        _classify_tokens(inner_tokens)
    except _Unsafe as e:
        raise _Unsafe(f"container inner command not safe: {e}")


def _check_docker(args):
    sub = next((a for a in args if not a.startswith("-")), None)
    if sub == "compose":
        rest = args[args.index("compose") + 1:]
        # find the compose subcommand (first bareword that is a known verb)
        verbs_read = _DOCKER_COMPOSE_READ
        verbs_exec = {"run", "exec"}
        csub = next((a for a in rest if a in verbs_read or a in verbs_exec
                     or a in ("up", "down", "restart", "start", "stop", "kill", "rm",
                              "build", "pull", "push", "create")), None)
        if csub in verbs_exec:
            # docker compose run [--rm] SERVICE CMD...  /  exec SERVICE CMD...
            after = rest[rest.index(csub) + 1:]
            # drop run/exec flags and their values, then service name, keep CMD.
            i = 0
            while i < len(after) and after[i].startswith("-"):
                i += 2 if after[i] in ("-e", "--env", "-w", "--workdir", "-u", "--user", "-p") else 1
            svc_and_cmd = after[i:]
            _inner_command_safe(svc_and_cmd[1:])   # svc_and_cmd[0] is the service
            return
        if csub in verbs_read:
            return
        raise _Unsafe(f"docker compose {csub or '?'} not read/test")
    if sub == "exec":
        after = args[args.index("exec") + 1:]
        i = 0
        while i < len(after) and after[i].startswith("-"):
            i += 2 if after[i] in ("-e", "--env", "-w", "--workdir", "-u", "--user") else 1
        cont_and_cmd = after[i:]
        _inner_command_safe(cont_and_cmd[1:])       # cont_and_cmd[0] is the container
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


def _check_alembic(args):
    """Read-only alembic only: heads/history/current/check/show/branches, plus
    `upgrade|downgrade … --sql` (offline SQL render to stdout — no DB write). A
    real migration (no `--sql`), revision, stamp, or merge is a WRITE → unsafe."""
    sub = next((a for a in args if not a.startswith("-")), None)
    if sub in _ALEMBIC_READ:
        return
    if sub in ("upgrade", "downgrade"):
        if "--sql" in args:
            return                       # offline SQL render — prints, never writes
        raise _Unsafe(f"alembic {sub} without --sql runs a live migration")
    raise _Unsafe(f"alembic {sub or '?'} is not a read-only check")


def _check_timeout(args):
    """`timeout [opts] DURATION <cmd>` bounds a command's runtime — it adds no
    write capability, so it is safe iff <cmd> is safe. Classify the inner command
    (no re-join: the tokens are already split)."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in ("-s", "--signal", "-k", "--kill-after"):
            i += 2                                   # option consumes a value
        else:
            i += 1
    if i >= len(args) or not re.match(r"^\d+(\.\d+)?[smhd]?$", args[i]):
        raise _Unsafe("timeout without a numeric duration")
    inner = args[i + 1:]
    if not inner:
        raise _Unsafe("timeout with no command")
    _classify_tokens(inner)


def _check_shell_c(prog, args):
    """`sh -c <script>` / `bash -lc <script>` unwrap to a recursive classification
    of <script>; that is a common, safe wrapper for a read-only pipeline. Any
    other form (a script FILE, reading stdin, a login shell running a file) hides
    the real command and stays unsafe."""
    ci = args.index("-c") if "-c" in args else next(
        (i for i, a in enumerate(args) if re.match(r"^-[A-Za-z]*c$", a)), None)
    if ci is None:
        raise _Unsafe(f"{prog} without -c runs a script/stdin, not a read-only check")
    # Nothing but flags may precede -c (a bareword before it would be a script file).
    if any(not a.startswith("-") for a in args[:ci]):
        raise _Unsafe(f"{prog} with a script argument before -c")
    script = args[ci + 1] if ci + 1 < len(args) else ""
    if not script.strip():
        raise _Unsafe(f"{prog} -c with an empty script")
    verdict = classify_command(script)
    if not verdict["safe"]:
        raise _Unsafe(f"{prog} -c inner command not safe: {verdict['reason']}")


_CURL_WRITE_FLAGS = {"-d", "--data", "--data-raw", "--data-binary", "--data-urlencode",
                     "-F", "--form", "-T", "--upload-file", "-o", "--output",
                     "-O", "--remote-name", "--data-ascii"}


def _check_curl(args):
    """`--version`/`--help`, or a read-only HTTP health check against a LOOPBACK
    host (GET/HEAD, no body, no upload, no output-to-file). Any non-loopback host
    is external network I/O → unsafe (fail closed)."""
    if any(a in ("--version", "-V", "--help", "-h", "--manual") for a in args):
        return
    urls = [a for a in args if re.match(r"^https?://", a, re.I)]
    if not urls:
        raise _Unsafe("curl without an explicit loopback health-check URL")
    for u in urls:
        if not _LOOPBACK_RE.match(u):
            raise _Unsafe(f"curl to a non-loopback host (external network I/O): {u}")
    for i, a in enumerate(args):
        if a in _CURL_WRITE_FLAGS or a.startswith("--data"):
            raise _Unsafe(f"curl {a} is a write/upload/output — not a read-only check")
        if a in ("-X", "--request"):
            method = (args[i + 1] if i + 1 < len(args) else "").upper()
            if method not in ("GET", "HEAD", ""):
                raise _Unsafe(f"curl -X {method} is not a read-only method")


def _check_wget(args):
    """wget writes a file by default → allow only a loopback `--spider` (no body)
    or stdout render (`-O-`/`-qO-`)."""
    if any(a in ("--version", "--help") for a in args):
        return
    urls = [a for a in args if re.match(r"^https?://", a, re.I)]
    if not urls or any(not _LOOPBACK_RE.match(u) for u in urls):
        raise _Unsafe("wget without a loopback URL is external network I/O")
    if "--spider" in args:
        return
    if any(a in ("-O-", "-qO-") or a == "-" for a in args) or (
            "-O" in args and args[args.index("-O") + 1: args.index("-O") + 2] == ["-"]):
        return
    raise _Unsafe("wget writes a file (use --spider or -O- for a read-only check)")


def _classify_segment(segment: str) -> str:
    return _classify_tokens(_tokens(segment))


def _classify_tokens(tokens: list[str]) -> str:
    """Classify one already-tokenized pipeline segment. Returns the program name
    or raises _Unsafe. Shells unwrap `-c <script>` recursively."""
    if not tokens:
        raise _Unsafe("empty")
    prog, args = _leading_program(tokens)
    if prog in _HARD_WRAPPERS:
        # env/eval/exec/sudo/xargs/subshell wrappers hide the real command.
        raise _Unsafe(f"wrapper/privilege: {prog}")
    if prog in _SHELLS:
        _check_secret_paths(args)
        _check_shell_c(prog, args)      # only `-c <script>` → recursive classify
        return prog
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
    elif prog == "alembic":
        _check_alembic(args)
    elif prog == "timeout":
        _check_timeout(args)
    elif prog == "curl":
        _check_curl(args)
    elif prog == "wget":
        _check_wget(args)
    elif prog in ("tee", "dd"):
        raise _Unsafe(f"{prog} writes")
    return prog


# Absolute paths under these system roots are off-limits even to read tools
# (host secrets/config), independent of the secret-name regex.
_SENSITIVE_ABS_RE = re.compile(
    r"(^|[\s'\"=])(/etc(/|\b)|/root/\.ssh|/root/\.aws|/proc/\d+/environ|/sys(/|\b)|/boot(/|\b)|"
    r"/var/lib/docker|/home/[^/]+/\.ssh|/\.ssh)", re.I)


def _check_sensitive_abs_paths(command: str) -> None:
    if _SENSITIVE_ABS_RE.search(command):
        raise _Unsafe("references a sensitive system path")


def _mask_quotes(s: str) -> str:
    """Return `s` with the CONTENTS of every quoted span replaced by 'x',
    preserving length and the quote/operator characters that sit OUTSIDE quotes.
    This lets the quote-blind construct/segment regexes see only real (unquoted)
    shell operators — so `grep 'A|B'` or `sh -c '… && …'` are no longer split or
    flagged on metacharacters that live inside quotes. Raises on unbalanced
    quotes (which we then treat as unsafe rather than guessing)."""
    out: list[str] = []
    i, n, quote = 0, len(s), None
    while i < n:
        c = s[i]
        if quote is None:
            if c in ("'", '"'):
                quote = c
                out.append(c)
            elif c == "\\":
                out.append(c)
                if i + 1 < n:
                    out.append("x")
                    i += 2
                    continue
            else:
                out.append(c)
        elif quote == "'":                    # single quotes: literal, end only on '
            if c == "'":
                quote = None
                out.append(c)
            else:
                out.append("x")
        else:                                 # double quotes
            # Double quotes do NOT suppress `$`/backtick expansion, so those stay
            # visible to the construct scan; a backslash escapes the next char
            # (masked as literal); every other char is inert and masked.
            if c == "\\":
                out.append(c)
                if i + 1 < n:
                    out.append("x")
                    i += 2
                    continue
            elif c == '"':
                quote = None
                out.append(c)
            elif c in ("$", "`"):
                out.append(c)                 # expansion/substitution still active
            else:
                out.append("x")
        i += 1
    if quote is not None:
        raise _Unsafe("unbalanced quote")
    return "".join(out)


def _split_segments(original: str, scan: str) -> list[str]:
    """Split `original` into pipeline/chain segments using separator positions
    found in the index-aligned quote-masked `scan` (so separators inside quotes
    are ignored). Returns the ORIGINAL substrings (real quoting intact)."""
    segs, last = [], 0
    for m in _SEGMENT_SPLIT_RE.finditer(scan):
        segs.append(original[last:m.start()])
        last = m.end()
    segs.append(original[last:])
    return [s.strip() for s in segs if s.strip()]


def classify_command(command: str, cwd: str | None = None,
                     project_roots: list[str] | None = None) -> dict:
    """Classify a shell command. Fail-closed. Optionally validate the agent's
    cwd/project context (not keywords): the working directory must sit inside an
    approved project root, and the command must not reference sensitive system
    paths. Returns {safe, category, reason, hash, cwd_ok}."""
    cmd = (command or "").strip()
    result = {"command": cmd, "hash": command_hash(cmd), "safe": False,
              "category": "unknown", "reason": "", "cwd_ok": None}
    if not cmd:
        result["reason"] = "empty command"
        return result
    try:
        # Quote-aware analysis: mask quoted spans (length-preserving) so the
        # construct/segment regexes only ever see UNQUOTED shell operators, then
        # blank the harmless /dev/null and stderr-merge redirects.
        masked = _mask_quotes(cmd)
        scan = _SAFE_REDIRECT_RE.sub(lambda m: " " * len(m.group(0)), masked)
    except _Unsafe as e:
        result["reason"] = str(e)
        result["category"] = "denied"
        return result
    bad = _has_unsafe_construct(scan)
    if bad:
        result["reason"] = f"unsafe shell construct: {bad!r}"
        result["category"] = "shell_construct"
        return result
    segments = _split_segments(cmd, scan)
    if not segments:
        result["reason"] = "no command"
        return result
    progs = []
    try:
        # Sensitive absolute paths are checked on the ORIGINAL command (a secret
        # path is sensitive whether or not it is quoted).
        _check_sensitive_abs_paths(cmd)
        for seg in segments:
            progs.append(_classify_segment(seg))
    except _Unsafe as e:
        result["reason"] = str(e)
        result["category"] = "denied"
        return result

    # Context validation: the agent must be operating inside an approved project.
    if project_roots is not None:
        import os
        real = os.path.realpath(cwd) if cwd else ""
        roots = [os.path.realpath(r) for r in project_roots]
        result["cwd_ok"] = bool(real) and any(real == r or real.startswith(r + os.sep) for r in roots)
        if not result["cwd_ok"]:
            result["reason"] = f"cwd {cwd!r} is not inside an approved project root"
            result["category"] = "context"
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
