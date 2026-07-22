"""Narrow routine-push auto-approval for managed approved projects.

A `git push` is UNSAFE by default (the static resolver denies it). This module
grants a NARROW, stateful exception so the owner is not asked for every normal
push: a ROUTINE push of the CURRENT branch to its EXISTING configured upstream,
in a managed registered project, whose approved work is committed — and NOTHING
unusual. Any deviation STOPS with an exact reason.

Auto-approve only when ALL hold:
  * a managed registered project (caller passes the project record with the
    expected remote repo);
  * commits exist and HEAD is not behind the upstream (no history rewrite);
  * the push targets the current branch's existing, unchanged configured upstream;
  * NO force / force-with-lease / tags / delete / mirror / all / prune /
    set-upstream / new upstream / other branch / protected branch / colon refspec;
  * the working tree has no uncommitted tracked changes;
  * no secret-named file is introduced by the commits being pushed;
  * the remote URL matches the project record (account/repo).

It reads git state READ-ONLY (never pushes, never rewrites), never prints a
credential, and uses GIT_TERMINAL_PROMPT=0 so it can never hang on an auth prompt.
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Optional

from core import permission_resolver as pr

# Flags that make a push non-routine → always deny.
_FORBIDDEN_FLAGS = {
    "-f", "--force", "--force-with-lease", "--force-if-includes", "--tags",
    "--follow-tags", "--delete", "-d", "--mirror", "--all", "--prune",
    "--set-upstream", "-u", "--repo", "--receive-pack", "--exec", "--signed",
    "-o", "--push-option", "--no-verify",
}
# Benign flags that do not change WHAT is pushed.
_ALLOWED_FLAGS = {"-q", "--quiet", "-v", "--verbose", "--progress", "--no-progress",
                  "--porcelain", "--atomic"}
_DEFAULT_PROTECTED = ("main", "master", "release", "prod", "production")


class PushPolicyError(Exception):
    pass


def is_push_command(command: str) -> bool:
    """True when the command's real action is a `git push` (possibly after a
    `cd`/`&&` prefix). Used to route to this policy instead of a blanket deny."""
    try:
        seg = _push_segment(command)
        return seg is not None
    except PushPolicyError:
        return False


def _segments(command: str) -> list[str]:
    """Quote-aware split into pipeline/chain segments (reuse the resolver)."""
    masked = pr._mask_quotes(command)
    scan = pr._SAFE_REDIRECT_RE.sub(lambda m: " " * len(m.group(0)), masked)
    return pr._split_segments(command, scan)


def _push_segment(command: str) -> Optional[list[str]]:
    """Return the tokens of the git-push segment, or None. Every OTHER segment
    must be a harmless `cd`/read-only builtin — a push hidden among writes is not
    routine."""
    push_tokens = None
    for seg in _segments(command):
        try:
            toks = pr._tokens(seg)
        except pr._Unsafe:
            raise PushPolicyError("unparseable command")
        if not toks:
            continue
        prog = toks[0].rsplit("/", 1)[-1]
        if prog == "git":
            sub = next((a for a in toks[1:] if not a.startswith("-")), None)
            if sub == "push":
                if push_tokens is not None:
                    raise PushPolicyError("more than one git push")
                push_tokens = toks
            else:
                raise PushPolicyError(f"non-push git command present: git {sub}")
        elif prog in ("cd", "pushd", "popd", "true", ":"):
            continue
        else:
            raise PushPolicyError(f"non-routine command present: {prog}")
    return push_tokens


def parse_push(command: str) -> dict:
    """Parse + shape-check the push. Returns {remote, refspec} for an allowed
    SHAPE, or raises PushPolicyError with the exact reason. Shape only — the repo
    state is checked separately."""
    toks = _push_segment(command)
    if toks is None:
        raise PushPolicyError("not a git push")
    args = toks[toks.index("push") + 1:]
    positionals = []
    for a in args:
        if a.startswith("-"):
            if a in _FORBIDDEN_FLAGS or a.split("=")[0] in _FORBIDDEN_FLAGS:
                raise PushPolicyError(f"forbidden push flag: {a}")
            if a not in _ALLOWED_FLAGS:
                raise PushPolicyError(f"unrecognised push flag (fail closed): {a}")
        else:
            positionals.append(a)
    if len(positionals) > 2:
        raise PushPolicyError("too many push arguments (only `<remote> <branch>` allowed)")
    remote = positionals[0] if positionals else None
    refspec = positionals[1] if len(positionals) > 1 else None
    if refspec is not None:
        if refspec.startswith("+"):
            raise PushPolicyError("force refspec ('+') not allowed")
        if ":" in refspec:
            raise PushPolicyError("explicit src:dst refspec not allowed")
        if refspec.lower().startswith("refs/tags/") or refspec == "--tags":
            raise PushPolicyError("tag push not allowed")
    return {"remote": remote, "refspec": refspec}


def _git(cwd: str, *args: str, timeout: int = 12) -> tuple[int, str]:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        p = subprocess.run(["git", "-C", cwd, *args], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout, env=env)
        return p.returncode, p.stdout.decode("utf-8", "replace").strip()
    except Exception as e:  # noqa: BLE001
        return 1, str(e)[:200]


def _normalise_remote(url: str) -> str:
    """host/owner/repo, lowercased, no scheme/credentials/.git — for comparing a
    remote URL to the project record without ever exposing a token."""
    u = url.strip()
    u = re.sub(r"^[a-z]+://", "", u, flags=re.I)     # scheme
    u = re.sub(r"^[^@/]+@", "", u)                    # user[:token]@  (strips creds)
    u = u.replace(":", "/", 1) if "@" not in u and "/" not in u.split(":", 1)[0] else u
    u = re.sub(r"\.git$", "", u.strip("/"))
    return u.lower()


def evaluate_push(command: str, cwd: str, project: dict) -> dict:
    """Decide whether this push may be auto-approved. Returns
    {allowed, reason, checks}. Deny-by-default: any failed check stops with the
    exact reason. `project` carries `push_repo` (expected host/owner/repo) and an
    optional `protected_branches` list."""
    checks: dict = {}

    def deny(reason: str) -> dict:
        return {"allowed": False, "reason": reason, "checks": checks}

    try:
        shape = parse_push(command)
    except PushPolicyError as e:
        return deny(str(e))
    checks["shape"] = shape

    if not cwd or _git(cwd, "rev-parse", "--is-inside-work-tree")[1] != "true":
        return deny("not inside a git work tree")

    rc, branch = _git(cwd, "symbolic-ref", "--short", "HEAD")
    if rc != 0 or not branch:
        return deny("detached HEAD or no current branch")
    checks["branch"] = branch

    protected = [b.lower() for b in (project.get("protected_branches") or _DEFAULT_PROTECTED)]
    if branch.lower() in protected or any(branch.lower().startswith(p + "/") for p in protected):
        return deny(f"current branch {branch!r} is protected")

    rc, upstream = _git(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if rc != 0 or "/" not in upstream:
        return deny("no configured upstream for the current branch")
    up_remote, up_branch = upstream.split("/", 1)
    checks["upstream"] = upstream

    # The push must resolve to that SAME upstream — remote and (if given) branch.
    remote = shape["remote"] or up_remote
    if remote != up_remote:
        return deny(f"push remote {remote!r} != upstream remote {up_remote!r}")
    if shape["refspec"] not in (None, "HEAD", branch):
        return deny(f"refspec {shape['refspec']!r} is not the current branch/HEAD")
    if shape["refspec"] in (branch,) and up_branch != branch:
        return deny(f"branch {branch!r} maps to a different upstream branch {up_branch!r}")
    checks["remote"] = remote

    # Remote URL must match the project record (account/repo), creds stripped.
    expected = (project.get("push_repo") or "").strip()
    rc, url = _git(cwd, "remote", "get-url", remote)
    if rc != 0 or not url:
        return deny(f"cannot read remote url for {remote!r}")
    got = _normalise_remote(url)
    checks["remote_repo"] = got
    if not expected:
        return deny("project record has no push_repo to match against")
    if got != _normalise_remote(expected):
        return deny(f"remote {got!r} does not match project record {_normalise_remote(expected)!r}")

    # Commits exist; HEAD not behind upstream (no non-fast-forward / rewrite).
    rc, head = _git(cwd, "rev-parse", "HEAD")
    if rc != 0 or not re.fullmatch(r"[0-9a-f]{40}", head):
        return deny("no HEAD commit")
    checks["head"] = head
    rc, behind = _git(cwd, "rev-list", "--count", "HEAD..@{u}")
    if rc != 0:
        return deny("cannot compare with upstream")
    if behind != "0":
        return deny(f"HEAD is behind upstream by {behind} (would need force/rebase)")
    _, ahead = _git(cwd, "rev-list", "--count", "@{u}..HEAD")
    checks["ahead"] = ahead

    # Working tree: no uncommitted tracked changes (untracked files are not pushed).
    rc, porc = _git(cwd, "status", "--porcelain", "--untracked-files=no")
    if rc != 0:
        return deny("cannot read working tree status")
    if porc.strip():
        return deny("uncommitted tracked changes present (task not fully committed)")

    # No secret-named file introduced by the commits being pushed.
    if ahead not in ("0", ""):
        rc, names = _git(cwd, "diff", "--name-only", "@{u}..HEAD")
        if rc == 0:
            for f in names.splitlines():
                if pr._SECRET_PATH_RE.search(f) or pr._SENSITIVE_ABS_RE.search("/" + f):
                    return deny(f"push introduces a secret-named file: {f}")
        checks["pushed_files"] = len([n for n in names.splitlines() if n])

    return {"allowed": True, "reason": "routine current-branch push to existing upstream",
            "checks": checks}


def verify_push(cwd: str, branch: str, expected_sha: str) -> dict:
    """After the push, the remote branch SHA must equal the local HEAD. Any
    mismatch stops and surfaces the exact reason. Read-only ls-remote."""
    rc, out = _git(cwd, "ls-remote", "--heads", "origin", branch)
    if rc != 0 or not out:
        return {"ok": False, "reason": f"cannot read remote ref for {branch!r}: {out[:120]}"}
    remote_sha = out.split()[0]
    ok = remote_sha == expected_sha
    return {"ok": ok, "remote_sha": remote_sha, "local_head": expected_sha,
            "reason": "remote SHA matches local HEAD" if ok
                      else f"remote {remote_sha[:12]} != local HEAD {expected_sha[:12]}"}
